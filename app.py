"""MineBank rebuilt portal.

This file intentionally keeps the web layer small: authentication, routing and
presentation live here; financial mutations are delegated to bank_lib's tested
ledger/auth modules.  The old fragmented portal is no longer assembled at
import time.
"""
import csv, io, secrets, hashlib, os
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, Response, flash, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from bank_lib.database import execute_query, execute_query_dict, ensure_minebank_schema, ensure_transaction_schema, ensure_message_schema, ensure_loan_schema
from bank_lib.credicheck import ensure_credicheck_schema, get_profile as get_credicheck_profile, assess as credicheck_assess, set_score as credicheck_set_score, set_product_access as credicheck_set_product_access, activate_dynamic_cashline, close_dynamic_cashline
from bank_lib.minebank_auth import (
    create_account, get_client_accounts, login_client, logout_client,
    set_wallet_pin, verify_wallet_pin,
)
from bank_lib.minebank_core import (
    approve_transfer, chargeback, deposit, withdraw, draw_credit,
    reject_transfer, reject_business_transfer, approve_business_transfer,
    repay_credit, transfer, cancel_transfer,
    accrue_daily_credit_interest, generate_cashline_statement, disburse_loan, repay_loan_installment,
)
from bank_lib.minebank_requests import create_request, list_requests, create_notification
from bank_lib.minebank_security import ensure_security_schema, validate_session, list_active_sessions, terminate_session, terminate_other_sessions, security_event

app = Flask(__name__, template_folder="templates", static_folder="static")
# Vercel may run multiple serverless workers. Flask's signed session cookie
# requires the same secret on every worker; generating a random key at import
# time silently invalidates sessions whenever traffic moves between workers.
_configured_secret = os.environ.get("SECRET_KEY", "").strip()
if not _configured_secret:
    # Prefer a stable deployment-specific fallback if SECRET_KEY was omitted.
    # The raw database URL is never exposed; only its SHA-256 digest is used.
    _database_secret = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
        or ""
    ).strip()
    _configured_secret = hashlib.sha256(
        ("MineBank session secret|" + _database_secret).encode("utf-8")
    ).hexdigest()
app.secret_key = _configured_secret
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
        "minebank_request_updates": request_update_count(client["id"]) if client else 0,
        "minebank_unread_messages": unread_message_count(client["id"]) if client else 0,
        "minebank_frozen_account": (
            next((a for a in accounts if a.get("id") == session.get("minebank_account_id") and a.get("status") == "FROZEN"),
                 next((a for a in accounts if a.get("status") == "FROZEN"), None))
            if client else None
        ),
    }

def unread_message_count(client_id):
    rows = execute_query("SELECT COUNT(*) FROM bank_notifications WHERE client_id=%s AND read_at IS NULL", (client_id,))
    return int(rows[0][0]) if rows else 0

def current_client():
    cached = getattr(__import__("flask").g, "_minebank_client", None)
    if cached is not None:
        return cached
    cid = session.get("minebank_client_id")
    if not cid:
        return None
    rows = execute_query_dict(
        "SELECT id,email,role,status,date_of_birth,first_name,last_name,phone,address,city,postal_code,country,occupation,password_hash,password_changed_at,last_login,"
        "wallet_pin_hash,wallet_pin_failed_attempts,wallet_pin_locked_until,admin_reauth_at FROM bank_clients WHERE id=%s", (cid,)
    )
    client = rows[0] if rows else None
    __import__("flask").g._minebank_client = client
    return client

def get_accounts(client_id):
    # Keep account/dashboard CashLine data compatible with older production schemas.
    ensure_transaction_schema()
    cache = getattr(__import__("flask").g, "_minebank_accounts", None)
    if cache is not None and cache[0] == client_id:
        return cache[1]
    accounts = execute_query_dict(
        """SELECT a.id,a.account_number,a.account_type,a.balance,a.status,
                  a.monthly_outgoing_used,a.monthly_outgoing_period,a.last_outgoing_at,a.freeze_type,
                  t.code,t.display_name,t.monthly_fee,t.max_balance,t.monthly_outgoing_limit,
                  t.daily_outgoing_limit,t.single_transfer_limit,t.credit_enabled,t.default_credit_limit,
                  cf.credit_limit AS facility_credit_limit,cf.status AS credit_status,
                  CASE WHEN cf.status='ACTIVE' THEN GREATEST(0, COALESCE(cf.credit_limit,0) - COALESCE((
                    SELECT SUM(CASE WHEN l.transaction_type='TRANSFER' AND l.transfer_kind='CASHLINE'
                                      AND l.status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL') THEN l.amount+l.fee
                                    WHEN l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE') AND l.status='COMPLETED' THEN l.amount
                                    WHEN l.transaction_type='CREDIT_REPAYMENT' AND l.status='COMPLETED' AND l.recipient_account_id=a.id THEN -l.amount
                                    ELSE 0 END)
                    FROM ledger_transactions l WHERE l.sender_account_id=a.id OR l.recipient_account_id=a.id
                  ),0)) ELSE 0 END AS available_credit
           FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
           LEFT JOIN credit_facilities cf ON cf.account_id=a.id
           WHERE a.status<>'CLOSED' AND (a.client_id=%s OR EXISTS (SELECT 1 FROM minebank_business_members bm WHERE bm.account_id=a.id AND bm.client_id=%s))
           ORDER BY a.account_type,a.id""", (client_id,client_id,)
    )
    __import__("flask").g._minebank_accounts = (client_id, accounts)
    return accounts

def request_update_count(client_id):
    rows = execute_query("SELECT COUNT(*) FROM bank_notifications WHERE client_id=%s AND notification_type='REQUEST_UPDATE' AND read_at IS NULL",
                         (client_id,))
    return int(rows[0][0]) if rows else 0

def pending_count(client_id):
    cache = getattr(__import__("flask").g, "_minebank_pending_requests", None)
    if cache is not None and cache[0] == client_id:
        return cache[1]
    rows = execute_query("SELECT COUNT(*) FROM bank_requests_v2 WHERE client_id=%s AND status='PENDING'", (client_id,))
    value = int(rows[0][0]) if rows else 0
    __import__("flask").g._minebank_pending_requests = (client_id, value)
    return value

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

    # Schema creation/migrations are deliberately not run on every request.
    # ensure_minebank_schema() is internally cached for the lifetime of a warm
    # serverless worker, while the login route initializes it when needed.
    if session.get("minebank_client_id"):
        # Session validation is security-critical, but updating the database on
        # every page/resource request was unnecessarily expensive. Validate at
        # most once per minute per browser session.
        now_ts = datetime.now(timezone.utc).timestamp()
        last_check = float(session.get("_session_validated_at", 0) or 0)
        if now_ts - last_check >= 60:
            try:
                if not validate_session():
                    return redirect(url_for("minebank_login", next=request.path))
                session["_session_validated_at"] = now_ts
            except Exception:
                return redirect(url_for("minebank_login", next=request.path))

    failed = csrf_check()
    if failed:
        return failed
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
        ok, message = login_client(request.form.get("email",""), request.form.get("password",""), request.form.get("captcha_answer"))
        if ok:
            session["csrf"] = secrets.token_urlsafe(32)
            if session.pop("minebank_password_expired",False):
                flash("Your password has expired. Please change it before continuing.","error")
                return redirect(url_for("minebank_security_page"))
            return redirect(request.args.get("next") or url_for("minebank_dashboard"))
        error = message or "Invalid credentials."
    return render_template("minebank_login_new.html", error=error, captcha_question=session.get("login_captcha_question"))

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
    accounts = get_accounts(session["minebank_client_id"])
    account = selected_account()
    transactions = recent_transactions(account)
    total_balance = sum(int(a.get("balance") or 0) for a in accounts)
    balance_breakdown = accounts
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
                           accounts=accounts, total_balance=total_balance, balance_breakdown=balance_breakdown,
                           transactions=transactions, messages=messages, chart_rows=chart_rows,
                           portal_active="dashboard")

def selected_account(account_id=None):
    accounts = get_accounts(session["minebank_client_id"])
    if not accounts and session.get("minebank_role") == "ADMIN":
        create_account(session["minebank_client_id"], "BUSINESS", "BUSINESS")
        accounts = get_accounts(session["minebank_client_id"])
    if not accounts:
        return None
    wanted = account_id or session.get("minebank_account_id") or request.args.get("account_id")
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
    return redirect(url_for("minebank_accounts_page"))

def evaluate_tier_eligibility(client, account, tier):
    """Evaluate MineBank's configured Personal rules and fixed Business rules."""
    config = tier.get("eligibility_config") or {}
    code = str(tier.get("code") or "").upper()
    reasons = []

    if account and account.get("account_type") == "BUSINESS":
        # Business accounts are always opened at Standard. Upgrades are
        # determined by balance and financial standing, never by age.
        if code == "BUSINESS":
            return True, "Standard Business account."
        balance = int(account.get("balance") or 0)
        debt_rows = execute_query(
            """SELECT COALESCE(SUM(CASE
                WHEN l.transaction_type='TRANSFER' AND l.transfer_kind='CASHLINE'
                     AND l.status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL') THEN l.amount+l.fee
                WHEN l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE') AND l.status='COMPLETED' THEN l.amount
                WHEN l.transaction_type='CREDIT_REPAYMENT' AND l.status='COMPLETED'
                     AND l.recipient_account_id=%s THEN -l.amount
                ELSE 0 END),0)
               FROM ledger_transactions l
               WHERE l.sender_account_id=%s OR l.recipient_account_id=%s""",
            (account["id"], account["id"], account["id"])
        )
        debt = max(0, int(debt_rows[0][0] or 0)) if debt_rows else 0

        if code == "BUSINESS_PRO":
            if balance < 1000:
                reasons.append("Minimum balance: 1,000 Emerald.")
            if debt > max(250, balance // 4):
                reasons.append("Good financial standing required: outstanding debt is too high.")
        elif code == "CORPORATE":
            if balance < 10000:
                reasons.append("Minimum balance: 10,000 Emerald.")
            if debt > max(1000, balance // 4):
                reasons.append("Good financial standing required: outstanding debt is too high.")
            # The financial checks determine eligibility; Admin approval is
            # a separate authorization step handled by the TIER_CHANGE request.
        else:
            reasons.append("This Business tier is not currently available.")
        if code == "CORPORATE" and not reasons:
            return True, "You qualify. Corporate accounts require Admin approval."
        return (False, "Not eligible: " + " ".join(reasons)) if reasons else (True, "You qualify for this plan.")

    minimum_age = int(config.get("minimum_age") or 0)
    if minimum_age:
        dob = client.get("date_of_birth")
        if not dob:
            reasons.append(f"Minimum age: {minimum_age}; date of birth is not set.")
        else:
            try:
                today = datetime.now(timezone.utc).date()
                birth = dob.date() if hasattr(dob, "date") else dob
                age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
                if age < minimum_age:
                    reasons.append(f"Minimum age: {minimum_age} (you are {age}).")
            except Exception:
                reasons.append(f"Minimum age: {minimum_age}; date of birth could not be verified.")
    minimum_balance = int(config.get("minimum_balance") or 0)
    if int(account.get("balance") or 0) < minimum_balance:
        reasons.append(f"Minimum balance: {minimum_balance} Emerald.")
    minimum_operations = int(config.get("minimum_operations") or 0)
    if minimum_operations:
        rows = execute_query(
            """SELECT COUNT(*) FROM ledger_transactions
               WHERE (sender_account_id=%s OR recipient_account_id=%s)
                 AND status='COMPLETED'
                 AND created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'""",
            (account["id"], account["id"])
        )
        operations = int(rows[0][0]) if rows else 0
        if operations < minimum_operations:
            reasons.append(f"Minimum activity: {minimum_operations} completed operations in the last 30 days (you have {operations}).")
    return (False, "Not eligible: " + " ".join(reasons)) if reasons else (True, "You qualify for this plan.")


@app.route("/portal/accounts", methods=["GET","POST"])
@require_login
def minebank_accounts_page():
    error=None
    if request.method=="POST":
        try:
            action=request.form.get("action","open_business")
            if action=="tier_change":
                account_id=int(request.form.get("account_id","0") or 0)
                tier_code=request.form.get("tier_code","").strip().upper()
                owned=execute_query_dict("SELECT id,account_type FROM bank_accounts WHERE id=%s AND client_id=%s AND status<>'CLOSED'",(account_id,session["minebank_client_id"]))
                if not owned:
                    raise ValueError("Account not found.")
                tier=execute_query_dict("SELECT code FROM account_tiers_v2 WHERE code=%s AND account_type=%s AND active=TRUE",(tier_code,owned[0]["account_type"]))
                if not tier:
                    raise ValueError("That tier is not available for this account.")
                create_request(session["minebank_client_id"],"TIER_CHANGE",account_id,{"tier_code":tier_code})
                flash("Account tier change submitted for bank review.","success")
                return redirect(url_for("minebank_accounts_page",account_id=account_id))
            # Business accounts are opened at Standard only. Upgrades are
            # requested from the account after the eligibility checks pass.
            tier="BUSINESS"
            legal=request.form.get("legal_name","").strip()[:200]
            trading=request.form.get("trading_name","").strip()[:200]
            if not legal:
                raise ValueError("Business legal name is required.")
            if not tier.startswith("BUSINESS"):
                raise ValueError("Only Business tiers can be opened from this page.")
            account_id,number=create_account(session["minebank_client_id"],"BUSINESS",tier)
            execute_query("""INSERT INTO minebank_business_profiles(account_id,legal_name,trading_name,address,contact_email)
                             VALUES(%s,%s,%s,%s,%s)""",
                          (account_id,legal,trading,request.form.get("address","")[:300],session.get("minebank_email","")),commit=True)
            flash(f"Business account {number} created.","success")
            return redirect(url_for("minebank_accounts_page"))
        except Exception as exc:
            error=str(exc)
    account=selected_account()
    plan_tiers=execute_query_dict("""SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,
                                               credit_enabled,default_credit_limit,eligibility_config
                                        FROM account_tiers_v2
                                        WHERE account_type=%s AND active=TRUE ORDER BY id""",
                                   (account["account_type"] if account else "PERSONAL",))
    for tier in plan_tiers:
        tier["is_current"] = bool(account and tier["code"] == account.get("tier_code"))
        tier["eligible"], tier["eligibility_reason"] = evaluate_tier_eligibility(
            current_client(), account, tier
        ) if account else (False, "No account selected.")
    business_tiers=execute_query_dict("SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,credit_enabled,default_credit_limit FROM account_tiers_v2 WHERE account_type='BUSINESS' AND active=TRUE ORDER BY id")
    return render_template("minebank_portal.html",mode="account",account=account,accounts=get_accounts(session["minebank_client_id"]),
                           plan_tiers=plan_tiers,business_tiers=business_tiers,account_error=error,portal_active="account")

@app.route("/portal/business/<int:account_id>/members",methods=["GET","POST"])
@require_login
def minebank_business_members(account_id):
    owner=execute_query_dict("SELECT id,account_number,account_type FROM bank_accounts WHERE id=%s AND client_id=%s AND account_type='BUSINESS'",
                             (account_id,session["minebank_client_id"]))
    if not owner:
        return "Business account not found",404
    error=None
    if request.method=="POST":
        try:
            email=request.form.get("email","").strip().lower()
            role=request.form.get("role","EMPLOYEE")
            if role not in {"ADMIN","FINANCE_MANAGER","EMPLOYEE","READ_ONLY","PAYROLL_MANAGER"}:
                raise ValueError("Invalid Business role.")
            user=execute_query_dict("SELECT id FROM bank_clients WHERE LOWER(email)=LOWER(%s) AND status<>'CLOSED'",(email,))
            if not user: raise ValueError("The employee must already have a MUOC customer account.")
            execute_query("""INSERT INTO minebank_business_members(account_id,client_id,role)
                             VALUES(%s,%s,%s)
                             ON CONFLICT(account_id,client_id) DO UPDATE SET role=EXCLUDED.role""",
                          (account_id,user[0]["id"],role),commit=True)
            flash("Business member access updated.","success")
        except Exception as exc:
            error=str(exc)
    members=execute_query_dict("""SELECT m.*,c.email FROM minebank_business_members m
                                  JOIN bank_clients c ON c.id=m.client_id
                                  WHERE m.account_id=%s ORDER BY m.role,c.email""",(account_id,))
    pending_payments=execute_query_dict("""SELECT b.transaction_id,b.risk_score,l.amount,l.description,c.email requested_by_email
                                             FROM minebank_business_payment_approvals b JOIN ledger_transactions l ON l.transaction_id=b.transaction_id
                                             JOIN bank_clients c ON c.id=b.requested_by WHERE b.account_id=%s AND b.status='PENDING' ORDER BY b.created_at""",(account_id,))
    return render_template("minebank_portal.html",mode="business_members",business_account=owner[0],
                           members=members,pending_payments=pending_payments,error=error,portal_active="account")

@app.route("/portal/business/<int:account_id>/payments/<transaction_id>/reject",methods=["POST"])
@require_login
def reject_business_payment(account_id,transaction_id):
    try:
        reject_business_transfer(transaction_id,session["minebank_client_id"],request.form.get("reason",""),request.remote_addr)
        flash("Business payment rejected and funds released.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_business_members",account_id=account_id))
@app.route("/portal/business/<int:account_id>/payments/<transaction_id>/approve",methods=["POST"])
@require_login
def approve_business_payment(account_id,transaction_id):
    owner=execute_query_dict("SELECT id FROM bank_accounts WHERE id=%s AND account_type='BUSINESS'",(account_id,))
    if not owner: return "Business account not found",404
    try:
        status=approve_business_transfer(transaction_id,session["minebank_client_id"],request.remote_addr)
        flash("Business payment approved." if status=="COMPLETED" else "Business payment approved and sent to bank review.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_business_members",account_id=account_id))
@app.route("/portal/transfer", methods=["GET","POST"])
@require_login
def minebank_transfer_page():
    account = selected_account(request.args.get("account_id") or request.form.get("account_id"))
    if not account:
        return redirect(url_for("minebank_accounts_page"))
    if account.get("status") in {"FROZEN", "CLOSED"}:
        flash("This account is suspended and cannot be used for transfers.", "error")
        return redirect(url_for("minebank_accounts_page", account_id=account["id"]))
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
                funding_source=request.form.get("funding_source","BALANCE").upper()
                preview = preview_transfer(session["minebank_client_id"],account["id"],recipient,amount,funding_source=funding_source)
                preview["description"] = request.form.get("description","")[:500]
                preview["reference"] = request.form.get("reference","")[:100]
                preview["causal"] = request.form.get("causal","").strip()[:500]
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
                        causal=preview.get("causal"),
                        funding_source=preview.get("funding_source","BALANCE"),
                        actor_client_id=session["minebank_client_id"],
                        ip_address=request.remote_addr,
                    )
                    session.pop("transfer_preview",None)
                    flash({"transaction_id":result["transaction_id"],"status":result["status"]},"success")
                    return redirect(url_for("minebank_transactions_page"))
                except Exception as exc:
                    error = str(exc)
    return render_template("minebank_portal.html", mode="transfer", account=account, accounts=get_accounts(session["minebank_client_id"]),
                           preview=preview, error=error, portal_active="transfer")

@app.route("/portal/transactions")
@require_login
def minebank_transactions_page():
    if not ensure_transaction_schema():
        return "MineBank database is temporarily unavailable. Please try again in a moment.", 503
    account=selected_account()
    if not account:
        flash("No bank account is available for this client yet.","error")
        return redirect(url_for("minebank_accounts_page"))
    q=request.args.get("q","").strip()
    status=request.args.get("status","").strip().upper()
    direction=request.args.get("direction","").strip().upper()
    category=request.args.get("category","").strip()
    date_from=request.args.get("date_from","").strip()
    date_to=request.args.get("date_to","").strip()
    min_amount=request.args.get("min_amount","").strip()
    max_amount=request.args.get("max_amount","").strip()
    params=[account["id"],account["id"]]
    where=["(l.sender_account_id=%s OR l.recipient_account_id=%s)"]
    if q:
        where.append("(l.transaction_id ILIKE %s OR COALESCE(l.description,'') ILIKE %s OR COALESCE(l.causal,'') ILIKE %s OR COALESCE(l.reference_id,'') ILIKE %s)")
        like=f"%{q}%"; params.extend([like,like,like,like])
    if status:
        where.append("l.status=%s"); params.append(status)
    if direction=="INCOMING":
        where.append("l.recipient_account_id=%s"); params.append(account["id"])
    elif direction=="OUTGOING":
        where.append("l.sender_account_id=%s"); params.append(account["id"])
    if category:
        where.append("EXISTS (SELECT 1 FROM minebank_transaction_categories f WHERE f.transaction_id=l.transaction_id AND f.account_id=%s AND f.category=%s)")
        params.extend([account["id"],category])
    if date_from:
        where.append("l.created_at::date>=%s"); params.append(date_from)
    if date_to:
        where.append("l.created_at::date<=%s"); params.append(date_to)
    if min_amount:
        where.append("l.amount>=%s"); params.append(int(min_amount))
    if max_amount:
        where.append("l.amount<=%s"); params.append(int(max_amount))
    # Keep category aggregation out of GROUP BY so this page remains compatible
    # with production ledgers where transaction_id is not declared UNIQUE.
    sql=("SELECT l.transaction_id,l.transaction_type,l.amount,l.fee,l.status,l.description,l.causal,l.reference_id,l.created_at,"
         "l.sender_account_id,l.recipient_account_id,"
         "COALESCE((SELECT string_agg(tc.category, ', ' ORDER BY tc.category) "
         "FROM minebank_transaction_categories tc "
         "WHERE tc.transaction_id=l.transaction_id AND tc.account_id=%s),'') AS categories "
         "FROM ledger_transactions l WHERE "+" AND ".join(where)+
         " ORDER BY l.created_at DESC LIMIT 500")
    query_params=tuple([account["id"]]+params)
    try:
        transactions=execute_query_dict(sql,query_params)
        categories=execute_query_dict(
            "SELECT DISTINCT category FROM minebank_transaction_categories WHERE account_id=%s ORDER BY category",
            (account["id"],)
        )
    except Exception as exc:
        # A legacy database may have the ledger columns but not the optional
        # category table. The banking register must still remain usable.
        print(f"MineBank transaction category compatibility warning: {type(exc).__name__}: {exc}")
        fallback=("SELECT transaction_id,transaction_type,amount,fee,status,description,"
                  "causal,reference_id,created_at,sender_account_id,recipient_account_id "
                  "FROM ledger_transactions l WHERE "+" AND ".join(where)+
                  " ORDER BY created_at DESC LIMIT 500")
        fallback_params=tuple(params)
        transactions=execute_query_dict(fallback,fallback_params)
        categories=[]
    default_categories=["Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"]
    return render_template("minebank_portal.html",mode="transactions",account=account,transactions=transactions,
                           categories=categories,default_categories=default_categories,portal_active="transactions")

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
    tx=rows[0]
    cat_rows=execute_query_dict("SELECT category FROM minebank_transaction_categories WHERE transaction_id=%s AND account_id=%s ORDER BY category",
                                (transaction_id,account["id"]))
    tx["categories"]=", ".join(x["category"] for x in cat_rows)
    default_categories=["Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"]
    return render_template("minebank_portal.html", mode="transaction", account=account,
                           transaction=tx,default_categories=default_categories,portal_active="transactions")

def _pdf_response(title, rows, filename, sections=None, customer=None, account=None, document_reference=None):
    """Canonical MineBank customer PDF: branded, identity-first, detailed, signed."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    buffer=io.BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=15*mm,leftMargin=15*mm,topMargin=34*mm,bottomMargin=22*mm,title=title,author="MineBank")
    styles=getSampleStyleSheet()
    body=ParagraphStyle("MineBody",parent=styles["BodyText"],fontName="Helvetica",fontSize=8.5,leading=11,textColor=colors.HexColor("#16324a"))
    label=ParagraphStyle("MineLabel",parent=body,fontName="Helvetica-Bold",fontSize=8.2,textColor=colors.HexColor("#315c7e"))
    small=ParagraphStyle("MineSmall",parent=body,fontSize=7.5,leading=9,textColor=colors.HexColor("#60778b"))
    heading=ParagraphStyle("MineTitle",parent=styles["Heading1"],fontName="Helvetica-Bold",fontSize=19,textColor=colors.HexColor("#173f65"))
    section=ParagraphStyle("MineSection",parent=styles["Heading3"],fontName="Helvetica-Bold",fontSize=10,textColor=colors.HexColor("#07549a"),spaceBefore=5,spaceAfter=6)
    def esc(v): return str(v if v not in (None,"") else "—").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br/>")
    def p(v,style=body): return Paragraph(esc(v),style)
    def table(data,widths,header=False):
        t=Table(data,colWidths=widths,repeatRows=1 if header else 0,hAlign="LEFT")
        bg=colors.HexColor("#dceaf4") if header else colors.HexColor("#edf2f5")
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.35,colors.HexColor("#aebbc5")),("BACKGROUND",(0,0),(-1,0),bg),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.append(t); story.append(Spacer(1,7))
    customer=customer or {}; account=account or {}
    story=[Paragraph(title,heading),Paragraph("The New Bank · Official customer document",small),Spacer(1,8)]
    identity=[("Customer name",customer.get("full_name") or customer.get("name")),("Customer email",customer.get("email")),("Customer ID",customer.get("id") or customer.get("client_id")),("Account number",account.get("account_number")),("Account type",account.get("account_type")),("Account tier",account.get("tier_name") or account.get("tier_code")),("Account status",account.get("status")),("Document reference",document_reference or filename.replace(".pdf",""))]
    story.append(Paragraph("Customer & Account",section)); table([[p(k,label),p(v)] for k,v in identity],[45*mm,125*mm])
    if sections:
        for h,rs in sections:
            story.append(Paragraph(h,section))
            if rs: table([[p(k,label),p(v)] for k,v in rs],[45*mm,125*mm])
            else: story.append(Paragraph("No records for this section.",body))
    else:
        story.append(Paragraph("Document details",section)); table([[p(k,label),p(v)] for k,v in rows],[45*mm,125*mm])
    story += [Spacer(1,10),Paragraph("Signature",section),Paragraph("______________________________________________",body),Spacer(1,3),Paragraph("Sign here if asked to do so",small),Spacer(1,9),Paragraph("Generated by MineBank Online. Retain this document for your records.",small)]
    def hf(canvas,doc):
        canvas.saveState(); canvas.setFillColor(colors.HexColor("#07549a")); canvas.setFont("Helvetica-Bold",15); canvas.drawString(15*mm,A4[1]-13*mm,"◆ MineBank")
        canvas.setFont("Helvetica",7); canvas.setFillColor(colors.HexColor("#60778b")); canvas.drawString(15*mm,A4[1]-18*mm,"The New Bank")
        canvas.setStrokeColor(colors.HexColor("#8aa9bf")); canvas.line(15*mm,A4[1]-21*mm,A4[0]-15*mm,A4[1]-21*mm)
        canvas.setStrokeColor(colors.HexColor("#d2dce4")); canvas.line(15*mm,15*mm,A4[0]-15*mm,15*mm)
        canvas.setFillColor(colors.HexColor("#647482")); canvas.setFont("Helvetica",7); canvas.drawString(15*mm,9*mm,"MineBank · The New Bank"); canvas.drawRightString(A4[0]-15*mm,9*mm,f"Page {doc.page}"); canvas.restoreState()
    doc.build(story,onFirstPage=hf,onLaterPages=hf); buffer.seek(0)
    return Response(buffer.getvalue(),mimetype="application/pdf",headers={"Content-Disposition":f'inline; filename="{filename}"'})

@app.route("/portal/transactions/<transaction_id>/receipt")
@require_login
def minebank_transaction_receipt(transaction_id):
    rows=execute_query_dict("""SELECT l.*,s.account_number sender_number,r.account_number recipient_number,
                                      sc.email sender_email,rc.email recipient_email
                               FROM ledger_transactions l
                               LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
                               LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
                               LEFT JOIN bank_clients sc ON sc.id=s.client_id
                               LEFT JOIN bank_clients rc ON rc.id=r.client_id
                               WHERE l.transaction_id=%s AND (s.client_id=%s OR r.client_id=%s)""",
                            (transaction_id,session["minebank_client_id"],session["minebank_client_id"]))
    if not rows: return "Transaction not found",404
    tx=rows[0]
    customer_rows=execute_query_dict("SELECT id,email,COALESCE(full_name,email) AS full_name FROM bank_clients WHERE id=%s",(session["minebank_client_id"],))
    preferred_account=tx["sender_account_id"] or tx["recipient_account_id"]
    account_rows=execute_query_dict("SELECT a.*,t.display_name AS tier_name FROM bank_accounts a LEFT JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE a.id=%s AND a.client_id=%s",(preferred_account,session["minebank_client_id"]))
    customer=customer_rows[0] if customer_rows else {}
    account=account_rows[0] if account_rows else {}
    return _pdf_response("Transaction Receipt",[
        ("Transaction ID",tx["transaction_id"]),("Date",tx["created_at"]),("Status",tx["status"]),
        ("Type",tx["transaction_type"]),("Sender",tx["sender_email"] or tx["sender_number"] or "Bank"),
        ("Sender account",tx["sender_number"]),("Recipient",tx["recipient_email"] or tx["recipient_number"] or "Bank"),
        ("Recipient account",tx["recipient_number"]),("Amount",f'{tx["amount"]} Emerald'),
        ("Fee",f'{tx["fee"]} Emerald'),("Total debited",f'{int(tx["amount"] or 0)+int(tx["fee"] or 0)} Emerald' if tx["sender_account_id"] else "—"),
        ("Description",tx["description"]),("Causal",tx["causal"]),("Reference",tx["reference_id"]),
        ("Funding source",tx["transfer_kind"] or "Balance")
    ],f"minebank-receipt-{transaction_id}.pdf",customer=customer,account=account,document_reference=transaction_id)

@app.route("/portal/transactions/<transaction_id>/category",methods=["POST"])
@require_login
def minebank_transaction_category(transaction_id):
    account=selected_account()
    values=[x.strip()[:60] for x in request.form.getlist("category") if x.strip()][:3]
    defaults={"Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"}
    custom=execute_query_dict("SELECT DISTINCT category FROM minebank_transaction_categories WHERE account_id=%s",(account["id"],))
    allowed=defaults|{x["category"] for x in custom}
    if not values or all(v in allowed for v in values):
        execute_query("DELETE FROM minebank_transaction_categories WHERE transaction_id=%s AND account_id=%s",(transaction_id,account["id"]),commit=True)
        for value in values:
            execute_query("INSERT INTO minebank_transaction_categories(transaction_id,account_id,category) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(transaction_id,account["id"],value),commit=True)
        flash("Transaction categories updated.","success")
    else:
        flash("One or more categories are not valid.","error")
    return redirect(url_for("minebank_transaction_detail",transaction_id=transaction_id))

@app.route("/portal/transactions/<transaction_id>/cancel",methods=["POST"])
@require_login
def minebank_cancel_transfer(transaction_id):
    try:
        cancel_transfer(transaction_id,session["minebank_client_id"],request.remote_addr)
        flash("Pending transfer cancelled and funds released.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_transaction_detail",transaction_id=transaction_id))

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
    # Statements and Transactions are one canonical register. Reuse the same
    # schema compatibility and query path instead of maintaining a second,
    # divergent implementation.
    if not ensure_transaction_schema():
        return "MineBank database is temporarily unavailable. Please try again in a moment.", 503
    return minebank_transactions_page()

@app.route("/portal/statements/print")
@require_login
def minebank_statements_print():
    account=selected_account()
    if int(account["balance"]) < 1:
        flash("1 Emerald is required for a printed statement.","error")
        return redirect(url_for("minebank_statements_page"))
    execute_query("UPDATE bank_accounts SET balance=balance-1,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(account["id"],),commit=True)
    execute_query("""INSERT INTO ledger_transactions
                     (transaction_id,transaction_type,amount,fee,currency,sender_account_id,status,description)
                     VALUES(%s,'PRINTED_STATEMENT_FEE',1,0,'Emerald',%s,'COMPLETED','Printed statement')""",
                  (f"MB-STMT-{secrets.token_hex(6)}",account["id"]),commit=True)
    transactions=recent_transactions(account,500)
    total_in=sum(int(t["amount"] or 0) for t in transactions if t["recipient_account_id"]==account["id"] and t["status"]=="COMPLETED")
    total_out=sum(int(t["amount"] or 0)+int(t["fee"] or 0) for t in transactions if t["sender_account_id"]==account["id"] and t["status"]=="COMPLETED")
    rows=[("Account number",account["account_number"]),("Account type",account["display_name"]),
          ("Opening/current balance",f'{account["balance"]} Emerald'),("Generated",datetime.now(timezone.utc)),
          ("Completed incoming",f'{total_in} Emerald'),("Completed outgoing",f'{total_out} Emerald'),
          ("Transaction count",len(transactions))]
    detail=[]
    for tx in transactions:
        direction="Incoming" if tx["recipient_account_id"]==account["id"] else "Outgoing"
        detail.append((f'{tx["created_at"]} · {tx["transaction_id"]}',
                       f'{direction} · {tx["transaction_type"]} · {tx["status"]} · {tx["amount"]} Emerald · Fee {tx["fee"]} · {tx["description"] or tx["causal"] or "No description"}'))
    return _pdf_response("Account Statement",rows,f"minebank-statement-{account['account_number']}.pdf",
                         sections=[("Statement overview",rows),("Transaction register",detail)])

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
    error=None
    if request.method=="POST":
        try:
            account_id=int(request.form.get("account_id","0") or 0)
            tier_code=request.form.get("tier_code","").strip().upper()
            owned=execute_query_dict(
                "SELECT id,account_type,status FROM bank_accounts WHERE id=%s AND client_id=%s AND status<>'CLOSED'",
                (account_id,session["minebank_client_id"])
            )
            if not owned:
                raise ValueError("Account not found.")
            account=selected_account(account_id)
            tier=execute_query_dict(
                "SELECT code FROM account_tiers_v2 WHERE code=%s AND account_type=%s AND active=TRUE",
                (tier_code,owned[0]["account_type"])
            )
            if not tier:
                raise ValueError("That tier is not available for this account.")
            eligible,reason=evaluate_tier_eligibility(current_client(),account,tier[0])
            if tier_code==account.get("tier_code"):
                raise ValueError("This is already your current plan.")
            if not eligible:
                raise ValueError(reason)
            create_request(session["minebank_client_id"],"TIER_CHANGE",account_id,{"tier_code":tier_code})
            flash("Account tier upgrade submitted for bank review.","success")
            return redirect(url_for("minebank_plans_page",account_id=account_id))
        except Exception as exc:
            error=str(exc)
    account=selected_account()
    plan_tiers=execute_query_dict("""SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,
                                             credit_enabled,default_credit_limit,eligibility_config
                                      FROM account_tiers_v2
                                      WHERE account_type=%s AND active=TRUE ORDER BY id""",
                                   (account["account_type"] if account else "PERSONAL",))
    for tier in plan_tiers:
        tier["is_current"]=bool(account and tier["code"]==account.get("tier_code"))
        tier["eligible"],tier["eligibility_reason"]=evaluate_tier_eligibility(
            current_client(),account,tier
        ) if account else (False,"No account selected.")
    return render_template("minebank_portal.html",mode="plans",account=account,
                           accounts=get_accounts(session["minebank_client_id"]),
                           tiers=plan_tiers,error=error,portal_active="plans")

def cashline_outstanding_for_app(account_id):
    from bank_lib.minebank_core import cashline_outstanding
    conn=__import__("bank_lib.database",fromlist=["get_db_connection"]).get_db_connection()
    if conn is None: return 0
    try:
        with conn.cursor() as cur: return cashline_outstanding(cur,account_id)
    finally: __import__("bank_lib.database",fromlist=["release_db_connection"]).release_db_connection(conn)


def loan_financial_profile(client_id):
    accounts=get_accounts(client_id)
    positive_balance=sum(max(0,int(a.get("balance") or 0)) for a in accounts)
    cashline_debt=0
    cashline_limit=0
    for a in accounts:
        try:
            cashline_debt += cashline_outstanding_for_app(a["id"])
        except Exception:
            pass
        if a.get("credit_status")=="ACTIVE":
            cashline_limit += int(a.get("facility_credit_limit") or 0)
    active_loan_rows=execute_query("SELECT COALESCE(SUM(principal),0) FROM minebank_loans WHERE client_id=%s AND status='ACTIVE'",(client_id,))
    active_loans=int(active_loan_rows[0][0] or 0) if active_loan_rows else 0
    net_position=max(0,positive_balance-cashline_debt-active_loans)
    maximum=(net_position*10//5000)*5000
    maximum=min(50000000,maximum)
    auto_budget=positive_balance >= 0
    return {
        "accounts":accounts,
        "balance":positive_balance,
        "cashline_debt":cashline_debt,
        "cashline_limit":cashline_limit,
        "active_loans":active_loans,
        "maximum":maximum,
        "auto_budget":auto_budget,
        "cashline_ratio":(cashline_debt/cashline_limit if cashline_limit else 0),
    }

def loan_terms(principal,term_days):
    annual_bps=2370 if principal<=100000 else 1830
    interest=(principal*annual_bps*term_days + 3650000)//3650000
    request_fee=100 if principal>=100000 else 50
    total_cost=principal+interest
    installment=total_cost//term_days
    return {"annual_bps":annual_bps,"interest":interest,"request_fee":request_fee,
            "total_cost":total_cost,"installment":installment}

@app.route("/portal/loans", methods=["GET","POST"])
@require_login
def minebank_loans_page():
    if not ensure_loan_schema():
        return "MineBank Loans are temporarily unavailable. Please try again in a moment.",503
    profile=loan_financial_profile(session["minebank_client_id"])
    error=None
    if request.method=="POST":
        action=request.form.get("action","request")
        try:
            if action=="repay":
                loan_id=int(request.form.get("loan_id","0"))
                source_id=int(request.form.get("source_account_id","0"))
                ok,msg=verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
                if not ok: raise ValueError(msg)
                result=repay_loan_installment(loan_id,source_id,session["minebank_client_id"])
                flash(f"Loan installment paid: {result['amount']} Emerald.","success")
                return redirect(url_for("minebank_loans_page"))
            if action!="request":
                raise ValueError("Invalid Loan operation.")
            amount=int(request.form.get("amount","0"))
            term=int(request.form.get("term_days","0"))
            source_id=int(request.form.get("source_account_id","0"))
            destination_id=int(request.form.get("destination_account_id","0"))
            approval_mode=request.form.get("approval_mode","BANK_REVIEW").upper()
            pin=request.form.get("wallet_pin","")
            if amount<10000 or amount>50000000:
                raise ValueError("Loan amount must be between 10,000 and 50,000,000 Emerald.")
            if profile["maximum"]<amount:
                raise ValueError(f"Based on your current financial position, your maximum requestable Loan is {profile['maximum'] or 'below the 10,000 Emerald minimum'} Emerald.")
            if term not in (12,24,48,96,192):
                raise ValueError("Select a valid repayment term.")
            owned_ids={int(a["id"]) for a in profile["accounts"]}
            if source_id not in owned_ids or destination_id not in owned_ids:
                raise ValueError("Select one of your own accounts.")
            if any(int(a["id"])==destination_id and a["status"]!="ACTIVE" for a in profile["accounts"]):
                raise ValueError("The destination account is not active.")
            if any(int(a["id"])==source_id and a["status"]!="ACTIVE" for a in profile["accounts"]):
                raise ValueError("The fee source account is not active.")
            if approval_mode=="AUTO":
                if amount>20000:
                    raise ValueError("Loans above 20,000 Emerald always require Bank authorisation.")
                if amount<10000 or (profile["cashline_limit"] and profile["cashline_debt"] >= profile["cashline_limit"]*0.40):
                    raise ValueError("This Loan does not qualify for automatic approval. Choose Bank review.")
                if profile["balance"] < int(amount*1.10):
                    raise ValueError("Automatic approval requires sufficient available balance to cover the Loan plus 10%.")
            else:
                approval_mode="BANK_REVIEW"
            ok,msg=verify_wallet_pin(session["minebank_client_id"],pin)
            if not ok: raise ValueError(msg)
            terms=loan_terms(amount,term)
            withdraw(source_id,terms["request_fee"],description=f"Loan request fee")
            loan_number="LN-"+secrets.token_hex(8).upper()
            next_due=(datetime.now(timezone.utc).date()+timedelta(days=1))
            rows=execute_query("""INSERT INTO minebank_loans
                (loan_number,client_id,account_id,destination_account_id,principal,term_days,annual_interest_bps,total_interest,total_cost,request_fee,installment_amount,next_due_date,status,approval_mode)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (loan_number,session["minebank_client_id"],destination_id,destination_id,amount,term,terms["annual_bps"],
                 terms["interest"],terms["total_cost"],terms["request_fee"],terms["installment"],next_due,
                 "PENDING","AUTO" if approval_mode=="AUTO" else "BANK_REVIEW"),commit=True)
            loan_id=rows[0][0]
            if approval_mode=="AUTO":
                result=disburse_loan(loan_id,session["minebank_client_id"])
                create_notification(session["minebank_client_id"],"LOAN_APPROVED","Loan automatically approved",
                                    f"Your {loan_number} Loan for {amount} Emerald has been approved and deposited into your selected account.",
                                    destination_id)
                flash(f"Loan approved automatically and deposited: {result['amount']} Emerald.","success")
            else:
                create_request(session["minebank_client_id"],"LOAN_REQUEST",destination_id,
                               {"loan_id":loan_id,"loan_number":loan_number,"amount":amount,"term_days":term,
                                "annual_interest_bps":terms["annual_bps"],"total_interest":terms["interest"],
                                "request_fee":terms["request_fee"],"destination_account_id":destination_id})
                flash("Loan request submitted for Bank review.","success")
            return redirect(url_for("minebank_loans_page"))
        except Exception as exc:
            error=str(exc)
    loans=execute_query_dict("SELECT l.*,a.account_number destination_number FROM minebank_loans l JOIN bank_accounts a ON a.id=l.destination_account_id WHERE l.client_id=%s ORDER BY l.requested_at DESC LIMIT 50",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="loans",loan_profile=profile,loan_error=error,loans=loans,
                           portal_active="loans")

@app.route("/portal/loans/<int:loan_id>/agreement")
@require_login
def minebank_loan_agreement(loan_id):
    if not ensure_loan_schema(): return "MineBank Loans are temporarily unavailable.",503
    rows=execute_query_dict("""SELECT l.*,a.account_number destination_number,t.display_name,
                                      c.email
                               FROM minebank_loans l
                               JOIN bank_accounts a ON a.id=l.destination_account_id
                               JOIN account_tiers_v2 t ON t.id=a.tier_id
                               JOIN bank_clients c ON c.id=l.client_id
                               WHERE l.id=%s AND l.client_id=%s""",(loan_id,session["minebank_client_id"]))
    if not rows: return "Loan not found",404
    loan=rows[0]
    start=loan["approved_at"].date() if loan.get("approved_at") else datetime.now(timezone.utc).date()
    schedule=[]
    for day in range(1,int(loan["term_days"])+1):
        amount=int(loan["installment_amount"])
        if day==int(loan["term_days"]):
            amount=int(loan["total_cost"])-int(loan["installment_amount"])*(day-1)
        schedule.append((f"Day {day}",f"{start+timedelta(days=day)} · {amount} Emerald"))
    return _pdf_response("Loan Agreement",[
        ("Loan number",loan["loan_number"]),("Customer",loan["email"]),("Destination account",loan["destination_number"]),
        ("Principal",f"{loan['principal']} Emerald"),("Term",f"{loan['term_days']} days"),
        ("Annual interest rate",f"{loan['annual_interest_bps']/100:.2f}%"),("Interest",f"{loan['total_interest']} Emerald"),
        ("Loan request fee",f"{loan['request_fee']} Emerald"),("Total repayment",f"{loan['total_cost']} Emerald"),
        ("Daily installment",f"{loan['installment_amount']} Emerald"),("Status",loan["status"])
    ],f"minebank-loan-agreement-{loan['loan_number']}.pdf",sections=[
        ("Loan summary",[
            ("Loan number",loan["loan_number"]),("Principal",f"{loan['principal']} Emerald"),
            ("Term",f"{loan['term_days']} days"),("Interest",f"{loan['total_interest']} Emerald"),
            ("Request fee",f"{loan['request_fee']} Emerald"),("Total repayment",f"{loan['total_cost']} Emerald"),
            ("Daily installment",f"{loan['installment_amount']} Emerald")
        ]),
        ("Repayment schedule",schedule)
    ])

@app.route("/portal/credit", methods=["GET","POST"])
@require_login
def minebank_credit_page():
    ensure_transaction_schema()
    account=selected_account()
    if not account:
        flash("No bank account exists for this client yet.","error")
        return redirect(url_for("minebank_account_page"))
    facility=execute_query_dict("SELECT * FROM credit_facilities WHERE account_id=%s",(account["id"],))
    if request.method=="POST":
        action=request.form.get("action","request")
        try:
            if action=="repay":
                source_id=int(request.form.get("source_account_id","0"))
                amount=int(request.form.get("amount","0"))
                result=repay_credit(account["id"],amount,source_id)
                flash(f'Credit repayment completed: {result["transaction_id"]}.',"success")
            elif action=="cancel":
                if facility and cashline_outstanding_for_app(account["id"])>0:
                    raise ValueError("Repay the outstanding CashLine balance before requesting cancellation.")
                create_request(session["minebank_client_id"],"CREDIT_CANCEL",account["id"],{"reason":request.form.get("reason","")[:500]})
                flash("Credit cancellation request submitted for bank review.","success")
            elif action=="request_review":
                amount=int(request.form.get("requested_limit","0"))
                maximum=int(account.get("default_credit_limit") or 0)
                if amount < 300 or (maximum and amount > maximum):
                    raise ValueError(f"CashLine request must be between 300 and {maximum} Emerald.")
                reason=request.form.get("reason","")[:500]
                session["cashline_request_preview"]={"requested_limit":amount,"reason":reason}
            elif action=="request_confirm":
                preview=session.get("cashline_request_preview")
                if not preview:
                    amount=int(request.form.get("requested_limit","0"))
                    maximum=int(account.get("default_credit_limit") or 0)
                    if amount < 300 or (maximum and amount > maximum):
                        raise ValueError(f"CashLine request must be between 300 and {maximum} Emerald.")
                    preview={"requested_limit":amount,"reason":request.form.get("reason","")[:500]}
                ok,msg=verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
                if not ok:
                    raise ValueError(msg)
                amount=int(preview["requested_limit"])
                maximum=int(account.get("default_credit_limit") or 0)
                if amount < 300 or (maximum and amount > maximum):
                    raise ValueError(f"CashLine request must be between 300 and {maximum} Emerald.")
                assessment=credicheck_assess(session["minebank_client_id"],account,"CASHLINE",amount)
                preview["credicheck_assessment"]=assessment["decision"]
                preview["credicheck_reason"]=assessment["reason"]
                create_request(session["minebank_client_id"],"CREDIT_LINE",account["id"],preview)
                session.pop("cashline_request_preview",None)
                flash("CashLine request submitted for bank review. CrediCheck assessment recorded.","success")
            else:
                raise ValueError("Invalid CashLine operation.")
            return redirect(url_for("minebank_credit_page"))
        except Exception as exc:
            flash(str(exc),"error")
    statements=execute_query_dict("""SELECT * FROM credit_statements WHERE account_id=%s ORDER BY period_end DESC LIMIT 12""",(account["id"],))
    outstanding=cashline_outstanding_for_app(account["id"])
    cashline_transactions=execute_query_dict("""SELECT l.transaction_id,l.transaction_type,l.amount,l.fee,l.status,l.description,l.created_at,
                                                       l.transfer_kind,s.account_number sender_number,r.account_number recipient_number
                                                FROM ledger_transactions l
                                                LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
                                                LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
                                                WHERE (l.sender_account_id=%s AND l.transfer_kind='CASHLINE')
                                                   OR (l.recipient_account_id=%s AND l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE','CREDIT_REPAYMENT'))
                                                ORDER BY l.created_at DESC LIMIT 100""",(account["id"],account["id"]))
"""MineBank rebuilt portal.

This file intentionally keeps the web layer small: authentication, routing and
presentation live here; financial mutations are delegated to bank_lib's tested
ledger/auth modules.  The old fragmented portal is no longer assembled at
import time.
"""
import csv, io, secrets, hashlib, os
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, Response, flash, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from bank_lib.database import execute_query, execute_query_dict, ensure_minebank_schema, ensure_transaction_schema, ensure_message_schema, ensure_loan_schema
from bank_lib.minebank_auth import (
    create_account, get_client_accounts, login_client, logout_client,
    set_wallet_pin, verify_wallet_pin,
)
from bank_lib.minebank_core import (
    approve_transfer, chargeback, deposit, withdraw, draw_credit,
    reject_transfer, reject_business_transfer, approve_business_transfer,
    repay_credit, transfer, cancel_transfer,
    accrue_daily_credit_interest, generate_cashline_statement, disburse_loan, repay_loan_installment,
)
from bank_lib.minebank_requests import create_request, list_requests, create_notification
from bank_lib.minebank_security import ensure_security_schema, validate_session, list_active_sessions, terminate_session, terminate_other_sessions, security_event

app = Flask(__name__, template_folder="templates", static_folder="static")
# Vercel may run multiple serverless workers. Flask's signed session cookie
# requires the same secret on every worker; generating a random key at import
# time silently invalidates sessions whenever traffic moves between workers.
_configured_secret = os.environ.get("SECRET_KEY", "").strip()
if not _configured_secret:
    # Prefer a stable deployment-specific fallback if SECRET_KEY was omitted.
    # The raw database URL is never exposed; only its SHA-256 digest is used.
    _database_secret = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
        or ""
    ).strip()
    _configured_secret = hashlib.sha256(
        ("MineBank session secret|" + _database_secret).encode("utf-8")
    ).hexdigest()
app.secret_key = _configured_secret
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
        "minebank_request_updates": request_update_count(client["id"]) if client else 0,
        "minebank_unread_messages": unread_message_count(client["id"]) if client else 0,
        "minebank_frozen_account": (
            next((a for a in accounts if a.get("id") == session.get("minebank_account_id") and a.get("status") == "FROZEN"),
                 next((a for a in accounts if a.get("status") == "FROZEN"), None))
            if client else None
        ),
    }

def unread_message_count(client_id):
    rows = execute_query("SELECT COUNT(*) FROM bank_notifications WHERE client_id=%s AND read_at IS NULL", (client_id,))
    return int(rows[0][0]) if rows else 0

def current_client():
    cached = getattr(__import__("flask").g, "_minebank_client", None)
    if cached is not None:
        return cached
    cid = session.get("minebank_client_id")
    if not cid:
        return None
    rows = execute_query_dict(
        "SELECT id,email,role,status,date_of_birth,first_name,last_name,phone,address,city,postal_code,country,occupation,password_hash,password_changed_at,last_login,"
        "wallet_pin_hash,wallet_pin_failed_attempts,wallet_pin_locked_until,admin_reauth_at FROM bank_clients WHERE id=%s", (cid,)
    )
    client = rows[0] if rows else None
    __import__("flask").g._minebank_client = client
    return client

def get_accounts(client_id):
    # Keep account/dashboard CashLine data compatible with older production schemas.
    ensure_transaction_schema()
    cache = getattr(__import__("flask").g, "_minebank_accounts", None)
    if cache is not None and cache[0] == client_id:
        return cache[1]
    accounts = execute_query_dict(
        """SELECT a.id,a.account_number,a.account_type,a.balance,a.status,
                  a.monthly_outgoing_used,a.monthly_outgoing_period,a.last_outgoing_at,a.freeze_type,
                  t.code,t.display_name,t.monthly_fee,t.max_balance,t.monthly_outgoing_limit,
                  t.daily_outgoing_limit,t.single_transfer_limit,t.credit_enabled,t.default_credit_limit,
                  cf.credit_limit AS facility_credit_limit,cf.status AS credit_status,
                  CASE WHEN cf.status='ACTIVE' THEN GREATEST(0, COALESCE(cf.credit_limit,0) - COALESCE((
                    SELECT SUM(CASE WHEN l.transaction_type='TRANSFER' AND l.transfer_kind='CASHLINE'
                                      AND l.status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL') THEN l.amount+l.fee
                                    WHEN l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE') AND l.status='COMPLETED' THEN l.amount
                                    WHEN l.transaction_type='CREDIT_REPAYMENT' AND l.status='COMPLETED' AND l.recipient_account_id=a.id THEN -l.amount
                                    ELSE 0 END)
                    FROM ledger_transactions l WHERE l.sender_account_id=a.id OR l.recipient_account_id=a.id
                  ),0)) ELSE 0 END AS available_credit
           FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
           LEFT JOIN credit_facilities cf ON cf.account_id=a.id
           WHERE a.status<>'CLOSED' AND (a.client_id=%s OR EXISTS (SELECT 1 FROM minebank_business_members bm WHERE bm.account_id=a.id AND bm.client_id=%s))
           ORDER BY a.account_type,a.id""", (client_id,client_id,)
    )
    __import__("flask").g._minebank_accounts = (client_id, accounts)
    return accounts

def request_update_count(client_id):
    rows = execute_query("SELECT COUNT(*) FROM bank_notifications WHERE client_id=%s AND notification_type='REQUEST_UPDATE' AND read_at IS NULL",
                         (client_id,))
    return int(rows[0][0]) if rows else 0

def pending_count(client_id):
    cache = getattr(__import__("flask").g, "_minebank_pending_requests", None)
    if cache is not None and cache[0] == client_id:
        return cache[1]
    rows = execute_query("SELECT COUNT(*) FROM bank_requests_v2 WHERE client_id=%s AND status='PENDING'", (client_id,))
    value = int(rows[0][0]) if rows else 0
    __import__("flask").g._minebank_pending_requests = (client_id, value)
    return value

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

    # Schema creation/migrations are deliberately not run on every request.
    # ensure_minebank_schema() is internally cached for the lifetime of a warm
    # serverless worker, while the login route initializes it when needed.
    if session.get("minebank_client_id"):
        # Session validation is security-critical, but updating the database on
        # every page/resource request was unnecessarily expensive. Validate at
        # most once per minute per browser session.
        now_ts = datetime.now(timezone.utc).timestamp()
        last_check = float(session.get("_session_validated_at", 0) or 0)
        if now_ts - last_check >= 60:
            try:
                if not validate_session():
                    return redirect(url_for("minebank_login", next=request.path))
                session["_session_validated_at"] = now_ts
            except Exception:
                return redirect(url_for("minebank_login", next=request.path))

    failed = csrf_check()
    if failed:
        return failed
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
        ok, message = login_client(request.form.get("email",""), request.form.get("password",""), request.form.get("captcha_answer"))
        if ok:
            session["csrf"] = secrets.token_urlsafe(32)
            if session.pop("minebank_password_expired",False):
                flash("Your password has expired. Please change it before continuing.","error")
                return redirect(url_for("minebank_security_page"))
            return redirect(request.args.get("next") or url_for("minebank_dashboard"))
        error = message or "Invalid credentials."
    return render_template("minebank_login_new.html", error=error, captcha_question=session.get("login_captcha_question"))

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
    accounts = get_accounts(session["minebank_client_id"])
    account = selected_account()
    transactions = recent_transactions(account)
    total_balance = sum(int(a.get("balance") or 0) for a in accounts)
    balance_breakdown = accounts
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
                           accounts=accounts, total_balance=total_balance, balance_breakdown=balance_breakdown,
                           transactions=transactions, messages=messages, chart_rows=chart_rows,
                           portal_active="dashboard")

def selected_account(account_id=None):
    accounts = get_accounts(session["minebank_client_id"])
    if not accounts and session.get("minebank_role") == "ADMIN":
        create_account(session["minebank_client_id"], "BUSINESS", "BUSINESS")
        accounts = get_accounts(session["minebank_client_id"])
    if not accounts:
        return None
    wanted = account_id or session.get("minebank_account_id") or request.args.get("account_id")
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
    return redirect(url_for("minebank_accounts_page"))

def evaluate_tier_eligibility(client, account, tier):
    """Evaluate MineBank's configured Personal rules and fixed Business rules."""
    config = tier.get("eligibility_config") or {}
    code = str(tier.get("code") or "").upper()
    reasons = []

    if account and account.get("account_type") == "BUSINESS":
        # Business accounts are always opened at Standard. Upgrades are
        # determined by balance and financial standing, never by age.
        if code == "BUSINESS":
            return True, "Standard Business account."
        balance = int(account.get("balance") or 0)
        debt_rows = execute_query(
            """SELECT COALESCE(SUM(CASE
                WHEN l.transaction_type='TRANSFER' AND l.transfer_kind='CASHLINE'
                     AND l.status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL') THEN l.amount+l.fee
                WHEN l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE') AND l.status='COMPLETED' THEN l.amount
                WHEN l.transaction_type='CREDIT_REPAYMENT' AND l.status='COMPLETED'
                     AND l.recipient_account_id=%s THEN -l.amount
                ELSE 0 END),0)
               FROM ledger_transactions l
               WHERE l.sender_account_id=%s OR l.recipient_account_id=%s""",
            (account["id"], account["id"], account["id"])
        )
        debt = max(0, int(debt_rows[0][0] or 0)) if debt_rows else 0

        if code == "BUSINESS_PRO":
            if balance < 1000:
                reasons.append("Minimum balance: 1,000 Emerald.")
            if debt > max(250, balance // 4):
                reasons.append("Good financial standing required: outstanding debt is too high.")
        elif code == "CORPORATE":
            if balance < 10000:
                reasons.append("Minimum balance: 10,000 Emerald.")
            if debt > max(1000, balance // 4):
                reasons.append("Good financial standing required: outstanding debt is too high.")
            # The financial checks determine eligibility; Admin approval is
            # a separate authorization step handled by the TIER_CHANGE request.
        else:
            reasons.append("This Business tier is not currently available.")
        if code == "CORPORATE" and not reasons:
            return True, "You qualify. Corporate accounts require Admin approval."
        return (False, "Not eligible: " + " ".join(reasons)) if reasons else (True, "You qualify for this plan.")

    minimum_age = int(config.get("minimum_age") or 0)
    if minimum_age:
        dob = client.get("date_of_birth")
        if not dob:
            reasons.append(f"Minimum age: {minimum_age}; date of birth is not set.")
        else:
            try:
                today = datetime.now(timezone.utc).date()
                birth = dob.date() if hasattr(dob, "date") else dob
                age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
                if age < minimum_age:
                    reasons.append(f"Minimum age: {minimum_age} (you are {age}).")
            except Exception:
                reasons.append(f"Minimum age: {minimum_age}; date of birth could not be verified.")
    minimum_balance = int(config.get("minimum_balance") or 0)
    if int(account.get("balance") or 0) < minimum_balance:
        reasons.append(f"Minimum balance: {minimum_balance} Emerald.")
    minimum_operations = int(config.get("minimum_operations") or 0)
    if minimum_operations:
        rows = execute_query(
            """SELECT COUNT(*) FROM ledger_transactions
               WHERE (sender_account_id=%s OR recipient_account_id=%s)
                 AND status='COMPLETED'
                 AND created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'""",
            (account["id"], account["id"])
        )
        operations = int(rows[0][0]) if rows else 0
        if operations < minimum_operations:
            reasons.append(f"Minimum activity: {minimum_operations} completed operations in the last 30 days (you have {operations}).")
    return (False, "Not eligible: " + " ".join(reasons)) if reasons else (True, "You qualify for this plan.")


@app.route("/portal/accounts", methods=["GET","POST"])
@require_login
def minebank_accounts_page():
    error=None
    if request.method=="POST":
        try:
            action=request.form.get("action","open_business")
            if action=="tier_change":
                account_id=int(request.form.get("account_id","0") or 0)
                tier_code=request.form.get("tier_code","").strip().upper()
                owned=execute_query_dict("SELECT id,account_type FROM bank_accounts WHERE id=%s AND client_id=%s AND status<>'CLOSED'",(account_id,session["minebank_client_id"]))
                if not owned:
                    raise ValueError("Account not found.")
                tier=execute_query_dict("SELECT code FROM account_tiers_v2 WHERE code=%s AND account_type=%s AND active=TRUE",(tier_code,owned[0]["account_type"]))
                if not tier:
                    raise ValueError("That tier is not available for this account.")
                create_request(session["minebank_client_id"],"TIER_CHANGE",account_id,{"tier_code":tier_code})
                flash("Account tier change submitted for bank review.","success")
                return redirect(url_for("minebank_accounts_page",account_id=account_id))
            # Business accounts are opened at Standard only. Upgrades are
            # requested from the account after the eligibility checks pass.
            tier="BUSINESS"
            legal=request.form.get("legal_name","").strip()[:200]
            trading=request.form.get("trading_name","").strip()[:200]
            if not legal:
                raise ValueError("Business legal name is required.")
            if not tier.startswith("BUSINESS"):
                raise ValueError("Only Business tiers can be opened from this page.")
            account_id,number=create_account(session["minebank_client_id"],"BUSINESS",tier)
            execute_query("""INSERT INTO minebank_business_profiles(account_id,legal_name,trading_name,address,contact_email)
                             VALUES(%s,%s,%s,%s,%s)""",
                          (account_id,legal,trading,request.form.get("address","")[:300],session.get("minebank_email","")),commit=True)
            flash(f"Business account {number} created.","success")
            return redirect(url_for("minebank_accounts_page"))
        except Exception as exc:
            error=str(exc)
    account=selected_account()
    plan_tiers=execute_query_dict("""SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,
                                               credit_enabled,default_credit_limit,eligibility_config
                                        FROM account_tiers_v2
                                        WHERE account_type=%s AND active=TRUE ORDER BY id""",
                                   (account["account_type"] if account else "PERSONAL",))
    for tier in plan_tiers:
        tier["is_current"] = bool(account and tier["code"] == account.get("tier_code"))
        tier["eligible"], tier["eligibility_reason"] = evaluate_tier_eligibility(
            current_client(), account, tier
        ) if account else (False, "No account selected.")
    business_tiers=execute_query_dict("SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,credit_enabled,default_credit_limit FROM account_tiers_v2 WHERE account_type='BUSINESS' AND active=TRUE ORDER BY id")
    return render_template("minebank_portal.html",mode="account",account=account,accounts=get_accounts(session["minebank_client_id"]),
                           plan_tiers=plan_tiers,business_tiers=business_tiers,account_error=error,portal_active="account")

@app.route("/portal/business/<int:account_id>/members",methods=["GET","POST"])
@require_login
def minebank_business_members(account_id):
    owner=execute_query_dict("SELECT id,account_number,account_type FROM bank_accounts WHERE id=%s AND client_id=%s AND account_type='BUSINESS'",
                             (account_id,session["minebank_client_id"]))
    if not owner:
        return "Business account not found",404
    error=None
    if request.method=="POST":
        try:
            email=request.form.get("email","").strip().lower()
            role=request.form.get("role","EMPLOYEE")
            if role not in {"ADMIN","FINANCE_MANAGER","EMPLOYEE","READ_ONLY","PAYROLL_MANAGER"}:
                raise ValueError("Invalid Business role.")
            user=execute_query_dict("SELECT id FROM bank_clients WHERE LOWER(email)=LOWER(%s) AND status<>'CLOSED'",(email,))
            if not user: raise ValueError("The employee must already have a MUOC customer account.")
            execute_query("""INSERT INTO minebank_business_members(account_id,client_id,role)
                             VALUES(%s,%s,%s)
                             ON CONFLICT(account_id,client_id) DO UPDATE SET role=EXCLUDED.role""",
                          (account_id,user[0]["id"],role),commit=True)
            flash("Business member access updated.","success")
        except Exception as exc:
            error=str(exc)
    members=execute_query_dict("""SELECT m.*,c.email FROM minebank_business_members m
                                  JOIN bank_clients c ON c.id=m.client_id
                                  WHERE m.account_id=%s ORDER BY m.role,c.email""",(account_id,))
    pending_payments=execute_query_dict("""SELECT b.transaction_id,b.risk_score,l.amount,l.description,c.email requested_by_email
                                             FROM minebank_business_payment_approvals b JOIN ledger_transactions l ON l.transaction_id=b.transaction_id
                                             JOIN bank_clients c ON c.id=b.requested_by WHERE b.account_id=%s AND b.status='PENDING' ORDER BY b.created_at""",(account_id,))
    return render_template("minebank_portal.html",mode="business_members",business_account=owner[0],
                           members=members,pending_payments=pending_payments,error=error,portal_active="account")

@app.route("/portal/business/<int:account_id>/payments/<transaction_id>/reject",methods=["POST"])
@require_login
def reject_business_payment(account_id,transaction_id):
    try:
        reject_business_transfer(transaction_id,session["minebank_client_id"],request.form.get("reason",""),request.remote_addr)
        flash("Business payment rejected and funds released.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_business_members",account_id=account_id))
@app.route("/portal/business/<int:account_id>/payments/<transaction_id>/approve",methods=["POST"])
@require_login
def approve_business_payment(account_id,transaction_id):
    owner=execute_query_dict("SELECT id FROM bank_accounts WHERE id=%s AND account_type='BUSINESS'",(account_id,))
    if not owner: return "Business account not found",404
    try:
        status=approve_business_transfer(transaction_id,session["minebank_client_id"],request.remote_addr)
        flash("Business payment approved." if status=="COMPLETED" else "Business payment approved and sent to bank review.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_business_members",account_id=account_id))
@app.route("/portal/transfer", methods=["GET","POST"])
@require_login
def minebank_transfer_page():
    account = selected_account(request.args.get("account_id") or request.form.get("account_id"))
    if not account:
        return redirect(url_for("minebank_accounts_page"))
    if account.get("status") in {"FROZEN", "CLOSED"}:
        flash("This account is suspended and cannot be used for transfers.", "error")
        return redirect(url_for("minebank_accounts_page", account_id=account["id"]))
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
                funding_source=request.form.get("funding_source","BALANCE").upper()
                preview = preview_transfer(session["minebank_client_id"],account["id"],recipient,amount,funding_source=funding_source)
                preview["description"] = request.form.get("description","")[:500]
                preview["reference"] = request.form.get("reference","")[:100]
                preview["causal"] = request.form.get("causal","").strip()[:500]
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
                        causal=preview.get("causal"),
                        funding_source=preview.get("funding_source","BALANCE"),
                        actor_client_id=session["minebank_client_id"],
                        ip_address=request.remote_addr,
                    )
                    session.pop("transfer_preview",None)
                    flash({"transaction_id":result["transaction_id"],"status":result["status"]},"success")
                    return redirect(url_for("minebank_transactions_page"))
                except Exception as exc:
                    error = str(exc)
    return render_template("minebank_portal.html", mode="transfer", account=account, accounts=get_accounts(session["minebank_client_id"]),
                           preview=preview, error=error, portal_active="transfer")

@app.route("/portal/transactions")
@require_login
def minebank_transactions_page():
    if not ensure_transaction_schema():
        return "MineBank database is temporarily unavailable. Please try again in a moment.", 503
    account=selected_account()
    if not account:
        flash("No bank account is available for this client yet.","error")
        return redirect(url_for("minebank_accounts_page"))
    q=request.args.get("q","").strip()
    status=request.args.get("status","").strip().upper()
    direction=request.args.get("direction","").strip().upper()
    category=request.args.get("category","").strip()
    date_from=request.args.get("date_from","").strip()
    date_to=request.args.get("date_to","").strip()
    min_amount=request.args.get("min_amount","").strip()
    max_amount=request.args.get("max_amount","").strip()
    params=[account["id"],account["id"]]
    where=["(l.sender_account_id=%s OR l.recipient_account_id=%s)"]
    if q:
        where.append("(l.transaction_id ILIKE %s OR COALESCE(l.description,'') ILIKE %s OR COALESCE(l.causal,'') ILIKE %s OR COALESCE(l.reference_id,'') ILIKE %s)")
        like=f"%{q}%"; params.extend([like,like,like,like])
    if status:
        where.append("l.status=%s"); params.append(status)
    if direction=="INCOMING":
        where.append("l.recipient_account_id=%s"); params.append(account["id"])
    elif direction=="OUTGOING":
        where.append("l.sender_account_id=%s"); params.append(account["id"])
    if category:
        where.append("EXISTS (SELECT 1 FROM minebank_transaction_categories f WHERE f.transaction_id=l.transaction_id AND f.account_id=%s AND f.category=%s)")
        params.extend([account["id"],category])
    if date_from:
        where.append("l.created_at::date>=%s"); params.append(date_from)
    if date_to:
        where.append("l.created_at::date<=%s"); params.append(date_to)
    if min_amount:
        where.append("l.amount>=%s"); params.append(int(min_amount))
    if max_amount:
        where.append("l.amount<=%s"); params.append(int(max_amount))
    # Keep category aggregation out of GROUP BY so this page remains compatible
    # with production ledgers where transaction_id is not declared UNIQUE.
    sql=("SELECT l.transaction_id,l.transaction_type,l.amount,l.fee,l.status,l.description,l.causal,l.reference_id,l.created_at,"
         "l.sender_account_id,l.recipient_account_id,"
         "COALESCE((SELECT string_agg(tc.category, ', ' ORDER BY tc.category) "
         "FROM minebank_transaction_categories tc "
         "WHERE tc.transaction_id=l.transaction_id AND tc.account_id=%s),'') AS categories "
         "FROM ledger_transactions l WHERE "+" AND ".join(where)+
         " ORDER BY l.created_at DESC LIMIT 500")
    query_params=tuple([account["id"]]+params)
    try:
        transactions=execute_query_dict(sql,query_params)
        categories=execute_query_dict(
            "SELECT DISTINCT category FROM minebank_transaction_categories WHERE account_id=%s ORDER BY category",
            (account["id"],)
        )
    except Exception as exc:
        # A legacy database may have the ledger columns but not the optional
        # category table. The banking register must still remain usable.
        print(f"MineBank transaction category compatibility warning: {type(exc).__name__}: {exc}")
        fallback=("SELECT transaction_id,transaction_type,amount,fee,status,description,"
                  "causal,reference_id,created_at,sender_account_id,recipient_account_id "
                  "FROM ledger_transactions l WHERE "+" AND ".join(where)+
                  " ORDER BY created_at DESC LIMIT 500")
        fallback_params=tuple(params)
        transactions=execute_query_dict(fallback,fallback_params)
        categories=[]
    default_categories=["Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"]
    return render_template("minebank_portal.html",mode="transactions",account=account,transactions=transactions,
                           categories=categories,default_categories=default_categories,portal_active="transactions")

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
    tx=rows[0]
    cat_rows=execute_query_dict("SELECT category FROM minebank_transaction_categories WHERE transaction_id=%s AND account_id=%s ORDER BY category",
                                (transaction_id,account["id"]))
    tx["categories"]=", ".join(x["category"] for x in cat_rows)
    default_categories=["Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"]
    return render_template("minebank_portal.html", mode="transaction", account=account,
                           transaction=tx,default_categories=default_categories,portal_active="transactions")

def _pdf_response(title, rows, filename, sections=None, customer=None, account=None, document_reference=None):
    """Canonical MineBank customer PDF: branded, identity-first, detailed, signed."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    buffer=io.BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=15*mm,leftMargin=15*mm,topMargin=34*mm,bottomMargin=22*mm,title=title,author="MineBank")
    styles=getSampleStyleSheet()
    body=ParagraphStyle("MineBody",parent=styles["BodyText"],fontName="Helvetica",fontSize=8.5,leading=11,textColor=colors.HexColor("#16324a"))
    label=ParagraphStyle("MineLabel",parent=body,fontName="Helvetica-Bold",fontSize=8.2,textColor=colors.HexColor("#315c7e"))
    small=ParagraphStyle("MineSmall",parent=body,fontSize=7.5,leading=9,textColor=colors.HexColor("#60778b"))
    heading=ParagraphStyle("MineTitle",parent=styles["Heading1"],fontName="Helvetica-Bold",fontSize=19,textColor=colors.HexColor("#173f65"))
    section=ParagraphStyle("MineSection",parent=styles["Heading3"],fontName="Helvetica-Bold",fontSize=10,textColor=colors.HexColor("#07549a"),spaceBefore=5,spaceAfter=6)
    def esc(v): return str(v if v not in (None,"") else "—").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br/>")
    def p(v,style=body): return Paragraph(esc(v),style)
    def table(data,widths,header=False):
        t=Table(data,colWidths=widths,repeatRows=1 if header else 0,hAlign="LEFT")
        bg=colors.HexColor("#dceaf4") if header else colors.HexColor("#edf2f5")
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.35,colors.HexColor("#aebbc5")),("BACKGROUND",(0,0),(-1,0),bg),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
        story.append(t); story.append(Spacer(1,7))
    customer=customer or {}; account=account or {}
    story=[Paragraph(title,heading),Paragraph("The New Bank · Official customer document",small),Spacer(1,8)]
    identity=[("Customer name",customer.get("full_name") or customer.get("name")),("Customer email",customer.get("email")),("Customer ID",customer.get("id") or customer.get("client_id")),("Account number",account.get("account_number")),("Account type",account.get("account_type")),("Account tier",account.get("tier_name") or account.get("tier_code")),("Account status",account.get("status")),("Document reference",document_reference or filename.replace(".pdf",""))]
    story.append(Paragraph("Customer & Account",section)); table([[p(k,label),p(v)] for k,v in identity],[45*mm,125*mm])
    if sections:
        for h,rs in sections:
            story.append(Paragraph(h,section))
            if rs: table([[p(k,label),p(v)] for k,v in rs],[45*mm,125*mm])
            else: story.append(Paragraph("No records for this section.",body))
    else:
        story.append(Paragraph("Document details",section)); table([[p(k,label),p(v)] for k,v in rows],[45*mm,125*mm])
    story += [Spacer(1,10),Paragraph("Signature",section),Paragraph("______________________________________________",body),Spacer(1,3),Paragraph("Sign here if asked to do so",small),Spacer(1,9),Paragraph("Generated by MineBank Online. Retain this document for your records.",small)]
    def hf(canvas,doc):
        canvas.saveState(); canvas.setFillColor(colors.HexColor("#07549a")); canvas.setFont("Helvetica-Bold",15); canvas.drawString(15*mm,A4[1]-13*mm,"◆ MineBank")
        canvas.setFont("Helvetica",7); canvas.setFillColor(colors.HexColor("#60778b")); canvas.drawString(15*mm,A4[1]-18*mm,"The New Bank")
        canvas.setStrokeColor(colors.HexColor("#8aa9bf")); canvas.line(15*mm,A4[1]-21*mm,A4[0]-15*mm,A4[1]-21*mm)
        canvas.setStrokeColor(colors.HexColor("#d2dce4")); canvas.line(15*mm,15*mm,A4[0]-15*mm,15*mm)
        canvas.setFillColor(colors.HexColor("#647482")); canvas.setFont("Helvetica",7); canvas.drawString(15*mm,9*mm,"MineBank · The New Bank"); canvas.drawRightString(A4[0]-15*mm,9*mm,f"Page {doc.page}"); canvas.restoreState()
    doc.build(story,onFirstPage=hf,onLaterPages=hf); buffer.seek(0)
    return Response(buffer.getvalue(),mimetype="application/pdf",headers={"Content-Disposition":f'inline; filename="{filename}"'})

@app.route("/portal/transactions/<transaction_id>/receipt")
@require_login
def minebank_transaction_receipt(transaction_id):
    rows=execute_query_dict("""SELECT l.*,s.account_number sender_number,r.account_number recipient_number,
                                      sc.email sender_email,rc.email recipient_email
                               FROM ledger_transactions l
                               LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
                               LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
                               LEFT JOIN bank_clients sc ON sc.id=s.client_id
                               LEFT JOIN bank_clients rc ON rc.id=r.client_id
                               WHERE l.transaction_id=%s AND (s.client_id=%s OR r.client_id=%s)""",
                            (transaction_id,session["minebank_client_id"],session["minebank_client_id"]))
    if not rows: return "Transaction not found",404
    tx=rows[0]
    customer_rows=execute_query_dict("SELECT id,email,COALESCE(full_name,email) AS full_name FROM bank_clients WHERE id=%s",(session["minebank_client_id"],))
    preferred_account=tx["sender_account_id"] or tx["recipient_account_id"]
    account_rows=execute_query_dict("SELECT a.*,t.display_name AS tier_name FROM bank_accounts a LEFT JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE a.id=%s AND a.client_id=%s",(preferred_account,session["minebank_client_id"]))
    customer=customer_rows[0] if customer_rows else {}
    account=account_rows[0] if account_rows else {}
    return _pdf_response("Transaction Receipt",[
        ("Transaction ID",tx["transaction_id"]),("Date",tx["created_at"]),("Status",tx["status"]),
        ("Type",tx["transaction_type"]),("Sender",tx["sender_email"] or tx["sender_number"] or "Bank"),
        ("Sender account",tx["sender_number"]),("Recipient",tx["recipient_email"] or tx["recipient_number"] or "Bank"),
        ("Recipient account",tx["recipient_number"]),("Amount",f'{tx["amount"]} Emerald'),
        ("Fee",f'{tx["fee"]} Emerald'),("Total debited",f'{int(tx["amount"] or 0)+int(tx["fee"] or 0)} Emerald' if tx["sender_account_id"] else "—"),
        ("Description",tx["description"]),("Causal",tx["causal"]),("Reference",tx["reference_id"]),
        ("Funding source",tx["transfer_kind"] or "Balance")
    ],f"minebank-receipt-{transaction_id}.pdf",customer=customer,account=account,document_reference=transaction_id)

@app.route("/portal/transactions/<transaction_id>/category",methods=["POST"])
@require_login
def minebank_transaction_category(transaction_id):
    account=selected_account()
    values=[x.strip()[:60] for x in request.form.getlist("category") if x.strip()][:3]
    defaults={"Groceries","Housing","Transport","Bills","Salary","Shopping","Entertainment","Travel","Health","Education","Fees","Transfers","CashLine","Other"}
    custom=execute_query_dict("SELECT DISTINCT category FROM minebank_transaction_categories WHERE account_id=%s",(account["id"],))
    allowed=defaults|{x["category"] for x in custom}
    if not values or all(v in allowed for v in values):
        execute_query("DELETE FROM minebank_transaction_categories WHERE transaction_id=%s AND account_id=%s",(transaction_id,account["id"]),commit=True)
        for value in values:
            execute_query("INSERT INTO minebank_transaction_categories(transaction_id,account_id,category) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(transaction_id,account["id"],value),commit=True)
        flash("Transaction categories updated.","success")
    else:
        flash("One or more categories are not valid.","error")
    return redirect(url_for("minebank_transaction_detail",transaction_id=transaction_id))

@app.route("/portal/transactions/<transaction_id>/cancel",methods=["POST"])
@require_login
def minebank_cancel_transfer(transaction_id):
    try:
        cancel_transfer(transaction_id,session["minebank_client_id"],request.remote_addr)
        flash("Pending transfer cancelled and funds released.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_transaction_detail",transaction_id=transaction_id))

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
    # Statements and Transactions are one canonical register. Reuse the same
    # schema compatibility and query path instead of maintaining a second,
    # divergent implementation.
    if not ensure_transaction_schema():
        return "MineBank database is temporarily unavailable. Please try again in a moment.", 503
    return minebank_transactions_page()

@app.route("/portal/statements/print")
@require_login
def minebank_statements_print():
    account=selected_account()
    if int(account["balance"]) < 1:
        flash("1 Emerald is required for a printed statement.","error")
        return redirect(url_for("minebank_statements_page"))
    execute_query("UPDATE bank_accounts SET balance=balance-1,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(account["id"],),commit=True)
    execute_query("""INSERT INTO ledger_transactions
                     (transaction_id,transaction_type,amount,fee,currency,sender_account_id,status,description)
                     VALUES(%s,'PRINTED_STATEMENT_FEE',1,0,'Emerald',%s,'COMPLETED','Printed statement')""",
                  (f"MB-STMT-{secrets.token_hex(6)}",account["id"]),commit=True)
    transactions=recent_transactions(account,500)
    total_in=sum(int(t["amount"] or 0) for t in transactions if t["recipient_account_id"]==account["id"] and t["status"]=="COMPLETED")
    total_out=sum(int(t["amount"] or 0)+int(t["fee"] or 0) for t in transactions if t["sender_account_id"]==account["id"] and t["status"]=="COMPLETED")
    rows=[("Account number",account["account_number"]),("Account type",account["display_name"]),
          ("Opening/current balance",f'{account["balance"]} Emerald'),("Generated",datetime.now(timezone.utc)),
          ("Completed incoming",f'{total_in} Emerald'),("Completed outgoing",f'{total_out} Emerald'),
          ("Transaction count",len(transactions))]
    detail=[]
    for tx in transactions:
        direction="Incoming" if tx["recipient_account_id"]==account["id"] else "Outgoing"
        detail.append((f'{tx["created_at"]} · {tx["transaction_id"]}',
                       f'{direction} · {tx["transaction_type"]} · {tx["status"]} · {tx["amount"]} Emerald · Fee {tx["fee"]} · {tx["description"] or tx["causal"] or "No description"}'))
    return _pdf_response("Account Statement",rows,f"minebank-statement-{account['account_number']}.pdf",
                         sections=[("Statement overview",rows),("Transaction register",detail)])

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
    error=None
    if request.method=="POST":
        try:
            account_id=int(request.form.get("account_id","0") or 0)
            tier_code=request.form.get("tier_code","").strip().upper()
            owned=execute_query_dict(
                "SELECT id,account_type,status FROM bank_accounts WHERE id=%s AND client_id=%s AND status<>'CLOSED'",
                (account_id,session["minebank_client_id"])
            )
            if not owned:
                raise ValueError("Account not found.")
            account=selected_account(account_id)
            tier=execute_query_dict(
                "SELECT code FROM account_tiers_v2 WHERE code=%s AND account_type=%s AND active=TRUE",
                (tier_code,owned[0]["account_type"])
            )
            if not tier:
                raise ValueError("That tier is not available for this account.")
            eligible,reason=evaluate_tier_eligibility(current_client(),account,tier[0])
            if tier_code==account.get("tier_code"):
                raise ValueError("This is already your current plan.")
            if not eligible:
                raise ValueError(reason)
            create_request(session["minebank_client_id"],"TIER_CHANGE",account_id,{"tier_code":tier_code})
            flash("Account tier upgrade submitted for bank review.","success")
            return redirect(url_for("minebank_plans_page",account_id=account_id))
        except Exception as exc:
            error=str(exc)
    account=selected_account()
    plan_tiers=execute_query_dict("""SELECT code,display_name,monthly_fee,opening_fee,monthly_outgoing_limit,
                                             credit_enabled,default_credit_limit,eligibility_config
                                      FROM account_tiers_v2
                                      WHERE account_type=%s AND active=TRUE ORDER BY id""",
                                   (account["account_type"] if account else "PERSONAL",))
    for tier in plan_tiers:
        tier["is_current"]=bool(account and tier["code"]==account.get("tier_code"))
        tier["eligible"],tier["eligibility_reason"]=evaluate_tier_eligibility(
            current_client(),account,tier
        ) if account else (False,"No account selected.")
    return render_template("minebank_portal.html",mode="plans",account=account,
                           accounts=get_accounts(session["minebank_client_id"]),
                           tiers=plan_tiers,error=error,portal_active="plans")

def cashline_outstanding_for_app(account_id):
    from bank_lib.minebank_core import cashline_outstanding
    conn=__import__("bank_lib.database",fromlist=["get_db_connection"]).get_db_connection()
    if conn is None: return 0
    try:
        with conn.cursor() as cur: return cashline_outstanding(cur,account_id)
    finally: __import__("bank_lib.database",fromlist=["release_db_connection"]).release_db_connection(conn)


def loan_financial_profile(client_id):
    accounts=get_accounts(client_id)
    positive_balance=sum(max(0,int(a.get("balance") or 0)) for a in accounts)
    cashline_debt=0
    cashline_limit=0
    for a in accounts:
        try:
            cashline_debt += cashline_outstanding_for_app(a["id"])
        except Exception:
            pass
        if a.get("credit_status")=="ACTIVE":
            cashline_limit += int(a.get("facility_credit_limit") or 0)
    active_loan_rows=execute_query("SELECT COALESCE(SUM(principal),0) FROM minebank_loans WHERE client_id=%s AND status='ACTIVE'",(client_id,))
    active_loans=int(active_loan_rows[0][0] or 0) if active_loan_rows else 0
    net_position=max(0,positive_balance-cashline_debt-active_loans)
    maximum=(net_position*10//5000)*5000
    maximum=min(50000000,maximum)
    auto_budget=positive_balance >= 0
    return {
        "accounts":accounts,
        "balance":positive_balance,
        "cashline_debt":cashline_debt,
        "cashline_limit":cashline_limit,
        "active_loans":active_loans,
        "maximum":maximum,
        "auto_budget":auto_budget,
        "cashline_ratio":(cashline_debt/cashline_limit if cashline_limit else 0),
    }

def loan_terms(principal,term_days):
    annual_bps=2370 if principal<=100000 else 1830
    interest=(principal*annual_bps*term_days + 3650000)//3650000
    request_fee=100 if principal>=100000 else 50
    total_cost=principal+interest
    installment=total_cost//term_days
    return {"annual_bps":annual_bps,"interest":interest,"request_fee":request_fee,
            "total_cost":total_cost,"installment":installment}

@app.route("/portal/loans", methods=["GET","POST"])
@require_login
def minebank_loans_page():
    if not ensure_loan_schema():
        return "MineBank Loans are temporarily unavailable. Please try again in a moment.",503
    profile=loan_financial_profile(session["minebank_client_id"])
    error=None
    if request.method=="POST":
        action=request.form.get("action","request")
        try:
            if action=="repay":
                loan_id=int(request.form.get("loan_id","0"))
                source_id=int(request.form.get("source_account_id","0"))
                ok,msg=verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
                if not ok: raise ValueError(msg)
                result=repay_loan_installment(loan_id,source_id,session["minebank_client_id"])
                flash(f"Loan installment paid: {result['amount']} Emerald.","success")
                return redirect(url_for("minebank_loans_page"))
            if action!="request":
                raise ValueError("Invalid Loan operation.")
            amount=int(request.form.get("amount","0"))
            term=int(request.form.get("term_days","0"))
            source_id=int(request.form.get("source_account_id","0"))
            destination_id=int(request.form.get("destination_account_id","0"))
            approval_mode=request.form.get("approval_mode","BANK_REVIEW").upper()
            pin=request.form.get("wallet_pin","")
            if amount<10000 or amount>50000000:
                raise ValueError("Loan amount must be between 10,000 and 50,000,000 Emerald.")
            if profile["maximum"]<amount:
                raise ValueError(f"Based on your current financial position, your maximum requestable Loan is {profile['maximum'] or 'below the 10,000 Emerald minimum'} Emerald.")
            if term not in (12,24,48,96,192):
                raise ValueError("Select a valid repayment term.")
            owned_ids={int(a["id"]) for a in profile["accounts"]}
            if source_id not in owned_ids or destination_id not in owned_ids:
                raise ValueError("Select one of your own accounts.")
            if any(int(a["id"])==destination_id and a["status"]!="ACTIVE" for a in profile["accounts"]):
                raise ValueError("The destination account is not active.")
            if any(int(a["id"])==source_id and a["status"]!="ACTIVE" for a in profile["accounts"]):
                raise ValueError("The fee source account is not active.")
            if approval_mode=="AUTO":
                if amount>20000:
                    raise ValueError("Loans above 20,000 Emerald always require Bank authorisation.")
                if amount<10000 or (profile["cashline_limit"] and profile["cashline_debt"] >= profile["cashline_limit"]*0.40):
                    raise ValueError("This Loan does not qualify for automatic approval. Choose Bank review.")
                if profile["balance"] < int(amount*1.10):
                    raise ValueError("Automatic approval requires sufficient available balance to cover the Loan plus 10%.")
            else:
                approval_mode="BANK_REVIEW"
            ok,msg=verify_wallet_pin(session["minebank_client_id"],pin)
            if not ok: raise ValueError(msg)
            terms=loan_terms(amount,term)
            withdraw(source_id,terms["request_fee"],description=f"Loan request fee")
            loan_number="LN-"+secrets.token_hex(8).upper()
            next_due=(datetime.now(timezone.utc).date()+timedelta(days=1))
            rows=execute_query("""INSERT INTO minebank_loans
                (loan_number,client_id,account_id,destination_account_id,principal,term_days,annual_interest_bps,total_interest,total_cost,request_fee,installment_amount,next_due_date,status,approval_mode)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (loan_number,session["minebank_client_id"],destination_id,destination_id,amount,term,terms["annual_bps"],
                 terms["interest"],terms["total_cost"],terms["request_fee"],terms["installment"],next_due,
                 "PENDING","AUTO" if approval_mode=="AUTO" else "BANK_REVIEW"),commit=True)
            loan_id=rows[0][0]
            if approval_mode=="AUTO":
                result=disburse_loan(loan_id,session["minebank_client_id"])
                create_notification(session["minebank_client_id"],"LOAN_APPROVED","Loan automatically approved",
                                    f"Your {loan_number} Loan for {amount} Emerald has been approved and deposited into your selected account.",
                                    destination_id)
                flash(f"Loan approved automatically and deposited: {result['amount']} Emerald.","success")
            else:
                create_request(session["minebank_client_id"],"LOAN_REQUEST",destination_id,
                               {"loan_id":loan_id,"loan_number":loan_number,"amount":amount,"term_days":term,
                                "annual_interest_bps":terms["annual_bps"],"total_interest":terms["interest"],
                                "request_fee":terms["request_fee"],"destination_account_id":destination_id})
                flash("Loan request submitted for Bank review.","success")
            return redirect(url_for("minebank_loans_page"))
        except Exception as exc:
            error=str(exc)
    loans=execute_query_dict("SELECT l.*,a.account_number destination_number FROM minebank_loans l JOIN bank_accounts a ON a.id=l.destination_account_id WHERE l.client_id=%s ORDER BY l.requested_at DESC LIMIT 50",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="loans",loan_profile=profile,loan_error=error,loans=loans,
                           portal_active="loans")

@app.route("/portal/loans/<int:loan_id>/agreement")
@require_login
def minebank_loan_agreement(loan_id):
    if not ensure_loan_schema(): return "MineBank Loans are temporarily unavailable.",503
    rows=execute_query_dict("""SELECT l.*,a.account_number destination_number,t.display_name,
                                      c.email
                               FROM minebank_loans l
                               JOIN bank_accounts a ON a.id=l.destination_account_id
                               JOIN account_tiers_v2 t ON t.id=a.tier_id
                               JOIN bank_clients c ON c.id=l.client_id
                               WHERE l.id=%s AND l.client_id=%s""",(loan_id,session["minebank_client_id"]))
    if not rows: return "Loan not found",404
    loan=rows[0]
    start=loan["approved_at"].date() if loan.get("approved_at") else datetime.now(timezone.utc).date()
    schedule=[]
    for day in range(1,int(loan["term_days"])+1):
        amount=int(loan["installment_amount"])
        if day==int(loan["term_days"]):
            amount=int(loan["total_cost"])-int(loan["installment_amount"])*(day-1)
        schedule.append((f"Day {day}",f"{start+timedelta(days=day)} · {amount} Emerald"))
    return _pdf_response("Loan Agreement",[
        ("Loan number",loan["loan_number"]),("Customer",loan["email"]),("Destination account",loan["destination_number"]),
        ("Principal",f"{loan['principal']} Emerald"),("Term",f"{loan['term_days']} days"),
        ("Annual interest rate",f"{loan['annual_interest_bps']/100:.2f}%"),("Interest",f"{loan['total_interest']} Emerald"),
        ("Loan request fee",f"{loan['request_fee']} Emerald"),("Total repayment",f"{loan['total_cost']} Emerald"),
        ("Daily installment",f"{loan['installment_amount']} Emerald"),("Status",loan["status"])
    ],f"minebank-loan-agreement-{loan['loan_number']}.pdf",sections=[
        ("Loan summary",[
            ("Loan number",loan["loan_number"]),("Principal",f"{loan['principal']} Emerald"),
            ("Term",f"{loan['term_days']} days"),("Interest",f"{loan['total_interest']} Emerald"),
            ("Request fee",f"{loan['request_fee']} Emerald"),("Total repayment",f"{loan['total_cost']} Emerald"),
            ("Daily installment",f"{loan['installment_amount']} Emerald")
        ]),
        ("Repayment schedule",schedule)
    ])

@app.route("/portal/credit", methods=["GET","POST"])
@require_login
def minebank_credit_page():
    ensure_transaction_schema()
    account=selected_account()
    if not account:
        flash("No bank account exists for this client yet.","error")
        return redirect(url_for("minebank_account_page"))
    facility=execute_query_dict("SELECT * FROM credit_facilities WHERE account_id=%s",(account["id"],))
    if request.method=="POST":
        action=request.form.get("action","request")
        try:
            if action=="repay":
                source_id=int(request.form.get("source_account_id","0"))
                amount=int(request.form.get("amount","0"))
                result=repay_credit(account["id"],amount,source_id)
                flash(f'Credit repayment completed: {result["transaction_id"]}.',"success")
            elif action=="cancel":
                if facility and cashline_outstanding_for_app(account["id"])>0:
                    raise ValueError("Repay the outstanding CashLine balance before requesting cancellation.")
                create_request(session["minebank_client_id"],"CREDIT_CANCEL",account["id"],{"reason":request.form.get("reason","")[:500]})
                flash("Credit cancellation request submitted for bank review.","success")
            elif action=="request_review":
                amount=int(request.form.get("requested_limit","0"))
                maximum=int(account.get("default_credit_limit") or 0)
                if amount < 300 or (maximum and amount > maximum):
                    raise ValueError(f"CashLine request must be between 300 and {maximum} Emerald.")
                reason=request.form.get("reason","")[:500]
                session["cashline_request_preview"]={"requested_limit":amount,"reason":reason}
            elif action=="request_confirm":
                preview=session.get("cashline_request_preview")
                if not preview:
                    raise ValueError("CashLine review expired. Please start again.")
                ok,msg=verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
                if not ok:
                    raise ValueError(msg)
                amount=int(preview["requested_limit"])
                maximum=int(account.get("default_credit_limit") or 0)
                if amount < 300 or (maximum and amount > maximum):
                    raise ValueError(f"CashLine request must be between 300 and {maximum} Emerald.")
                create_request(session["minebank_client_id"],"CREDIT_LINE",account["id"],preview)
                session.pop("cashline_request_preview",None)
                flash("CashLine request submitted for bank approval.","success")
            else:
                raise ValueError("Invalid CashLine operation.")
            return redirect(url_for("minebank_credit_page"))
        except Exception as exc:
            flash(str(exc),"error")
    statements=execute_query_dict("""SELECT * FROM credit_statements WHERE account_id=%s ORDER BY period_end DESC LIMIT 12""",(account["id"],))
    outstanding=cashline_outstanding_for_app(account["id"])
    cashline_transactions=execute_query_dict("""SELECT l.transaction_id,l.transaction_type,l.amount,l.fee,l.status,l.description,l.created_at,
                                                       l.transfer_kind,s.account_number sender_number,r.account_number recipient_number
                                                FROM ledger_transactions l
                                                LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
                                                LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
                                                WHERE (l.sender_account_id=%s AND l.transfer_kind='CASHLINE')
                                                   OR (l.recipient_account_id=%s AND l.transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE','CREDIT_REPAYMENT'))
                                                ORDER BY l.created_at DESC LIMIT 100""",(account["id"],account["id"]))
    dynamic_cashline=execute_query_dict("SELECT * FROM dynamic_cashlines WHERE account_id=%s",(account["id"],))
    dynamic_cashline=dynamic_cashline[0] if dynamic_cashline else None
    return render_template("minebank_portal.html",mode="credit",account=account,
                           facility=facility[0] if facility else None,statements=statements,
                           cashline_outstanding=outstanding,cashline_transactions=cashline_transactions,dynamic_cashline=dynamic_cashline,
                           repayment_sources=[a for a in get_accounts(session["minebank_client_id"]) if int(a["id"])!=int(account["id"])],
                           portal_active="credit")

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
            import json
            schedule=request.form.get("schedule_type","MONTHLY")
            next_run=request.form.get("next_run_at","")
            if amount<=0 or schedule not in {"ONCE","DAILY","WEEKLY","MONTHLY","CUSTOM"} or not next_run:
                raise ValueError("Complete the scheduled payment fields.")
            interval_days=max(1,int(request.form.get("interval_days","1") or 1))
            recurrence=json.dumps({"interval_days":interval_days})
            recipient=execute_query_dict("SELECT id FROM bank_accounts WHERE account_number=%s AND status<>'CLOSED'",(number,))
            if not recipient:
                raise ValueError("Recipient account not found.")
            if int(account["balance"]) < 2:
                raise ValueError("2 Emerald are required to set up a scheduled payment.")
            execute_query("UPDATE bank_accounts SET balance=balance-2,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(account["id"],),commit=True)
            execute_query("""INSERT INTO ledger_transactions
                             (transaction_id,transaction_type,amount,fee,currency,sender_account_id,status,description)
                             VALUES(%s,'SCHEDULED_PAYMENT_SETUP',2,0,'Emerald',%s,'COMPLETED','Scheduled payment setup fee')""",
                          (f"MB-SCHED-{secrets.token_hex(6)}",account["id"]),commit=True)
            execute_query("""INSERT INTO minebank_scheduled_transfers
                             (client_id,account_id,recipient_account_number,amount,schedule_type,next_run_at,end_at,description,reference,recurrence_config)
                             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                          (session["minebank_client_id"],account["id"],number,amount,schedule,next_run,
                           request.form.get("end_at") or None,request.form.get("description","")[:500],
                           request.form.get("reference","")[:100],recurrence),commit=True)
            flash("Scheduled payment created. Setup fee: 2 Emerald.","success")
        except Exception as exc:
            flash(str(exc),"error")
    schedules=execute_query_dict("""SELECT * FROM minebank_scheduled_transfers
                                     WHERE client_id=%s ORDER BY next_run_at""",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="scheduled",account=account,schedules=schedules,portal_active="scheduled")

@app.route("/api/cron/minebank",methods=["GET","POST"])
def minebank_cron():
    expected=__import__("os").environ.get("MUOC_CRON_TOKEN") or __import__("os").environ.get("CRON_SECRET")
    supplied=request.headers.get("Authorization","")
    if not expected or supplied != "Bearer "+expected:
        return jsonify(error="Unauthorized"),401
    from bank_lib.minebank_scheduler import process_due_scheduled_transfers
    try:
        if not ensure_minebank_schema():
            return jsonify(error="MineBank database is unavailable"),503
        interest=accrue_daily_credit_interest()
        billing=[]
        statements=[]
        if datetime.now(timezone.utc).day == 1:
            from datetime import timedelta
            today=datetime.now(timezone.utc).date()
            first=today.replace(day=1)
            prev_end=first-timedelta(days=1)
            prev_start=prev_end.replace(day=1)
            facilities=execute_query_dict("SELECT account_id FROM credit_facilities WHERE status IN ('ACTIVE','SUSPENDED')")
            statements=[generate_cashline_statement(int(x["account_id"]),prev_start,prev_end) for x in facilities]
            from bank_lib.minebank_core import process_monthly_billing
            billing=process_monthly_billing()
        return jsonify(interest=interest,billing=billing,statements=statements,scheduled=process_due_scheduled_transfers())
    except Exception as exc:
        return jsonify(error=str(exc)),500

@app.route("/portal/requests")
@require_login
def minebank_requests_page():
    execute_query("""UPDATE bank_notifications SET read_at=CURRENT_TIMESTAMP
                     WHERE client_id=%s AND notification_type='REQUEST_UPDATE' AND read_at IS NULL""",
                  (session["minebank_client_id"],),commit=True)
    return render_template("minebank_portal.html",mode="requests",
                           requests=list_requests(session["minebank_client_id"]),portal_active="requests")

@app.route("/portal/payment-requests",methods=["GET","POST"])
@require_login
def minebank_payment_requests_page():
    cid=session["minebank_client_id"]
    account=selected_account()
    error=None
    if request.method=="POST":
        try:
            payer_number=request.form.get("payer_account_number","").strip().upper()
            amount=int(request.form.get("amount","0"))
            if amount<=0: raise ValueError("Requested amount must be positive.")
            count=execute_query("SELECT COUNT(*) FROM minebank_payment_requests WHERE requester_client_id=%s AND created_at::date=CURRENT_DATE",(cid,))
            if count and int(count[0][0]) >= 5:
                raise ValueError("Maximum 5 Payment Requests per day.")
            payer=execute_query_dict("SELECT id,client_id FROM bank_accounts WHERE account_number=%s AND status<>'CLOSED'",(payer_number,))
            if not payer: raise ValueError("Payer account not found.")
            if payer[0]["client_id"]==cid: raise ValueError("You cannot request payment from your own account.")
            if int(account["balance"]) < 1:
                raise ValueError("1 Emerald is required for a Payment Request.")
            execute_query("UPDATE bank_accounts SET balance=balance-1,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(account["id"],),commit=True)
            execute_query("""INSERT INTO ledger_transactions
                             (transaction_id,transaction_type,amount,fee,currency,sender_account_id,status,description)
                             VALUES(%s,'PAYMENT_REQUEST_FEE',1,0,'Emerald',%s,'COMPLETED','Payment Request fee')""",
                          (f"MB-PREQ-{secrets.token_hex(6)}",account["id"]),commit=True)
            execute_query("""INSERT INTO minebank_payment_requests
                             (requester_client_id,payer_client_id,requester_account_id,payer_account_number,amount,description,expires_at)
                             VALUES(%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP+INTERVAL '30 days')""",
                          (cid,payer[0]["client_id"],account["id"],payer_number,amount,
                           request.form.get("description","")[:500]),commit=True)
            flash("Payment request sent. It expires in 30 days.","success")
        except Exception as exc:
            error=str(exc)
    incoming=execute_query_dict("""SELECT p.*,a.account_number requester_account_number,c.email requester_email
                                    FROM minebank_payment_requests p
                                    JOIN bank_accounts a ON a.id=p.requester_account_id
                                    JOIN bank_clients c ON c.id=p.requester_client_id
                                    WHERE p.payer_client_id=%s AND p.status='PENDING' AND p.expires_at>CURRENT_TIMESTAMP
                                    ORDER BY p.created_at DESC""",(cid,))
    outgoing=execute_query_dict("""SELECT p.*,a.account_number requester_account_number
                                    FROM minebank_payment_requests p
                                    JOIN bank_accounts a ON a.id=p.requester_account_id
                                    WHERE p.requester_client_id=%s ORDER BY p.created_at DESC LIMIT 100""",(cid,))
    return render_template("minebank_portal.html",mode="payment_requests",account=account,error=error,
                           incoming_requests=incoming,outgoing_requests=outgoing,portal_active="payment_requests")

@app.route("/portal/payment-requests/<int:request_id>/reject",methods=["POST"])
@require_login
def reject_payment_request(request_id):
    execute_query("UPDATE minebank_payment_requests SET status='REJECTED' WHERE id=%s AND payer_client_id=%s AND status='PENDING'",
                  (request_id,session["minebank_client_id"]),commit=True)
    flash("Payment request rejected.","success")
    return redirect(url_for("minebank_payment_requests_page"))

@app.route("/portal/payment-requests/<int:request_id>/pay",methods=["POST"])
@require_login
def pay_payment_request(request_id):
    row=execute_query_dict("""SELECT p.*,a.account_number requester_account_number
                              FROM minebank_payment_requests p JOIN bank_accounts a ON a.id=p.requester_account_id
                              WHERE p.id=%s AND p.payer_client_id=%s AND p.status='PENDING' AND p.expires_at>CURRENT_TIMESTAMP""",
                           (request_id,session["minebank_client_id"]))
    if not row:
        flash("Payment request not found, expired or already processed.","error")
        return redirect(url_for("minebank_payment_requests_page"))
    payer=execute_query_dict("SELECT id FROM bank_accounts WHERE account_number=%s AND client_id=%s",(row[0]["payer_account_number"],session["minebank_client_id"]))
    if not payer:
        flash("The payer account is no longer available.","error")
        return redirect(url_for("minebank_payment_requests_page"))
    try:
        from bank_lib.minebank_auth import verify_wallet_pin
        ok,msg=verify_wallet_pin(session["minebank_client_id"],request.form.get("wallet_pin",""))
        if not ok: raise ValueError(msg)
        result=transfer(sender_account_id=payer[0]["id"],recipient_account_number=row[0]["requester_account_number"],
                        amount=int(row[0]["amount"]),description=row[0]["description"],actor_client_id=session["minebank_client_id"],
                        ip_address=request.remote_addr,transfer_kind="PAYMENT_REQUEST",
                        idempotency_key=f"PAYREQ-{request_id}")
        if result["status"]=="COMPLETED":
            execute_query("UPDATE minebank_payment_requests SET status='PAID',paid_transaction_id=%s WHERE id=%s AND status='PENDING'",
                          (result["transaction_id"],request_id),commit=True)
            flash("Payment request paid.","success")
        else:
            flash("Payment submitted and is pending bank approval.","success")
    except Exception as exc:
        flash(str(exc),"error")
    return redirect(url_for("minebank_payment_requests_page"))

@app.route("/portal/profile", methods=["GET","POST"])
@require_login
def minebank_profile_page():
    if request.method=="POST":
        execute_query("""UPDATE bank_clients
                         SET date_of_birth=%s,first_name=%s,last_name=%s,phone=%s,address=%s,city=%s,postal_code=%s,country=%s,occupation=%s,updated_at=CURRENT_TIMESTAMP
                         WHERE id=%s""",
                      (request.form.get("date_of_birth") or None,
                       request.form.get("first_name","").strip()[:100],
                       request.form.get("last_name","").strip()[:100],
                       request.form.get("phone","").strip()[:40],
                       request.form.get("address","").strip()[:300],
                       request.form.get("city","").strip()[:120],
                       request.form.get("postal_code","").strip()[:20],
                       request.form.get("country","").strip()[:80],
                       request.form.get("occupation","").strip()[:120],
                       session["minebank_client_id"]),commit=True)
        flash("Profile updated.","success")
    return render_template("minebank_portal.html",mode="profile",client=current_client(),portal_active="profile")

@app.route("/portal/notifications", methods=["GET","POST"])
@require_login
def minebank_notifications_page():
    if request.method=="POST":
        if request.form.get("action")=="read_all":
            execute_query("UPDATE bank_notifications SET read_at=CURRENT_TIMESTAMP WHERE client_id=%s AND read_at IS NULL",
                          (session["minebank_client_id"],),commit=True)
        elif request.form.get("notification_id"):
            execute_query("UPDATE bank_notifications SET read_at=CURRENT_TIMESTAMP WHERE id=%s AND client_id=%s",
                          (request.form["notification_id"],session["minebank_client_id"]),commit=True)
    messages=execute_query_dict("SELECT * FROM bank_notifications WHERE client_id=%s ORDER BY created_at DESC LIMIT 100",(session["minebank_client_id"],))
    return render_template("minebank_portal.html",mode="notifications",messages=messages,portal_active="notifications")

@app.route("/portal/security", methods=["GET","POST"])
@require_login
def minebank_security_page():
    error=None
    client=current_client()
    if request.method=="POST":
        try:
            old=request.form.get("current_password",""); pin=request.form.get("wallet_pin",""); new_password=request.form.get("new_password","")
            if not check_password_hash(client["password_hash"],old): raise ValueError("Current password is incorrect.")
            if not pin.isdigit() or len(pin)!=6: raise ValueError("Wallet PIN must contain exactly 6 digits.")
            set_wallet_pin(client["id"],pin)
            if new_password:
                if len(new_password)<8: raise ValueError("New password must contain at least 8 characters.")
                execute_query("UPDATE bank_clients SET password_hash=%s,password_changed_at=CURRENT_TIMESTAMP WHERE id=%s",(generate_password_hash(new_password),client["id"]),commit=True)
                session.pop("minebank_password_expired",None)
                terminate_other_sessions(client["id"])
            security_event(client["id"],"SECURITY_SETTINGS_CHANGED","INFO",{})
            flash("Security settings updated.","success")
            return redirect(url_for("minebank_security_page"))
        except Exception as exc:
            error=str(exc)
    sessions=list_active_sessions(client["id"])
    events=execute_query_dict("SELECT event_type,severity,ip_address,user_agent,created_at,context FROM minebank_security_events WHERE client_id=%s ORDER BY created_at DESC LIMIT 50",(client["id"],))
    return render_template("minebank_portal.html",mode="security",client=client,error=error,sessions=sessions,security_events=events,portal_active="security")

@app.route("/portal/security/session/<int:session_id>/terminate",methods=["POST"])
@require_login
def terminate_minebank_session(session_id):
    terminate_session(session["minebank_client_id"],session_id)
    flash("Session terminated.","success")
    return redirect(url_for("minebank_security_page"))

@app.route("/portal/security/sessions/terminate-others",methods=["POST"])
@require_login
def terminate_minebank_other_sessions():
    terminate_other_sessions(session["minebank_client_id"])
    flash("All other sessions have been terminated.","success")
    return redirect(url_for("minebank_security_page"))

@app.route("/portal/security/emergency-lock",methods=["POST"])
@require_login
def minebank_emergency_lock():
    cid=session["minebank_client_id"]
    execute_query("UPDATE bank_accounts SET status='FROZEN',freeze_type='EMERGENCY_FREEZE',updated_at=CURRENT_TIMESTAMP WHERE client_id=%s AND status<>'CLOSED'",(cid,),commit=True)
    execute_query("UPDATE credit_facilities SET status='SUSPENDED' WHERE account_id IN (SELECT id FROM bank_accounts WHERE client_id=%s)",(cid,),commit=True)
    security_event(cid,"EMERGENCY_ACCOUNT_LOCK","CRITICAL",{})
    terminate_other_sessions(cid)
    flash("Emergency security lock activated. Contact Support to restore access.","success")
    return redirect(url_for("minebank_security_page"))

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
            freeze_type=request.form.get("freeze_type") if status=="FROZEN" else None
            if freeze_type not in {None,"MANUAL_FREEZE","SECURITY_FREEZE","CREDIT_FREEZE","COMPLIANCE_FREEZE","EMERGENCY_FREEZE","SYSTEM_FREEZE"}: freeze_type="MANUAL_FREEZE"
            execute_query("UPDATE bank_accounts SET status=%s,freeze_type=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(db_status,freeze_type,account_id),commit=True)
            security_event(session["minebank_client_id"],"ACCOUNT_STATUS_CHANGED","WARNING" if status!="ENABLED" else "INFO",{"account_id":account_id,"status":status,"freeze_type":freeze_type})
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
    if not ensure_loan_schema():
        return "MineBank request processing is temporarily unavailable. Please try again in a moment.", 503
    data=request.get_json(silent=True) or request.form
    approve=str(data.get("approve","")).lower() in {"1","true","yes","approve","approved"}
    if not approve and not str(data.get("reason","")).strip():
        return jsonify(error="A rejection reason is required."),400
    row=execute_query_dict("SELECT * FROM bank_requests_v2 WHERE id=%s AND status='PENDING'",(request_id,))
    if not row: return jsonify(error="Request not found or already reviewed."),404
    r=row[0]
    try:
        if approve and r["request_type"]=="LOAN_REQUEST":
            p=r["payload"] or {}
            create_notification(r["client_id"],"LOAN_APPROVED","Loan approved",
                                f"Your Loan {p.get('loan_number')} for {p.get('amount')} Emerald has been approved and deposited.",
                                r["account_id"])
        if approve and r["request_type"]=="CREDIT_LINE":
            requested=int((r["payload"] or {}).get("requested_limit",0))
            limit=int(data.get("approved_limit") or requested)
            if limit<300 or limit>requested:
                raise ValueError("Approved CashLine limit must be at least 300 Emerald and no higher than the requested amount.")
            execute_query("""INSERT INTO credit_facilities(account_id,credit_limit,interest_monthly_bps,interest_annual_bps,activation_fee_monthly,status)
                             VALUES(%s,%s,CASE WHEN (SELECT account_type FROM bank_accounts WHERE id=%s)='PERSONAL' THEN 1830 ELSE 2370 END,CASE WHEN (SELECT account_type FROM bank_accounts WHERE id=%s)='PERSONAL' THEN 1830 ELSE 2370 END,CASE WHEN %s>5000 THEN 20 ELSE 10 END,'ACTIVE')
                             ON CONFLICT(account_id) DO UPDATE SET credit_limit=EXCLUDED.credit_limit,
                               activation_fee_monthly=EXCLUDED.activation_fee_monthly,interest_annual_bps=EXCLUDED.interest_annual_bps,status='ACTIVE'""",
                          (r["account_id"],limit,r["account_id"],r["account_id"],limit),commit=True)
        elif approve and r["request_type"]=="CREDIT_CANCEL":
            if cashline_outstanding_for_app(r["account_id"])>0:
                raise ValueError("CashLine cannot be cancelled while a balance is outstanding.")
            execute_query("UPDATE credit_facilities SET status='CLOSED' WHERE account_id=%s AND status='ACTIVE'",(r["account_id"],),commit=True)
        elif approve and r["request_type"]=="TIER_CHANGE":
            code=(r["payload"] or {}).get("tier_code")
            tier=execute_query_dict("SELECT id FROM account_tiers_v2 WHERE code=%s AND active=TRUE",(code,))
            if not tier: raise ValueError("Requested tier is unavailable.")
            execute_query("UPDATE bank_accounts SET tier_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(tier[0]["id"],r["account_id"]),commit=True)
        elif approve and r["request_type"]=="LOAN_REQUEST":
            p=r["payload"] or {}
            loan_id=int(p.get("loan_id") or 0)
            loan=execute_query_dict("SELECT * FROM minebank_loans WHERE id=%s AND status='PENDING' AND client_id=%s",(loan_id,r["client_id"]))
            if not loan: raise ValueError("Loan request no longer exists or has already been reviewed.")
            disburse_loan(loan_id,session["minebank_client_id"])
        elif not approve and r["request_type"]=="LOAN_REQUEST":
            p=r["payload"] or {}
            loan_id=int(p.get("loan_id") or 0)
            execute_query("UPDATE minebank_loans SET status='REJECTED',notes=%s WHERE id=%s AND status='PENDING'",(str(data.get("reason","")).strip()[:1000],loan_id),commit=True)
        elif approve and r["request_type"]=="REFUND":
            p=r["payload"] or {}
            tx=execute_query_dict("SELECT status FROM ledger_transactions WHERE transaction_id=%s",(p.get("transaction_id"),))
            if not tx:
                raise ValueError("Transaction no longer exists.")
            if tx[0]["status"]=="COMPLETED":
                chargeback(p["transaction_id"],session["minebank_client_id"],p.get("reason"))
            elif tx[0]["status"]=="PENDING_APPROVAL":
                cancel_transfer(p["transaction_id"],r["client_id"],request.remote_addr)
            else:
                raise ValueError("This transaction is no longer eligible for refund/chargeback.")
        status="APPROVED" if approve else "REJECTED"
        review_reason=str(data.get("reason","")).strip()[:500]
        execute_query("UPDATE bank_requests_v2 SET status=%s,reviewed_by=%s,reviewed_at=CURRENT_TIMESTAMP WHERE id=%s",
                      (status,session["minebank_client_id"],request_id),commit=True)
        create_notification(r["client_id"],"REQUEST_UPDATE",
                            f"Request {status.lower()}",
                            (f"Your {r['request_type']} request was approved."
                             if approve else f"Your {r['request_type']} request was rejected: {review_reason}"),
                            r["account_id"])
        if approve and r["request_type"]=="CREDIT_LINE":
            create_notification(r["client_id"],"REQUEST_UPDATE","Credit line approved",
                                f"Your credit request was approved for {limit} Emerald.",
                                r["account_id"])
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

@app.route("/admin/minebank/transactions", methods=["GET"])
@require_role("ADMIN","OPERATOR")
def admin_minebank_transactions():
    q=request.args.get("q","").strip()
    status=request.args.get("status","").strip().upper()
    params=[]
    where=[]
    if q:
        where.append("(l.transaction_id ILIKE %s OR COALESCE(l.description,'') ILIKE %s OR COALESCE(l.causal,'') ILIKE %s OR COALESCE(s.account_number,'') ILIKE %s OR COALESCE(r.account_number,'') ILIKE %s)")
        like=f"%{q}%"; params.extend([like,like,like,like,like])
    if status:
        where.append("l.status=%s"); params.append(status)
    condition=("WHERE "+" AND ".join(where)) if where else ""
    transactions=execute_query_dict(
        """SELECT l.*,s.account_number sender_number,r.account_number recipient_number,
                  sc.email sender_email,rc.email recipient_email,
                  COALESCE(string_agg(DISTINCT a.action,' | '),'') audit_actions
           FROM ledger_transactions l
           LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
           LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
           LEFT JOIN bank_clients sc ON sc.id=s.client_id
           LEFT JOIN bank_clients rc ON rc.id=r.client_id
           LEFT JOIN audit_events a ON a.transaction_id=l.transaction_id
           """+condition+""" GROUP BY l.id,s.account_number,r.account_number,sc.email,rc.email
           ORDER BY l.created_at DESC LIMIT 500""",tuple(params))
    audits=execute_query_dict("""SELECT a.*,c.email actor_email FROM audit_events a
                                 LEFT JOIN bank_clients c ON c.id=a.actor_client_id
                                 ORDER BY a.created_at DESC LIMIT 300""")
    risks=execute_query_dict("""SELECT r.*,a.account_number,c.email FROM minebank_risk_events r
                                LEFT JOIN bank_accounts a ON a.id=r.account_id
                                LEFT JOIN bank_clients c ON c.id=r.client_id
                                ORDER BY r.created_at DESC LIMIT 300""")
    security=execute_query_dict("""SELECT s.*,c.email FROM minebank_security_events s
                                   LEFT JOIN bank_clients c ON c.id=s.client_id
                                   ORDER BY s.created_at DESC LIMIT 300""")
    return render_template("minebank_admin_new.html",mode="transactions",transactions=transactions,audits=audits,risks=risks,security=security)

@app.route("/admin/minebank/messages", methods=["GET","POST"])
@require_role("ADMIN","OPERATOR")
def admin_minebank_messages():
    if not ensure_message_schema():
        return "MineBank messaging is temporarily unavailable. Please try again in a moment.", 503
    error=None
    if request.method=="POST":
        try:
            subject=request.form.get("subject","").strip()[:200]
            body=request.form.get("body","").strip()[:2000]
            target_mode=request.form.get("target_mode","ALL")
            account_number=request.form.get("account_number","").strip()
            account_type=request.form.get("account_type","").strip().upper()
            tier_code=request.form.get("tier_code","").strip().upper()
            if not subject or not body:
                raise ValueError("Subject and message are required.")
            if target_mode=="ACCOUNT":
                targets=execute_query_dict("SELECT id,client_id FROM bank_accounts WHERE account_number=%s AND status<>'CLOSED'",(account_number,))
            else:
                conditions=["a.status<>'CLOSED'"]; params=[]
                if target_mode=="TYPE":
                    conditions.append("a.account_type=%s"); params.append(account_type)
                elif target_mode=="TIER":
                    conditions.append("t.code=%s"); params.append(tier_code)
                targets=execute_query_dict("SELECT a.id,a.client_id FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE "+" AND ".join(conditions)+" ORDER BY a.id",tuple(params))
            if not targets:
                raise ValueError("No matching accounts were found.")
            recipients={}
            for target in targets:
                recipients.setdefault(target["client_id"], target["id"])
            for client_id, account_id in recipients.items():
                create_notification(client_id,"BANK_MESSAGE",subject,body,account_id)
            execute_query("""INSERT INTO minebank_message_campaigns(created_by,subject,body,recipient_filter)
                             VALUES(%s,%s,%s,%s::jsonb)""",
                          (session["minebank_client_id"],subject,body,__import__("json").dumps({
                              "mode":target_mode,"account_number":account_number,"account_type":account_type,"tier_code":tier_code,
                              "recipient_count":len(recipients)
                          })),commit=True)
            flash(f"Message sent to {len(recipients)} customer(s).","success")
            return redirect(url_for("admin_minebank_messages"))
        except Exception as exc:
            error=str(exc)
    accounts=execute_query_dict("SELECT a.account_number,a.account_type,t.code,t.display_name FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE a.status<>'CLOSED' ORDER BY a.account_number")
    tiers=execute_query_dict("SELECT code,display_name,account_type FROM account_tiers_v2 WHERE active=TRUE ORDER BY account_type,id")
    campaigns=execute_query_dict("""SELECT m.*,c.email created_by_email FROM minebank_message_campaigns m
                                    JOIN bank_clients c ON c.id=m.created_by ORDER BY m.created_at DESC LIMIT 50""")
    return render_template("minebank_admin_new.html",mode="messages",accounts=accounts,tiers=tiers,campaigns=campaigns,error=error)

@app.route("/admin/minebank/profiles")
@require_role("ADMIN","OPERATOR")
def admin_minebank_profiles():
    ensure_credicheck_schema()
    q=request.args.get("q","").strip()
    params=[]
    where=""
    if q:
        where="""WHERE c.email ILIKE %s
                  OR COALESCE(c.first_name,'') ILIKE %s
                  OR COALESCE(c.last_name,'') ILIKE %s
                  OR EXISTS (SELECT 1 FROM bank_accounts ax WHERE ax.client_id=c.id AND ax.account_number ILIKE %s)"""
        like=f"%{q}%"
        params=[like,like,like,like]
    profiles=execute_query_dict(f"""SELECT c.id,c.email,c.first_name,c.last_name,c.status,c.role,c.last_login,
        COUNT(DISTINCT a.id) FILTER (WHERE a.status<>'CLOSED') AS account_count,
        COALESCE(SUM(a.balance) FILTER (WHERE a.status<>'CLOSED'),0) AS total_balance,
        COUNT(DISTINCT cf.id) FILTER (WHERE cf.status IN ('ACTIVE','SUSPENDED')) AS credit_facilities,
        COALESCE(SUM(cf.credit_limit) FILTER (WHERE cf.status IN ('ACTIVE','SUSPENDED')),0) AS credit_limit
        FROM bank_clients c
        LEFT JOIN bank_accounts a ON a.client_id=c.id
        LEFT JOIN credit_facilities cf ON cf.account_id=a.id
        {where}
        GROUP BY c.id ORDER BY c.id DESC LIMIT 250""",tuple(params))
    return render_template("minebank_admin_new.html",mode="profiles",profiles=profiles)

@app.route("/admin/minebank/profile/<int:client_id>")
@require_role("ADMIN","OPERATOR")
def admin_minebank_profile(client_id):
    ensure_credicheck_schema()
    profile_rows=execute_query_dict("""SELECT c.*,
        COUNT(DISTINCT a.id) FILTER (WHERE a.status<>'CLOSED') AS account_count,
        COALESCE(SUM(a.balance) FILTER (WHERE a.status<>'CLOSED'),0) AS total_balance
        FROM bank_clients c LEFT JOIN bank_accounts a ON a.client_id=c.id
        WHERE c.id=%s GROUP BY c.id""",(client_id,))
    if not profile_rows:
        flash("Customer profile not found.","error")
        return redirect(url_for("admin_minebank_profiles"))
    profile=profile_rows[0]
    accounts=execute_query_dict("""SELECT a.*,t.display_name,t.code AS tier_code,
        COALESCE(cf.status,'NONE') AS credit_status,COALESCE(cf.credit_limit,0) AS credit_limit,
        COALESCE(cf.interest_annual_bps,0) AS interest_annual_bps
        FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
        LEFT JOIN credit_facilities cf ON cf.account_id=a.id
        WHERE a.client_id=%s ORDER BY a.id""",(client_id,))
    credit=execute_query_dict("""SELECT cf.*,a.account_number
        FROM credit_facilities cf JOIN bank_accounts a ON a.id=cf.account_id
        WHERE a.client_id=%s ORDER BY cf.id DESC""",(client_id,))
    transactions=execute_query_dict("""SELECT l.*,s.account_number sender_number,r.account_number recipient_number
        FROM ledger_transactions l
        LEFT JOIN bank_accounts s ON s.id=l.sender_account_id
        LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
        WHERE s.client_id=%s OR r.client_id=%s ORDER BY l.created_at DESC LIMIT 250""",(client_id,client_id))
    statements=execute_query_dict("""SELECT cs.*,a.account_number
        FROM credit_statements cs JOIN bank_accounts a ON a.id=cs.account_id
        WHERE a.client_id=%s ORDER BY cs.period_end DESC LIMIT 100""",(client_id,))
    requests=execute_query_dict("""SELECT r.*,a.account_number,rv.email reviewer_email
        FROM bank_requests_v2 r LEFT JOIN bank_accounts a ON a.id=r.account_id
        LEFT JOIN bank_clients rv ON rv.id=r.reviewed_by
        WHERE r.client_id=%s ORDER BY r.created_at DESC LIMIT 150""",(client_id,))
    security=execute_query_dict("""SELECT * FROM minebank_security_events WHERE client_id=%s
        ORDER BY created_at DESC LIMIT 150""",(client_id,))
    risk=execute_query_dict("""SELECT r.*,a.account_number FROM minebank_risk_events r
        LEFT JOIN bank_accounts a ON a.id=r.account_id WHERE r.client_id=%s
        ORDER BY r.created_at DESC LIMIT 150""",(client_id,))
    notifications=execute_query_dict("""SELECT n.*,a.account_number FROM bank_notifications n
        LEFT JOIN bank_accounts a ON a.id=n.account_id WHERE n.client_id=%s
        ORDER BY n.created_at DESC LIMIT 150""",(client_id,))
    audits=execute_query_dict("""SELECT ae.*,c.email actor_email FROM audit_events ae
        LEFT JOIN bank_clients c ON c.id=ae.actor_client_id
        WHERE ae.target_id=%s OR ae.actor_client_id=%s ORDER BY ae.created_at DESC LIMIT 200""",(client_id,client_id))
    business=execute_query_dict("""SELECT bm.*,a.account_number,bp.trading_name,bp.legal_name
        FROM minebank_business_members bm
        LEFT JOIN bank_accounts a ON a.id=bm.account_id
        LEFT JOIN minebank_business_profiles bp ON bp.account_id=bm.account_id
        WHERE bm.client_id=%s ORDER BY bm.created_at DESC""",(client_id,))
    cc=get_credicheck_profile(client_id)
    cc_events=__import__("bank_lib.credicheck",fromlist=["list_events"]).list_events(client_id,150)
    cc_decisions=execute_query_dict("SELECT * FROM credicheck_decisions WHERE client_id=%s ORDER BY created_at DESC LIMIT 100",(client_id,))
    cc_overrides=execute_query_dict("""SELECT o.*,a.email admin_email FROM credicheck_overrides o
        LEFT JOIN bank_clients a ON a.id=o.admin_id WHERE o.client_id=%s ORDER BY o.created_at DESC LIMIT 100""",(client_id,))
    product_access=execute_query_dict("SELECT * FROM credicheck_product_access WHERE client_id=%s ORDER BY product_type",(client_id,))
    dynamic=execute_query_dict("""SELECT d.*,a.account_number FROM dynamic_cashlines d
        JOIN bank_accounts a ON a.id=d.account_id WHERE d.client_id=%s ORDER BY d.id DESC""",(client_id,))
    return render_template("minebank_admin_new.html",mode="profile",profile=profile,accounts=accounts,credit=credit,
        transactions=transactions,statements=statements,requests=requests,security=security,risk=risk,
        notifications=notifications,audits=audits,business=business,credicheck_available=True,
        credicheck=[cc],credicheck_events=cc_events,credicheck_decisions=cc_decisions,
        credicheck_overrides=cc_overrides,product_access=product_access,dynamic=dynamic)

@app.route("/admin/minebank/profile/<int:client_id>/credicheck",methods=["POST"])
@require_role("ADMIN")
def admin_minebank_credicheck(client_id):
    action=request.form.get("action","").strip().upper()
    reason=request.form.get("reason","").strip()
    try:
        if not reason:
            raise ValueError("A reason is required for every CrediCheck administrative change.")
        if action=="SET_SCORE":
            credicheck_set_score(client_id,session["minebank_client_id"],int(request.form.get("score",5)),reason)
            flash("CrediCheck score updated and audit recorded.","success")
        elif action=="SET_ACCESS":
            product=request.form.get("product_type","").strip().upper()
            status=request.form.get("status","").strip().upper()
            credicheck_set_product_access(client_id,session["minebank_client_id"],product,status,reason)
            flash("Credit-product access updated.","success")
        elif action=="ASSESS":
            account_id=int(request.form.get("account_id"))
            rows=execute_query_dict("""SELECT a.*,t.code AS tier_code,t.display_name FROM bank_accounts a
                JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE a.id=%s AND a.client_id=%s""",(account_id,client_id))
            if not rows: raise ValueError("Account not found.")
            result=credicheck_assess(client_id,rows[0],request.form.get("product_type","CASHLINE"),int(request.form.get("amount",0) or 0))
            flash(f"CrediCheck assessment: {result['decision']} — {result['reason']}","success")
        elif action=="ACTIVATE_DYNAMIC":
            account_id=int(request.form.get("account_id"))
            activate_dynamic_cashline(client_id,account_id,session["minebank_client_id"])
            flash("Dynamic CashLine activated.","success")
        elif action=="CLOSE_DYNAMIC":
            account_id=int(request.form.get("account_id"))
            close_dynamic_cashline(client_id,account_id,session["minebank_client_id"])
            flash("Dynamic CashLine closed.","success")
        else:
            raise ValueError("Unknown CrediCheck action.")
    except Exception as exc:
        flash(f"CrediCheck action failed: {exc}","error")
    return redirect(url_for("admin_minebank_profile",client_id=client_id)+"#credit")

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
                create_account(admin_id, "BUSINESS", "BUSINESS", charge_opening_fee=False)
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
