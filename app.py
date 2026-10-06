import logging
import os
import secrets
from datetime import datetime, UTC, timedelta

import json
from urllib.request import Request as URLRequest, urlopen
from flask import Flask, render_template, redirect, url_for, send_from_directory, request, session
from flask import jsonify
from flask_talisman import Talisman
from flask_wtf import CSRFProtect
# noinspection PyProtectedMember
from flask_wtf.csrf import CSRFError
from waitress import serve
from werkzeug.exceptions import BadRequest, HTTPException
from werkzeug.security import check_password_hash

from api import register_request_api_routes, register_get_api_routes, \
    register_setup_api_routes, register_transfer_api_routes, register_admin_api_routes
from api.admin import sync_admin_wallet
from bank_lib import SetupForm
from bank_lib.database import init_db, is_db_initialized, execute_query, execute_query_dict
from bank_lib.decorator import admin_required, login_required
from bank_lib.form_CSRF_validators import LoginForm, RequestWalletForm, FreezeForm, ResetForm, BurnForm, \
    MintCurrencyForm, BurnCurrencyForm, CreateWalletForm, RulesForm, RequestForm, AdminLogForm, AdminRequestsForm
from bank_lib.form_validators import TransferForm, ResetPasswordForm, BankTransferForm, RefundForm, SqlQueryForm, \
    DelAccountForm
from bank_lib.get_data import get_settings, get_total_currency, get_user_by_wallet_name, get_user_account_profile, charge_monthly_tier_fee
from bank_lib.global_vars import DB_POOL
from bank_lib.log_module import create_log, rotate_logs
from bank_lib.minebank_auth import (login_client, logout_client, get_client_accounts, set_wallet_pin,
                                    verify_wallet_pin, require_minebank_login, require_role, require_admin_reauth,
                                    reauthenticate_admin, create_account)
from bank_lib.minebank_core import (transfer as minebank_transfer, approve_transfer, reject_transfer,
                                     deposit as minebank_deposit, withdraw as minebank_withdraw,
                                     repay_credit, activate_credit, chargeback, accrue_monthly_credit_interest)
from bank_lib.minebank_api import create_api_credential, authenticate_api_credential, revoke_api_credential

# Set up logging once in your app setup code (if not already done)
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Configuration
secret_key = os.environ.get("SECRET_KEY", None)
if secret_key is None:
    secret_key = secrets.token_hex(32)
    logging.warning("SECRET_KEY environment variable not set - Using random value")
    logging.warning("Please note this means any reboot to the server invalidates ALL sessions, so it is recommended to set the global variable properly")

app = Flask(__name__, static_folder='static')
app.config.update(
    SECRET_KEY=secret_key,  # Do NOT hardcode
    SESSION_COOKIE_HTTPONLY=True,  # JS can't access cookies
    SESSION_COOKIE_SECURE=True,  # Only send cookies over HTTPS
    SESSION_COOKIE_SAMESITE='Strict',  # Prevent CSRF (see below)
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),  # Auto-expire sessions
    PREFERRED_URL_SCHEME='https',  # Flask will prefer https for URL generation
)

Talisman(
    app=app,
    strict_transport_security=True,
    strict_transport_security_preload=True,
    strict_transport_security_max_age=31536000,
    frame_options='DENY',
    content_security_policy={
        'default-src': "'self'",
        'script-src': "'self' https://cdn.jsdelivr.net",
        'style-src': "'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        'img-src': "'self' data:",
        'connect-src': "'self'",
    }
)

csrf = CSRFProtect(app)

# Register API routes
register_request_api_routes(app)
register_get_api_routes(app)
register_setup_api_routes(app)
register_transfer_api_routes(app)
register_admin_api_routes(app)


# Error handlers
@app.errorhandler(BadRequest)
def handle_bad_request(e):
    if wants_html_response():
        return render_template('error.html', message=f"Invalid request data => {e.description}"), 400
    else:
        return jsonify(error="Invalid request data"), 400


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    # You can return a JSON error or render a template as you prefer
    return jsonify({"error": "CSRF validation failed", "details": str(e.description)}), 400


@app.errorhandler(Exception)
def handle_general_error(e):
    if isinstance(e, HTTPException):
        code = e.code
        description = e.description
    else:
        code = 500
        description = "An unexpected error occurred."

    if wants_html_response():
        return render_template('error.html', message=description), code
    else:
        return jsonify(error=description), code


def wants_html_response():
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return best == 'text/html' and request.accept_mimetypes[best] > request.accept_mimetypes['application/json']


# ---------------------------------------------------------------------------
# MineBank v2 portal routes. The legacy wallet routes remain available while
# the migration is completed.
# ---------------------------------------------------------------------------
@app.route('/portal/login', methods=['GET', 'POST'])
def minebank_login():
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password') or ''
        ok, error = login_client(email, password)
        if ok:
            return redirect(url_for('minebank_dashboard'))
        return render_template('minebank_login.html', error=error, settings=get_settings(),
                               is_admin=False, is_logged_in=False)
    return render_template('minebank_login.html', settings=get_settings(),
                           is_admin=False, is_logged_in=False)


@app.route('/portal/logout')
def minebank_logout():
    logout_client()
    return redirect(url_for('home'))


@app.route('/portal')
@require_minebank_login
def minebank_dashboard():
    client_id = session['minebank_client_id']
    accounts = get_client_accounts(client_id)
    if accounts and not session.get('minebank_account_id'):
        session['minebank_account_id'] = accounts[0][0]
    return render_template('minebank_dashboard.html', accounts=accounts,
                           selected_account_id=session.get('minebank_account_id'),
                           settings=get_settings(), is_logged_in=True,
                           is_admin=session.get('minebank_role') == 'ADMIN')


@app.route('/portal/account/select', methods=['POST'])
@require_minebank_login
def minebank_select_account():
    account_id = request.form.get('account_id', type=int)
    accounts = get_client_accounts(session['minebank_client_id'])
    allowed = {row[0] for row in accounts}
    if account_id not in allowed:
        return jsonify({"error": "Account does not belong to the logged-in client."}), 403
    session['minebank_account_id'] = account_id
    return redirect(url_for('minebank_dashboard'))


@app.route('/portal/security/pin', methods=['POST'])
@require_minebank_login
def minebank_set_pin():
    pin = request.form.get('pin') or ''
    confirmation = request.form.get('pin_confirmation') or ''
    if pin != confirmation:
        return jsonify({"error": "PIN confirmation does not match."}), 400
    try:
        set_wallet_pin(session['minebank_client_id'], pin)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return redirect(url_for('minebank_dashboard'))


@app.route('/api/v2/transfer', methods=['POST'])
@require_minebank_login
def minebank_transfer_api():
    data = request.get_json(silent=True) or {}
    pin = str(data.get('wallet_pin') or '')
    ok, error = verify_wallet_pin(session['minebank_client_id'], pin)
    if not ok:
        return jsonify({"error": error}), 401

    account_id = int(data.get('sender_account_id') or session.get('minebank_account_id') or 0)
    accounts = {row[0] for row in get_client_accounts(session['minebank_client_id'])}
    if account_id not in accounts:
        return jsonify({"error": "Invalid sender account."}), 403
    try:
        amount = int(data.get('amount'))
        result = minebank_transfer(
            sender_account_id=account_id,
            recipient_account_number=str(data.get('recipient_account_number') or '').strip(),
            amount=amount,
            description=data.get('description'),
            reference=data.get('reference'),
        )
        return jsonify(result), 201
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route('/api/v2/admin/reauth', methods=['POST'])
@require_role('ADMIN')
def minebank_admin_reauth():
    password = (request.get_json(silent=True) or {}).get('password') or ''
    if not reauthenticate_admin(session['minebank_client_id'], password):
        return jsonify({"error": "Admin re-authentication failed."}), 401
    return jsonify({"message": "Admin re-authenticated."})


@app.route('/api/v2/admin/clients', methods=['POST'])
@require_role('ADMIN', 'OPERATOR')
def minebank_create_client():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    role = (data.get('role') or 'CLIENT').upper()
    if not email or len(password) < 8 or role not in ('CLIENT', 'OPERATOR', 'ADMIN'):
        return jsonify({"error": "Email, password (8+ chars) and valid role are required."}), 400
    if role == 'ADMIN' and session.get('minebank_role') != 'ADMIN':
        return jsonify({"error": "Only an Admin can create another Admin."}), 403
    conn = None
    from werkzeug.security import generate_password_hash
    try:
        from bank_lib.database import get_db_connection, release_db_connection
        conn = get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM bank_clients WHERE LOWER(email)=LOWER(%s)", (email,))
                if cur.fetchone():
                    return jsonify({"error": "Client email already exists."}), 409
                cur.execute(
                    "INSERT INTO bank_clients(email,password_hash,role) VALUES(%s,%s,%s) RETURNING id,email,role,status",
                    (email, generate_password_hash(password), role),
                )
                client = cur.fetchone()
        return jsonify({"id": client[0], "email": client[1], "role": client[2], "status": client[3]}), 201
    finally:
        if conn is not None:
            from bank_lib.database import release_db_connection
            release_db_connection(conn)


@app.route('/api/v2/accounts', methods=['POST'])
@require_role('ADMIN', 'OPERATOR')
def minebank_create_account_api():
    data = request.get_json(silent=True) or {}
    try:
        client_id = int(data.get('client_id'))
        account_type = str(data.get('account_type') or 'PERSONAL').upper()
        tier_code = str(data.get('tier_code') or ('PERSONAL' if account_type == 'PERSONAL' else 'BUSINESS')).upper()
        account = create_account(client_id, account_type, tier_code)
        return jsonify({"id": account[0], "account_number": account[1]}), 201
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


def _api_scope(required):
    auth = authenticate_api_credential(request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    if not auth or required not in auth["scopes"] and "admin" not in auth["scopes"]:
        return None
    return auth


@app.route('/api/v1/accounts', methods=['GET'])
def api_v1_accounts():
    auth = _api_scope("accounts:read")
    if not auth:
        return jsonify({"error":"API authentication failed"}), 401
    return jsonify({"accounts":[
        {"id":r[0],"account_number":r[1],"account_type":r[2],"tier":r[3],
         "display_name":r[4],"balance":r[5],"status":r[6],"monthly_outgoing_used":r[7],
         "monthly_outgoing_limit":r[8],"max_balance":r[9],"credit_enabled":r[10]}
        for r in get_client_accounts(auth["client_id"])
    ]})


@app.route('/api/v1/transactions', methods=['GET'])
def api_v1_transactions():
    auth = _api_scope("transactions:read")
    if not auth:
        return jsonify({"error":"API authentication failed"}), 401
    conn=None
    try:
        from bank_lib.database import get_db_connection
        conn=get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT l.transaction_id,l.transaction_type,l.amount,l.fee,l.currency,
                                      l.status,l.description,l.reference_id,l.created_at
                               FROM ledger_transactions l
                               JOIN bank_accounts a ON a.id=COALESCE(l.sender_account_id,l.recipient_account_id)
                               WHERE a.client_id=%s ORDER BY l.created_at DESC LIMIT 200""",(auth["client_id"],))
                rows=cur.fetchall()
        return jsonify({"transactions":[{"transaction_id":r[0],"type":r[1],"amount":r[2],"fee":r[3],
          "currency":r[4],"status":r[5],"description":r[6],"reference":r[7],"created_at":r[8].isoformat()} for r in rows]})
    finally:
        if conn is not None:
            from bank_lib.database import release_db_connection
            release_db_connection(conn)


@app.route('/api/v1/transfers', methods=['POST'])
def api_v1_transfers():
    auth=_api_scope("transfers:write")
    if not auth:
        return jsonify({"error":"API authentication failed"}),401
    data=request.get_json(silent=True) or {}
    try:
        account_id=int(data.get("sender_account_id"))
        allowed={r[0] for r in get_client_accounts(auth["client_id"])}
        if account_id not in allowed:
            return jsonify({"error":"Sender account is not owned by API client"}),403
        result=minebank_transfer(sender_account_id=account_id,
            recipient_account_number=str(data.get("recipient_account_number") or ""),
            amount=int(data.get("amount")),description=data.get("description"),
            reference=data.get("reference"),idempotency_key=request.headers.get("Idempotency-Key"))
        return jsonify(result),201
    except (ValueError,TypeError) as exc:
        return jsonify({"error":str(exc)}),400


@app.route('/api/v1/credit', methods=['POST'])
def api_v1_credit():
    auth=_api_scope("credit:write")
    if not auth:
        return jsonify({"error":"API authentication failed"}),401
    data=request.get_json(silent=True) or {}
    try:
        account_id=int(data.get("account_id"))
        allowed={r[0] for r in get_client_accounts(auth["client_id"])}
        if account_id not in allowed:
            return jsonify({"error":"Account is not owned by API client"}),403
        if data.get("action")=="activate":
            return jsonify(activate_credit(account_id,int(data["limit"])))
        if data.get("action")=="repay":
            return jsonify(repay_credit(account_id,int(data["amount"]),int(data["source_account_id"])))
        return jsonify({"error":"Unknown credit action"}),400
    except (ValueError,TypeError) as exc:
        return jsonify({"error":str(exc)}),400


@app.route('/api/v1/users', methods=['POST'])
def api_v1_users():
    auth=_api_scope("admin")
    if not auth:
        return jsonify({"error":"Admin API scope required"}),403
    data=request.get_json(silent=True) or {}
    from werkzeug.security import generate_password_hash
    email=str(data.get("email") or "").strip().lower()
    password=str(data.get("password") or "")
    role=str(data.get("role") or "CLIENT").upper()
    if not email or len(password)<8 or role not in ("CLIENT","OPERATOR","ADMIN"):
        return jsonify({"error":"Invalid user payload"}),400
    conn=None
    try:
        from bank_lib.database import get_db_connection
        conn=get_db_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO bank_clients(email,password_hash,role) VALUES(%s,%s,%s) RETURNING id,email,role",
                            (email,generate_password_hash(password),role))
                row=cur.fetchone()
        return jsonify({"id":row[0],"email":row[1],"role":row[2]}),201
    except Exception as exc:
        return jsonify({"error":str(exc)}),400
    finally:
        if conn is not None:
            from bank_lib.database import release_db_connection
            release_db_connection(conn)


@app.route('/api/v1/credentials', methods=['POST'])
@require_role('ADMIN')
@require_admin_reauth
def api_v1_create_credential():
    data=request.get_json(silent=True) or {}
    try:
        client_id=int(data["client_id"])
        scopes=data.get("scopes") or ["accounts:read","transactions:read"]
        return jsonify(create_api_credential(client_id,str(data.get("name") or "Minecraft"),scopes)),201
    except (KeyError,ValueError,TypeError) as exc:
        return jsonify({"error":str(exc)}),400


# Routes
@app.route('/')
def home():
    if not is_db_initialized():
        return redirect(url_for('setup_page'))

    if DB_POOL is None:
        return render_template('error.html',
                               message="Database is not initialized. Please set up the system by putting the required ENV variables.")

    settings = get_settings()
    return render_template('home.html', settings=settings, is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


@app.route('/setup', methods=['GET', 'POST'])
def setup_page():
    if DB_POOL is None:
        return render_template('error.html',
                               message="Please setup the required ENV variables for DB URL")

    if is_db_initialized():
        return redirect(url_for('home'))

    settings = get_settings()
    setupForm = SetupForm()

    if request.method == 'POST':
        # Collect form data
        bank_name = request.form.get('bank_name')
        currency_name = request.form.get('currency_name')
        admin_password = request.form.get('admin_password')

        # Make a POST request to the /api/setup endpoint
        payload = json.dumps({
            'bank_name': bank_name,
            'currency_name': currency_name,
            'admin_password': admin_password
        }).encode('utf-8')
        setup_request = URLRequest(
            url_for('api_setup', _external=True),
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urlopen(setup_request, timeout=10) as response:
            response_body = json.loads(response.read().decode('utf-8'))

        # Check if the API call failed
        if response.status != 200:
            error_message = response_body.get('error', 'Unknown error occurred')
            return render_template('setup.html', error=error_message,
                                   settings=settings,
                                   is_admin='admin' in session and session['admin'],
                                   is_logged_in='wallet_name' in session, setupForm=setupForm)

        # Redirect to home if setup is successful
        return redirect(url_for('home'))

    return render_template('setup.html', settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session, setupForm=setupForm)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_db_initialized():
        return redirect(url_for('setup_page'))

    settings = get_settings()
    session.permanent = True
    requestWalletForm = RequestWalletForm()
    loginForm = LoginForm()

    if request.method == 'POST':
        wallet_name = request.form.get('wallet_name')
        password = request.form.get('password')

        user = get_user_by_wallet_name(wallet_name)

        if user and user.get('locked_until') and user['locked_until'] > datetime.now(UTC):
            remaining = int((user['locked_until'] - datetime.now(UTC)).total_seconds())
            return render_template('login.html', error=f"Wallet temporarily locked. Try again in {max(1, remaining)} seconds.", settings=settings, is_admin=False, is_logged_in=False, loginForm=loginForm, requestWalletForm=requestWalletForm)

        if user and check_password_hash(user['password'], password):
            execute_query("UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE wallet_name=%s", (wallet_name,), commit=True)
            session['wallet_name'] = wallet_name
            charge_monthly_tier_fee(wallet_name)

            # Update last login time
            execute_query(
                "UPDATE users SET last_login = %s WHERE wallet_name = %s",
                (datetime.now(UTC), wallet_name),
                commit=True
            )

            if wallet_name == 'admin':
                session['admin'] = True

            create_log("Login", f"User {wallet_name} logged in", "Admin")
            return redirect(url_for('home'))

        if user:
            attempts = int(user.get('failed_login_attempts') or 0) + 1
            if attempts >= 3:
                execute_query("UPDATE users SET failed_login_attempts=0, locked_until=%s WHERE wallet_name=%s",
                              (datetime.now(UTC) + timedelta(minutes=5), wallet_name), commit=True)
                create_log("Security Lockout", f"Wallet {wallet_name} was locked for 5 minutes after three failed login attempts", "Admin")
                error_message = "Wallet temporarily locked for 5 minutes after three failed login attempts."
            else:
                execute_query("UPDATE users SET failed_login_attempts=%s WHERE wallet_name=%s",
                              (attempts, wallet_name), commit=True)
                error_message = f"Invalid credentials. Failed attempt {attempts} of 3."
        else:
            error_message = "Invalid credentials"

        return render_template('login.html', error=error_message,
                               is_admin='admin' in session and session['admin'],
                               is_logged_in='wallet_name' in session, loginForm=loginForm,
                               requestWalletForm=requestWalletForm)

    return render_template('login.html', settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session, loginForm=loginForm,
                           requestWalletForm=requestWalletForm)


@app.route('/logout')
def logout():
    if 'wallet_name' in session:
        create_log("Logout", f"User {session['wallet_name']} logged out", "Admin")
        session.pop('wallet_name', None)
    session.pop('admin', None)
    return redirect(url_for('home'))


# Web UI routes
@app.route('/wallet/<wallet_name>')
def wallet_page(wallet_name):
    settings = get_settings()

    user = get_user_by_wallet_name(wallet_name)

    if not user:
        return render_template('error.html', message="Wallet not found")

    total_used = get_total_currency()

    freezeForm = FreezeForm()
    burnForm = BurnForm()
    resetForm = ResetForm()
    transferForm = TransferForm()
    resetPasswordForm = ResetPasswordForm()
    bankTransferForm = BankTransferForm()
    delAccountForm = DelAccountForm()

    profile = get_user_account_profile(wallet_name)

    return render_template('wallet.html', user=user, profile=profile, settings=settings, total_used=total_used,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session,
                           resetForm=resetForm, burnForm=burnForm, freezeForm=freezeForm, transferForm=transferForm,
                           resetPasswordForm=resetPasswordForm, bankTransferForm=bankTransferForm, delAccountForm=delAccountForm)



@app.route('/credit/apply')
@login_required
def credit_application():
    wallet_name = session['wallet_name']
    profile = get_user_account_profile(wallet_name)
    tier = execute_query_dict("SELECT * FROM account_tiers WHERE name=%s", (profile['account_tier'],))
    form = RequestForm()
    return render_template('credit_application.html', profile=profile, tier=tier[0] if tier else None,
                           form=form, settings=get_settings(), is_admin='admin' in session and session['admin'],
                           is_logged_in=True)


@app.route('/reports')
@login_required
def reports_page():
    wallet_name = session['wallet_name']
    settings = get_settings()
    start = request.args.get('start')
    end = request.args.get('end')
    params = [f"%{wallet_name}%"]
    query = "SELECT id, action, details, timestamp FROM logs WHERE private_level='Private' AND details ILIKE %s"
    if start:
        query += " AND timestamp >= %s"
        params.append(start)
    if end:
        query += " AND timestamp < (%s::date + INTERVAL '1 day')"
        params.append(end)
    query += " ORDER BY timestamp DESC LIMIT 1000"
    logs = execute_query_dict(query, tuple(params))
    profile = get_user_account_profile(wallet_name)
    return render_template('reports.html', logs=logs, profile=profile, settings=settings, start=start, end=end, is_admin='admin' in session and session['admin'], is_logged_in=True)


@app.route('/reports/print')
@login_required
def reports_print():
    wallet_name = session['wallet_name']
    settings = get_settings()
    logs = execute_query_dict("SELECT id, action, details, timestamp FROM logs WHERE private_level='Private' AND details ILIKE %s ORDER BY timestamp DESC LIMIT 1000", (f"%{wallet_name}%",))
    profile = get_user_account_profile(wallet_name)
    return render_template('report_print.html', logs=logs, profile=profile, settings=settings, generated_at=datetime.now(UTC), wallet_name=wallet_name)


@app.route('/admin/account-tiers')
@admin_required
def admin_account_tiers():
    settings = get_settings()
    tiers = execute_query_dict("SELECT * FROM account_tiers ORDER BY id")
    users = execute_query_dict("SELECT wallet_name, account_tier, credit_limit, credit_used FROM users WHERE wallet_name != 'admin' ORDER BY wallet_name")
    return render_template('admin_account_tiers.html', tiers=tiers, users=users, settings=settings, is_admin=True, is_logged_in=True)


@app.route('/admin/credit')
@admin_required
def admin_credit():
    settings = get_settings()
    requests = execute_query_dict("SELECT * FROM requests WHERE request_type='CreditLine' AND status='Pending' ORDER BY timestamp DESC")
    users = execute_query_dict("SELECT wallet_name, current_currency, account_tier, credit_limit, credit_used FROM users WHERE wallet_name != 'admin' ORDER BY wallet_name")
    return render_template('admin_credit.html', requests=requests, users=users, settings=settings, is_admin=True, is_logged_in=True)


@app.route('/leaderboard')
def leaderboard_page():
    settings = get_settings()

    if not settings['allow_leaderboard']:
        return render_template('error.html', message="Leaderboard is disabled")

    users = execute_query_dict(
        "SELECT wallet_name, current_currency FROM users WHERE wallet_name != 'admin' ORDER BY current_currency DESC"
    )

    return render_template('leaderboard.html', users=users, settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


@app.route('/logs')
def logs_page():
    settings = get_settings()

    if not settings['allow_public_logs']:
        return render_template('error.html', message="Public logs are disabled")

    logs = execute_query_dict(
        "SELECT action, details, timestamp FROM logs WHERE private_level = 'Global' ORDER BY timestamp DESC LIMIT 50"
    )

    return render_template('logs.html', logs=logs, settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


@app.route('/user/logs')
@login_required
def user_logs_page():
    wallet_name = session['wallet_name']
    logs = execute_query_dict(
        "SELECT action, details, timestamp FROM logs WHERE private_level = 'Private' AND details ILIKE %s ORDER BY timestamp DESC LIMIT 50",
        (f"%{wallet_name}%",)
    )

    settings = get_settings()
    refundForm = RefundForm()

    return render_template('user_logs.html', logs=logs, settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session,
                           refundForm=refundForm)


@app.route('/user/requests')
@login_required
def user_requests_page():
    """Page for users to view their request history"""
    wallet_name = session['wallet_name']
    requests = execute_query_dict(
        "SELECT * FROM requests WHERE wallet_name = %s ORDER BY timestamp DESC",
        (wallet_name,)
    )
    settings = get_settings()

    return render_template('user_requests.html', requests=requests, settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


@app.route('/admin/logs')
@admin_required
def admin_logs_page():
    logs = execute_query_dict(
        "SELECT action, details, timestamp FROM logs WHERE private_level = 'Admin' ORDER BY timestamp DESC LIMIT 50"
    )
    settings = get_settings()
    adminLogForm = AdminLogForm()

    return render_template('admin_logs.html', logs=logs, settings=settings,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           adminLogForm=adminLogForm)


@app.route('/admin/treasury')
@admin_required
def admin_treasury_page():
    sync_admin_wallet()

    settings = get_settings()
    total_used = get_total_currency()

    burnForm = BurnCurrencyForm()
    mintForm = MintCurrencyForm()

    return render_template('treasury.html', settings=settings,
                           total_used=total_used,
                           available=settings['maximum_currency'] - total_used,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           burnForm=burnForm, mintForm=mintForm)


@app.route('/admin/wallets')
@admin_required
def admin_wallets_page():
    sync_admin_wallet()

    users = execute_query_dict(
        "SELECT wallet_name, current_currency, is_frozen, created_at, last_login FROM users WHERE wallet_name != 'admin'"
    )
    settings = get_settings()
    createWalletForm = CreateWalletForm()

    return render_template('admin_wallets.html', users=users, settings=settings,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           createWalletForm=createWalletForm)


@app.route('/admin/wallet/<wallet_name>')
@admin_required
def admin_wallet_detail_page(wallet_name):
    settings = get_settings()

    user = get_user_by_wallet_name(wallet_name)

    if not user:
        return render_template('error.html', message="Wallet not found")

    requests = execute_query_dict(
        "SELECT * FROM requests WHERE wallet_name = %s AND status = 'Pending'",
        (wallet_name,)
    )
    freezeForm = FreezeForm()
    burnForm = BurnForm()
    resetForm = ResetForm()
    bankTransferForm = BankTransferForm()

    return render_template('admin_wallet_detail.html', user=user, requests=requests,
                           settings=settings, is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session,
                           bankTransferForm=bankTransferForm, freezeForm=freezeForm, resetForm=resetForm,
                           burnForm=burnForm)


@app.route('/admin/rules')
@admin_required
def admin_rules_page():
    settings = get_settings()
    rulesForm = RulesForm()

    return render_template('admin_rules.html', settings=settings,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           rulesForm=rulesForm)


@app.route('/admin/requests')
@admin_required
def admin_requests_page():
    """Admin page to view all pending requests"""
    requests = execute_query_dict(
        "SELECT * FROM requests WHERE status = 'Pending' ORDER BY timestamp DESC"
    )
    settings = get_settings()
    adminRequestsForm = AdminRequestsForm()

    return render_template('admin_requests.html', requests=requests, settings=settings,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           adminRequestsForm=adminRequestsForm)


@app.route('/admin/sql')
@admin_required
def admin_sql_page():
    """Admin page for SQL database explorer"""
    sync_admin_wallet()

    settings = get_settings()
    sqlQueryForm = SqlQueryForm()

    return render_template('admin_sql.html', settings=settings,
                           is_admin='admin' in session and session['admin'], is_logged_in='wallet_name' in session,
                           sqlQueryForm=sqlQueryForm)


@app.route('/server-health')
@admin_required
def server_health_page():
    """Public page showing server health metrics"""
    settings = get_settings()

    if not is_db_initialized():
        return render_template('error.html',
                               message="The DB is not initialised so the server health page is locked from rendering")

    return render_template('server_health.html', settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


@app.route('/requests')
@login_required
def requests_page():
    settings = get_settings()
    wallet_name = session['wallet_name']
    requestForm = RequestForm()

    # Get user's pending requests
    requests = execute_query_dict(
        "SELECT * FROM requests WHERE wallet_name = %s AND status = 'Pending'",
        (wallet_name,)
    )

    return render_template('requests.html', requests=requests, settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session,
                           requestForm=requestForm)


@app.route('/about')
def about():
    if DB_POOL is None:
        return render_template('error.html', message="Database pool is not initialized")

    settings = get_settings()
    if not settings:
        return render_template('error.html', message="Database tables are not initialized", settings=settings)

    return render_template('about.html', settings=settings,
                           is_admin='admin' in session and session['admin'],
                           is_logged_in='wallet_name' in session)


# Serve static files
@app.route('/static/<path:filename>')
@admin_required
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)


if __name__ == '__main__':
    try:
        logging.info("Database pool is initialized, starting the server...")
        logging.info("Checking database initialization...")
        if not init_db():
            logging.error("Oops! The DB init failed!! This means you have a issue with the database connection!")
            exit(1)
        logging.info("Rotating logs older than 30 days...")
        rotate_logs()  # Perform log rotation during startup
    except Exception as err:
        logging.error(f"Error during startup: {err}")
        logging.warning("Database pool is not initialized due to the error.")
    finally:
        logging.info("Server Started!")
        serve(app, host='0.0.0.0', port=int(os.environ.get('PORT', '5000')))
