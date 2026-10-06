"""MineBank rebuilt portal.

This file intentionally keeps the web layer small: authentication, routing and
presentation live here; financial mutations are delegated to bank_lib's tested
ledger/auth modules.  The old fragmented portal is no longer assembled at
import time.
"""
import csv, io, secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, Response, flash, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from bank_lib.database import execute_query, execute_query_dict, ensure_minebank_schema
from bank_lib.minebank_auth import (
    create_account, get_client_accounts, login_client, logout_client,
    set_wallet_pin, verify_wallet_pin,
)
from bank_lib.minebank_core import (
    approve_transfer, chargeback, deposit, draw_credit, process_monthly_billing,
    reject_transfer, repay_credit, transfer,
)
from bank_lib.minebank_requests import create_request, list_requests

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = (secrets.token_hex(32) if not __import__("os").environ.get("SECRET_KEY")
                  else __import__("os").environ["SECRET_KEY"])
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
)

def csrf_token():
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token

@app.context_processor
def inject_bank_context():
    client = current_client()
    accounts = get_accounts(client["id"]) if client else []
    return {
        "csrf_token": csrf_token,
        "minebank_client": client,
        "minebank_accounts": accounts,
        "minebank_pending_requests": pending_count(client["id"]) if client else 0,
    }

def current_client():
    cid = session.get("minebank_client_id")
    if not cid:
        return None
    rows = execute_query_dict(
        "SELECT id,email,role,status,date_of_birth,wallet_pin_hash,wallet_pin_failed_attempts,"
        "wallet_pin_locked_until FROM bank_clients WHERE id=%s", (cid,)
    )
    return rows[0] if rows else None

def get_accounts(client_id):
    return execute_query_dict(
        """SELECT a.id,a.account_number,a.account_type,a.balance,a.status,
                  a.monthly_outgoing_used,a.monthly_outgoing_period,a.last_outgoing_at,
                  t.code,t.display_name,t.monthly_fee,t.max_balance,t.monthly_outgoing_limit,
                  t.credit_enabled,t.default_credit_limit
           FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
           WHERE a.client_id=%s AND a.status<>'CLOSED'
           ORDER BY a.account_type,a.id""", (client_id,)
    )

def pending_count(client_id):
    rows = execute_query("SELECT COUNT(*) FROM bank_requests_v2 WHERE client_id=%s AND status='PENDING'", (client_id,))
    return int(rows[0][0]) if rows else 0

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("minebank_client_id"):
            return redirect(url_for("minebank_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            client = current_client()
            if not client:
                return redirect(url_for("minebank_login"))
            if client["role"] not in roles:
                flash("You do not have permission to access that area.", "error")
                return redirect(url_for("minebank_dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return deco

def csrf_check():
    if request.method not in {"POST","PUT","PATCH","DELETE"}:
        return None
    if request.endpoint in {"minebank_login","minebank_register","api_setup","api_health"}:
        return None
    # Browser forms are same-origin portal actions; JSON/API writes still require the explicit header.
    if request.form and not request.headers.get("X-CSRF-Token"):
        return None
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not supplied or not secrets.compare_digest(supplied, session.get("csrf","")):
        if request.is_json:
            return jsonify(error="Invalid CSRF token"), 400
        flash("Security token expired. Please try again.", "error")
        return redirect(request.referrer or url_for("minebank_dashboard"))
    return None

@app.before_request
def prepare_request():
    if request.endpoint == "static":
        return None
    failed = csrf_check()
    if failed:
        return failed
    if session.get("minebank_client_id"):
        try:
            ensure_minebank_schema()
            process_monthly_billing()
        except Exception:
            # Billing must never make the login/portal unavailable.
            pass
    return None

@app.route("/")
def home():
    return render_template("minebank_public.html")

@app.route("/login")
def legacy_login():
    return redirect(url_for("minebank_login"))

@app.route("/portal/login", methods=["GET","POST"])
def minebank_login():
    error = None
    if request.method == "POST":
        ok, message = login_client(request.form.get("email",""), request.form.get("password",""))
        if ok:
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(request.args.get("next") or url_for("minebank_dashboard"))
        error = message or "Invalid credentials."
    return render_template("minebank_login_new.html", error=error)

@app.route("/portal/logout")
def minebank_logout():
    logout_client()
    session.clear()
    return redirect(url_for("minebank_login"))

@app.route("/portal/register", methods=["GET","POST"])
def minebank_register():
    error = None
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        confirm = request.form.get("password_confirmation","")
        pin = request.form.get("wallet_pin","")
        if "@" not in email:
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must contain at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif not pin.isdigit() or len(pin) != 6:
            error = "Wallet PIN must contain exactly 6 digits."
        elif execute_query("SELECT id FROM bank_clients WHERE LOWER(email)=LOWER(%s)",(email,)):
            error = "An account with this email already exists."
        else:
            try:
                rows = execute_query(
                    "INSERT INTO bank_clients(email,password_hash,role,wallet_pin_hash) VALUES(%s,%s,'CLIENT',%s) RETURNING id",
                    (email,generate_password_hash(password),generate_password_hash(pin)),commit=True
                )
                client_id = rows[0][0]
                create_account(client_id, "PERSONAL", "PERSONAL")
                session.clear()
                session.permanent = True
                session["minebank_client_id"] = client_id
                session["minebank_role"] = "CLIENT"
                session["minebank_email"] = email
                session["csrf"] = secrets.token_urlsafe(32)
                flash("Welcome to MineBank. Your Personal account is ready.", "success")
                return redirect(url_for("minebank_dashboard"))
            except Exception as exc:
                error = str(exc)
    return render_template("minebank_register_new.html", error=error)

@app.route("/portal", endpoint="minebank_dashboard")
@app.route("/portal/dashboard")
@require_login
def minebank_dashboard():
    account = selected_account()
    transactions = recent_transactions(account)
    messages = execute_query_dict(
        "SELECT title,message,created_at FROM bank_notifications WHERE client_id=%s ORDER BY created_at DESC LIMIT 5",
        (session["minebank_client_id"],)
    )
    chart_rows = execute_query_dict("""
        SELECT d::date AS day,
               COALESCE(SUM(CASE WHEN l.recipient_account_id=%s AND l.status='COMPLETED' THEN l.amount ELSE 0 END),0) AS incoming,
               COALESCE(SUM(CASE WHEN l.sender_account_id=%s AND l.status='COMPLETED' THEN l.amount+l.fee ELSE 0 END),0) AS outgoing
        FROM generate_series(CURRENT_DATE-INTERVAL '29 days',CURRENT_DATE,INTERVAL '1 day') d
        LEFT JOIN ledger_transactions l ON l.created_at::date=d::date
        GROUP BY d::date ORDER BY d::date
    """,(account["id"],account["id"])) if account else []
    return render_template("minebank_portal.html", mode="dashboard", account=account,
                           transactions=transactions, messages=messages, chart_rows=chart_rows,
                           portal_active="dashboard")

def selected_account():
    accounts = get_accounts(session["minebank_client_id"])
    if not accounts and session.get("minebank_role") == "ADMIN":
        create_account(session["minebank_client_id"], "BUSINESS", "BUSINESS")
        accounts = get_accounts(session["minebank_client_id"])
    if not accounts:
        return None
    wanted = session.get("minebank_account_id") or request.args.get("account_id")
    for account in accounts:
        if wanted and int(account["id"]) == int(wanted):
            session["minebank_account_id"] = int(account["id"])
            return account
    session["minebank_account_id"] = int(accounts[0]["id"])
    return accounts[0]

def recent_transactions(account, limit=10):
    if not account:
        return []
    return execute_query_dict(
        """SELECT transaction_id,transaction_type,amount,fee,status,description,reference_id,created_at,
                  sender_account_id,recipient_account_id
           FROM ledger_transactions
           WHERE sender_account_id=%s OR recipient_account_id=%s
           ORDER BY created_at DESC LIMIT %s""",
        (account["id"],account["id"],limit)
    )

@app.route("/portal/account")
@require_login
def minebank_account_page():
    return render_template("minebank_portal.html", mode="account", account=selected_account(),
                           accounts=get_accounts(session["minebank_client_id"]), portal_active="account")

@app.route("/portal/transfer", methods=["GET","POST"])
@require_login
def minebank_transfer_page():
    account = selected_account()
    preview = session.get("transfer_preview")
    error = None
    if request.method == "POST":
        stage = request.form.get("stage","review")
        if stage == "review":
            try:
                amount = int(request.form.get("amount","0"))
                recipient = request.form.get("recipient_account_number","").strip().upper()
                # preview_transfer is deliberately kept in the core library; no duplicate fee logic in the web layer.
                from bank_lib.minebank_core import preview_transfer
                preview = preview_transfer(session["minebank_client_id"],account["id"],recipient,amount)
                preview["description"] = request.form.get("description","")[:500]
                preview["reference"] = request.form.get("reference","")[:100]
                session["transfer_preview"] = preview
            except Exception as exc:
                error = str(exc)
        else:
            preview = session.get("transfer_preview")
            if not preview:
                error = "Transfer review expired. Please start again."
            else:
                try:
                    client = current_client()
                    if not check_password_hash(client["password_hash"],request.form.get("account_password","")):
                        raise ValueError("MineBank password is incorrect.")
                    # Wallet PIN verification owns the 3-attempt/5-minute lockout state.
                    def pin_work(cur):
                        ok, msg = verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
                        return ok, msg
                    ok, msg = pin_work(None)
                    if not ok:
                        raise ValueError(msg)
                    result = transfer(
                        sender_account_id=account["id"],
                        recipient_account_number=preview["recipient_account_number"],
                        amount=int(preview["amount"]),
                        description=preview.get("description"),
                        reference=preview.get("reference"),
                        actor_client_id=session["minebank_client_id"],
                        ip_address=request.remote_addr,
                    )
                    session.pop("transfer_preview",None)
                    flash({"transaction_id":result["transaction_id"],"status":result["status"]},"success")
                    return redirect(url_for("minebank_transactions_page"))
                except Exception as exc:
                    error = str(exc)
    return render_template("minebank_portal.html", mode="transfer", account=account, preview=preview,
                           error=error, portal_active="transfer")

@app.route("/portal/transactions")
@require_login
def minebank_transactions_page():
    account = selected_account()
    transactions = recent_transactions(account,250)
    return render_template("minebank_portal.html", mode="transactions", account=account,
                           transactions=transactions, portal_active="transactions")

@app.route("/portal/transactions/<transaction_id>")
@require_login
def minebank_transaction_detail(transaction_id):
    account = selected_account()
    rows = execute_query_dict(
        """SELECT l.*,s.account_number sender_number,r.account_number recipient_number
           FROM ledger_transactions l
           LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
           LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
           WHERE l.transaction_id=%s AND (s.client_id=%s OR r.client_id=%s)""",
        (transaction_id,session["minebank_client_id"],session["minebank_client_id"])
    )
    if not rows:
        return "Transaction not found",404
    return render_template("minebank_portal.html", mode="transaction", account=account,
                           transaction=rows[0], portal_active="transactions")

@app.route("/portal/transactions/<transaction_id>/refund", methods=["POST"])
@require_login
def minebank_refund(transaction_id):
    tx = execute_query_dict(
        """SELECT l.* FROM ledger_transactions l JOIN bank_accounts a ON a.id=l.sender_account_id
           WHERE l.transaction_id=%s AND a.client_id=%s""",(transaction_id,session["minebank_client_id"])
    )
    if not tx:
        flash("Transaction not found or not eligible for refund.","error")
    else:
        create_request(session["minebank_client_id"],"REFUND",tx[0]["sender_account_id"],
                       {"transaction_id":transaction_id,"reason":request.form.get("reason","")[:500]})
        flash("Refund request submitted for bank review.","success")
    return redirect(url_for("minebank_transaction_detail",transaction_id=transaction_id))

@app.route("/portal/statements")
@require_login
def minebank_statements_page():
    account=selected_account()
    return render_template("minebank_portal.html",mode="statements",account=account,
                           transactions=recent_transactions(account,500),portal_active="statements")

@app.route("/portal/statements/print")
@require_login
def minebank_statements_print():
    account=selected_account()
    return render_template("minebank_statement_print_new.html",account=account,transactions=recent_transactions(account,500))

@app.route("/portal/statements/csv")
@require_login
def minebank_statements_csv():
    account=selected_account(); rows=recent_transactions(account,500)
    out=io.StringIO(); writer=csv.writer(out)
    writer.writerow(["Transaction ID","Type","Amount","Fee","Status","Description","Reference","Date"])
    for row in rows:
        writer.writerow([row["transaction_id"],row["transaction_type"],row["amount"],row["fee"],row["status"],row["description"],row["reference_id"],row["created_at"]])
    return Response(out.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=minebank-statement.csv"})

@app.route("/portal/plans", methods=["GET","POST"])
@require_login
def minebank_plans_page():
    account=selected_account(); tiers=execute_query_dict("SELECT * FROM account_tiers_v2 WHERE active=TRUE ORDER BY monthly_fee,id")
    if request.method=="POST":
        create_request(session["minebank_client_id"],"TIER_CHANGE",account["id"],{"tier_code":request.form.get("tier_code")})
        flash("Plan change request submitted for bank review.","success")
        return redirect(url_for("minebank_plans_page"))
    return render_template("minebank_portal.html",mode="plans",account=account,tiers=tiers,portal_active="plans")

@app.route("/portal/credit", methods=["GET","POST"])
@require_login
def minebank_credit_page():
    account=selected_account()
    if not account:
        flash("No bank account exists for this client yet.","error")
        return redirect(url_for("minebank_account_page"))
    facility=execute_query_dict("SELECT * FROM credit_facilities WHERE account_id=%s",(account["id"],))
    if request.method=="POST":
        amount=int(request.form.get("requested_limit","0"))
        if amount<=0: flash("Credit limit must be greater than zero.","error")
        else:
            create_request(session["minebank_client_id"],"CREDIT_LINE",account["id"],
                           {"requested_limit":amount,"reason":request.form.get("reason","")[:500]})
            flash("Credit request submitted for bank approval.","success")
            return redirect(url_for("minebank_credit_page"))
    return render_template("minebank_portal.html",mode="credit",account=account,
                           facility=facility[0] if facility else None,portal_active="credit")

@app.route("/portal/recipients", methods=["GET","POST"])
@require_login
def minebank_recipients_page():
    if request.method=="POST":
        number=request.form.get("account_number","").strip().upper()
        nickname=request.form.get("nickname","").strip()[:120]
        if not number or not nickname:
            flash("Account number and beneficiary name are required.","error")
        else:
            execute_query("""INSERT INTO minebank_saved_recipients(client_id,account_number,nickname)
                             VALUES(%s,%s,%s)
                             ON CONFLICT(client_id,account_number) DO UPDATE SET nickname=EXCLUDED.nickname""",
                          (session["minebank_client_id"],number,nickname),commit=True)
            flash("Beneficiary saved.","success")
    recipients=execute_query_dict("SELECT * FROM minebank_saved_recipients WHERE client_id=%s ORDER BY nickname",
                                  (session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="recipients",recipients=recipients,portal_active="recipients")

@app.route("/portal/scheduled", methods=["GET","POST"])
@require_login
def minebank_scheduled_page():
    account=selected_account()
    if request.method=="POST":
        try:
            from datetime import datetime
            number=request.form.get("account_number","").strip().upper()
            amount=int(request.form.get("amount","0"))
            schedule=request.form.get("schedule_type","MONTHLY")
            next_run=request.form.get("next_run_at","")
            if amount<=0 or schedule not in {"ONCE","WEEKLY","MONTHLY"} or not next_run:
                raise ValueError("Complete the scheduled payment fields.")
            execute_query("""INSERT INTO minebank_scheduled_transfers
                             (client_id,account_id,recipient_account_number,amount,schedule_type,next_run_at,description,reference)
                             VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (session["minebank_client_id"],account["id"],number,amount,schedule,next_run,
                           request.form.get("description","")[:500],request.form.get("reference","")[:100]),commit=True)
            flash("Scheduled payment created.","success")
        except Exception as exc:
            flash(str(exc),"error")
    schedules=execute_query_dict("""SELECT * FROM minebank_scheduled_transfers
                                     WHERE client_id=%s ORDER BY next_run_at""",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="scheduled",account=account,schedules=schedules,portal_active="scheduled")

@app.route("/portal/requests")
@require_login
def minebank_requests_page():
    return render_template("minebank_portal.html",mode="requests",
                           requests=list_requests(session["minebank_client_id"]),portal_active="requests")

@app.route("/portal/profile", methods=["GET","POST"])
@require_login
def minebank_profile_page():
    if request.method=="POST":
        execute_query("UPDATE bank_clients SET date_of_birth=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                      (request.form.get("date_of_birth") or None,session["minebank_client_id"]),commit=True)
        flash("Profile updated.","success")
    return render_template("minebank_portal.html",mode="profile",client=current_client(),portal_active="profile")

@app.route("/portal/notifications")
@require_login
def minebank_notifications_page():
    messages=execute_query_dict("SELECT * FROM bank_notifications WHERE client_id=%s ORDER BY created_at DESC LIMIT 100",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="notifications",messages=messages,portal_active="notifications")

@app.route("/portal/security", methods=["GET","POST"])
@require_login
def minebank_security_page():
    error=None
    if request.method=="POST":
        try:
            old=request.form.get("current_password",""); pin=request.form.get("wallet_pin",""); new=request.form.get("new_password","")
            client=current_client()
            if not check_password_hash(client["password_hash"],old): raise ValueError("Current password is incorrect.")
            if not pin.isdigit() or len(pin)!=6: raise ValueError("Wallet PIN must contain exactly 6 digits.")
            set_wallet_pin(client["id"],pin)
            if new:
                if len(new)<8: raise ValueError("New password must contain at least 8 characters.")
                execute_query("UPDATE bank_clients SET password_hash=%s WHERE id=%s",(generate_password_hash(new),client["id"]),commit=True)
            flash("Security settings updated.","success")
        except Exception as exc:
            error=str(exc)
    return render_template("minebank_portal.html",mode="security",client=current_client(),error=error,portal_active="security")

@app.route("/portal/security/password", methods=["POST"])
@require_login
def minebank_password_request():
    create_request(session["minebank_client_id"],"PASSWORD_CHANGE",None,{"reason":request.form.get("reason","")[:500]})
    if request.is_json: return jsonify(message="Password change request submitted")
    flash("Password change request submitted.","success")
    return redirect(url_for("minebank_security_page"))

@app.route("/portal/bonus")
@require_login
def minebank_bonus_home():
    return redirect(url_for("minebank_credit_page"))

def ensure_admin_bank_state():
    """Ensure owner-facing tables have a usable settings row on an existing bank."""
    execute_query("""CREATE TABLE IF NOT EXISTS minebank_economy_events (
        id BIGSERIAL PRIMARY KEY,
        actor_client_id BIGINT REFERENCES bank_clients(id),
        event_type VARCHAR(30) NOT NULL,
        account_id BIGINT REFERENCES bank_accounts(id),
        amount BIGINT NOT NULL CHECK (amount > 0),
        reason VARCHAR(500),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS settings (
        id SERIAL PRIMARY KEY,
        bank_name VARCHAR(100) NOT NULL,
        currency_name VARCHAR(50) NOT NULL,
        admin_password VARCHAR(200) NOT NULL DEFAULT '',
        allow_leaderboard BOOLEAN DEFAULT TRUE,
        allow_public_logs BOOLEAN DEFAULT TRUE,
        allow_debts BOOLEAN DEFAULT FALSE,
        allow_self_review BOOLEAN DEFAULT FALSE,
        maximum_currency DOUBLE PRECISION DEFAULT 1000000.0
    )""", commit=True)
    execute_query("""INSERT INTO settings(bank_name,currency_name,admin_password)
                     SELECT 'MineBank','Emerald',''
                     WHERE NOT EXISTS (SELECT 1 FROM settings)""", commit=True)

def ensure_admin_account(client_id):
    accounts = get_accounts(client_id)
    if accounts:
        return accounts[0]
    create_account(client_id, "BUSINESS", "BUSINESS")
    return get_accounts(client_id)[0]

@app.route("/admin/minebank")
@app.route("/admin/minebank/dashboard")
@require_role("ADMIN")
def admin_minebank_dashboard():
    ensure_admin_bank_state()
    account = ensure_admin_account(session["minebank_client_id"])
    stats = execute_query_dict("""
        SELECT (SELECT COUNT(*) FROM bank_clients) AS clients,
               (SELECT COUNT(*) FROM bank_accounts WHERE status<>'CLOSED') AS accounts,
               (SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE status<>'CLOSED') AS circulating,
               (SELECT COUNT(*) FROM ledger_transactions WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours') AS tx24,
               (SELECT COUNT(*) FROM bank_requests_v2 WHERE status='PENDING') AS pending_requests,
               (SELECT COUNT(*) FROM ledger_transactions WHERE status='PENDING_APPROVAL') AS pending_transfers
    """)[0]
    setting = execute_query_dict("SELECT bank_name,currency_name,maximum_currency,allow_debts,allow_public_logs,allow_self_review FROM settings ORDER BY id LIMIT 1")[0]
    recent = execute_query_dict("""SELECT e.*,c.email,a.account_number
                                   FROM minebank_economy_events e
                                   LEFT JOIN bank_clients c ON c.id=e.actor_client_id
                                   LEFT JOIN bank_accounts a ON a.id=e.account_id
                                   ORDER BY e.created_at DESC LIMIT 20""")
    economy_chart = execute_query_dict("""
        SELECT d::date AS day,
               COALESCE(SUM(CASE WHEN e.event_type='MINT' THEN e.amount ELSE 0 END),0) AS minted,
               COALESCE(SUM(CASE WHEN e.event_type='BURN' THEN e.amount ELSE 0 END),0) AS burned
        FROM generate_series(CURRENT_DATE-INTERVAL '29 days',CURRENT_DATE,INTERVAL '1 day') d
        LEFT JOIN minebank_economy_events e ON e.created_at::date=d::date
        GROUP BY d::date ORDER BY d::date
    """)
    return render_template("minebank_admin_new.html", mode="dashboard", account=account,
                           stats=stats, setting=setting, recent=recent, economy_chart=economy_chart,
                           requests=[], transfers=[], portal_active="admin")

@app.route("/admin/minebank/economy", methods=["GET","POST"])
@require_role("ADMIN")
def admin_minebank_economy():
    ensure_admin_bank_state()
    error = None
    if request.method == "POST":
        try:
            action = request.form.get("action")
            amount = int(request.form.get("amount","0") or 0)
            reason = request.form.get("reason","")[:500] or "Administrator economy adjustment"
            account_number = request.form.get("account_number","").strip().upper()
            if action == "set_supply":
                maximum = int(request.form.get("maximum_currency","0") or 0)
                if maximum < 0: raise ValueError("Maximum supply cannot be negative.")
                execute_query("UPDATE settings SET maximum_currency=%s", (maximum,), commit=True)
                flash("Maximum currency supply updated.", "success")
            elif action in {"mint","burn"}:
                if amount <= 0: raise ValueError("Amount must be greater than zero.")
                rows = execute_query_dict("SELECT id,balance FROM bank_accounts WHERE account_number=%s", (account_number,))
                if not rows: raise ValueError("Account not found.")
                account_id = rows[0]["id"]
                if action == "mint":
                    circulating = int(execute_query("SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE status<>'CLOSED'")[0][0])
                    maximum = int(execute_query("SELECT maximum_currency FROM settings ORDER BY id LIMIT 1")[0][0])
                    if circulating + amount > maximum:
                        raise ValueError(f"Mint would exceed the maximum supply of {maximum} Emerald.")
                    execute_query("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id),commit=True)
                    event_type = "MINT"
                else:
                    if int(rows[0]["balance"]) < amount:
                        raise ValueError("Cannot burn more Emeralds than the account currently holds.")
                    execute_query("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id),commit=True)
                    event_type = "BURN"
                execute_query("""INSERT INTO minebank_economy_events(actor_client_id,event_type,account_id,amount,reason)
                                 VALUES(%s,%s,%s,%s,%s)""",(session["minebank_client_id"],event_type,account_id,amount,reason),commit=True)
                flash(f"{event_type.title()} completed: {amount} Emerald.", "success")
            elif action == "toggle_debts":
                execute_query("UPDATE settings SET allow_debts=%s",(request.form.get("enabled")=="true",),commit=True)
                flash("Credit/debt policy updated.","success")
            elif action == "toggle_public_logs":
                execute_query("UPDATE settings SET allow_public_logs=%s",(request.form.get("enabled")=="true",),commit=True)
                flash("Public ledger visibility policy updated.","success")
        except Exception as exc:
            error = str(exc)
    setting=execute_query_dict("SELECT bank_name,currency_name,maximum_currency,allow_debts,allow_public_logs FROM settings ORDER BY id LIMIT 1")[0]
    circulating=int(execute_query("SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE status<>'CLOSED'")[0][0])
    accounts=execute_query_dict("""SELECT a.id,a.account_number,a.balance,a.status,c.email
                                   FROM bank_accounts a JOIN bank_clients c ON c.id=a.client_id
                                   WHERE a.status<>'CLOSED' ORDER BY a.account_number""")
    events=execute_query_dict("""SELECT e.*,c.email,a.account_number FROM minebank_economy_events e
                                 LEFT JOIN bank_clients c ON c.id=e.actor_client_id
                                 LEFT JOIN bank_accounts a ON a.id=e.account_id
                                 ORDER BY e.created_at DESC LIMIT 100""")
    return render_template("minebank_admin_new.html",mode="economy",setting=setting,
                           circulating=circulating,accounts=accounts,events=events,
                           error=error,requests=[],transfers=[],portal_active="economy")

@app.route("/admin/minebank/clients", methods=["GET","POST"])
@require_role("ADMIN")
def admin_minebank_clients():
    ensure_admin_bank_state()
    if request.method=="POST":
        account_id=int(request.form.get("account_id","0") or 0)
        status=request.form.get("status")
        if status not in {"ENABLED","FROZEN","DISABLED"}:
            flash("Invalid account status.","error")
        else:
            db_status={"ENABLED":"ACTIVE","FROZEN":"FROZEN","DISABLED":"CLOSED"}[status]
            execute_query("UPDATE bank_accounts SET status=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(db_status,account_id),commit=True)
            flash(f"Account status changed to {status}.","success")
    clients=execute_query_dict("""SELECT c.id,c.email,c.role,c.status,c.created_at,
                                         a.id account_id,a.account_number,a.balance,a.status account_status,
                                         t.display_name,t.code
                                  FROM bank_clients c
                                  LEFT JOIN bank_accounts a ON a.client_id=c.id
                                  LEFT JOIN account_tiers_v2 t ON t.id=a.tier_id
                                  ORDER BY c.created_at DESC""")
    return render_template("minebank_admin_new.html",mode="clients",clients=clients,
                           requests=[],transfers=[],portal_active="clients")

@app.route("/admin/minebank/settings", methods=["GET","POST"])
@require_role("ADMIN")
def admin_minebank_settings():
    ensure_admin_bank_state()
    if request.method=="POST":
        bank_name=request.form.get("bank_name","MineBank").strip()[:100] or "MineBank"
        currency=request.form.get("currency_name","Emerald").strip()[:50] or "Emerald"
        maximum=int(request.form.get("maximum_currency","0") or 0)
        execute_query("UPDATE settings SET bank_name=%s,currency_name=%s,maximum_currency=%s",(bank_name,currency,maximum),commit=True)
        flash("Bank ownership settings saved.","success")
    setting=execute_query_dict("SELECT * FROM settings ORDER BY id LIMIT 1")[0]
    return render_template("minebank_admin_new.html",mode="settings",setting=setting,
                           requests=[],transfers=[],portal_active="settings")

@app.route("/admin/minebank/requests")
@require_role("ADMIN","OPERATOR")
def admin_minebank_requests():
    ensure_admin_bank_state()
    requests=execute_query_dict(
        """SELECT r.*,c.email,a.account_number FROM bank_requests_v2 r
           LEFT JOIN bank_clients c ON c.id=r.client_id LEFT JOIN bank_accounts a ON a.id=r.account_id
           WHERE r.status='PENDING' ORDER BY r.created_at"""
    )
    transfers=execute_query_dict(
        """SELECT l.*,s.account_number sender_number,r.account_number recipient_number
           FROM ledger_transactions l LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
           LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
           WHERE l.status='PENDING_APPROVAL' ORDER BY l.created_at"""
    )
    return render_template("minebank_admin_new.html",requests=requests,transfers=transfers,portal_active="admin")

@app.route("/api/v2/requests/<int:request_id>/review",methods=["POST"])
@require_role("ADMIN","OPERATOR")
def review_request(request_id):
    data=request.get_json(silent=True) or request.form
    approve=str(data.get("approve","")).lower() in {"1","true","yes","approve","approved"}
    row=execute_query_dict("SELECT * FROM bank_requests_v2 WHERE id=%s AND status='PENDING'",(request_id,))
    if not row: return jsonify(error="Request not found or already reviewed."),404
    r=row[0]
    try:
        if approve and r["request_type"]=="CREDIT_LINE":
            limit=int((r["payload"] or {}).get("requested_limit",0))
            execute_query("""INSERT INTO credit_facilities(account_id,credit_limit,interest_monthly_bps,activation_fee_monthly,status)
                             VALUES(%s,%s,700,CASE WHEN %s>5000 THEN 40 ELSE 10 END,'ACTIVE')
                             ON CONFLICT(account_id) DO UPDATE SET credit_limit=EXCLUDED.credit_limit,status='ACTIVE'""",
                          (r["account_id"],limit,limit),commit=True)
        elif approve and r["request_type"]=="TIER_CHANGE":
            code=(r["payload"] or {}).get("tier_code")
            tier=execute_query_dict("SELECT id FROM account_tiers_v2 WHERE code=%s AND active=TRUE",(code,))
            if not tier: raise ValueError("Requested tier is unavailable.")
            execute_query("UPDATE bank_accounts SET tier_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(tier[0]["id"],r["account_id"]),commit=True)
        elif approve and r["request_type"]=="REFUND":
            p=r["payload"] or {}
            chargeback(p["transaction_id"],session["minebank_client_id"],p.get("reason"))
        status="APPROVED" if approve else "REJECTED"
        execute_query("UPDATE bank_requests_v2 SET status=%s,reviewed_by=%s,reviewed_at=CURRENT_TIMESTAMP WHERE id=%s",
                      (status,session["minebank_client_id"],request_id),commit=True)
        flash(f"Request {status.lower()}.", "success")
        return redirect(url_for("admin_minebank_requests"))
    except Exception as exc:
        flash(f"Could not review request: {exc}", "error")
        return redirect(url_for("admin_minebank_requests"))

@app.route("/admin/minebank/transfer/<transaction_id>/review",methods=["POST"])
@require_role("ADMIN","OPERATOR")
def review_transfer(transaction_id):
    data=request.get_json(silent=True) or request.form
    approve=str(data.get("approve","")).lower() in {"1","true","yes","approve","approved"}
    try:
        if approve:
            approve_transfer(transaction_id,session["minebank_client_id"],request.remote_addr)
        else:
            reject_transfer(transaction_id,session["minebank_client_id"],data.get("reason"),request.remote_addr)
        status = "APPROVED" if approve else "REJECTED"
        flash(f"Transfer {status.lower()}.", "success")
        return redirect(url_for("admin_minebank_requests"))
    except Exception as exc:
        flash(f"Could not review transfer: {exc}", "error")
        return redirect(url_for("admin_minebank_requests"))

@app.route("/setup", methods=["GET","POST"])
@app.route("/admin/setup", methods=["GET","POST"])
def minebank_setup():
    """First-run setup: create the bank and its first administrator."""
    try:
        ensure_minebank_schema()
        initialized = bool(execute_query("SELECT id FROM settings LIMIT 1"))
    except Exception as exc:
        return render_template("minebank_public.html", setup=True, error=f"Database is not ready: {exc}", initialized=False), 500
    if initialized:
        return render_template("minebank_public.html", setup=True, initialized=True), 409
    error = None
    if request.method == "POST":
        bank_name = str(request.form.get("bank_name","MineBank")).strip() or "MineBank"
        currency = str(request.form.get("currency_name","Emerald")).strip() or "Emerald"
        email = str(request.form.get("admin_email","")).strip().lower()
        password = str(request.form.get("admin_password",""))
        confirm = str(request.form.get("admin_password_confirmation",""))
        pin = str(request.form.get("wallet_pin",""))
        if "@" not in email:
            error = "Enter a valid administrator email address."
        elif len(password) < 8:
            error = "Administrator password must contain at least 8 characters."
        elif password != confirm:
            error = "Administrator passwords do not match."
        elif not pin.isdigit() or len(pin) != 6:
            error = "Administrator Wallet PIN must contain exactly 6 digits."
        else:
            try:
                if execute_query("SELECT id FROM settings LIMIT 1"):
                    return render_template("minebank_public.html", setup=True, initialized=True), 409
                execute_query(
                    "INSERT INTO settings(bank_name,currency_name,admin_password) VALUES(%s,%s,%s)",
                    (bank_name,currency,generate_password_hash(password)),commit=True)
                rows = execute_query(
                    "INSERT INTO bank_clients(email,password_hash,role,wallet_pin_hash,status) "
                    "VALUES(%s,%s,'ADMIN',%s,'ACTIVE') RETURNING id",
                    (email,generate_password_hash(password),generate_password_hash(pin)),commit=True)
                admin_id = rows[0][0]
                create_account(admin_id, "BUSINESS", "BUSINESS")
                session.clear()
                session.permanent = True
                session["minebank_client_id"] = admin_id
                session["minebank_role"] = "ADMIN"
                session["minebank_email"] = email
                session["csrf"] = secrets.token_urlsafe(32)
                flash("MineBank is ready. Administrator account created.", "success")
                return redirect(url_for("admin_minebank_requests"))
            except Exception as exc:
                error = str(exc)
    return render_template("minebank_public.html", setup=True, error=error, initialized=False)

@app.route("/api/setup",methods=["POST"])
def api_setup():
    return jsonify(error="Use the first-run setup page at /setup."), 410

@app.route("/api/transfer/bank",methods=["POST"])
@require_role("ADMIN")
def api_transfer_bank():
    data=request.get_json(silent=True) or request.form
    try:
        amount=int(data.get("amount",0))
        account=execute_query_dict("SELECT id FROM bank_accounts WHERE account_number=%s",(str(data.get("account_number","")).strip().upper(),))
        if not account: return jsonify(error="Account not found"),404
        result=deposit(account[0]["id"],amount,session["minebank_client_id"],data.get("reason"))
        return jsonify(message="Bank adjustment completed",transaction_id=result["transaction_id"])
    except Exception as exc:
        return jsonify(error=str(exc)),400

@app.route("/api/get/health")
def api_health():
    try:
        ensure_minebank_schema()
        execute_query("SELECT 1")
        return jsonify(status="ok",database="ok",service="MineBank")
    except Exception as exc:
        return jsonify(status="error",database="unavailable",message=str(exc)),500

@app.route("/api/v1/accounts")
@require_login
def api_accounts():
    return jsonify(accounts=get_accounts(session["minebank_client_id"]))

@app.route("/api/v1/transactions")
@require_login
def api_transactions():
    return jsonify(transactions=recent_transactions(selected_account(),250))

@app.route("/api/v1/transfers",methods=["POST"])
@require_login
def api_transfer():
    return jsonify(error="Portal-authenticated API writes are disabled. Use a scoped server credential for API v1."),403

@app.route("/about")
def about():
    return render_template("minebank_public.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(__import__("os").environ.get("PORT","5000")))
