"""MineBank v2 authentication and account primitives.

The legacy wallet system remains available during migration. These helpers are
the canonical identity/session layer for the new portal.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import request
from flask import jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .database import get_db_connection
from .minebank_security import (clear_login_failures, create_session, ensure_security_schema, login_challenge_required,
                                check_login_challenge, record_login_failure, set_login_challenge, revoke_current_session, security_event)


WALLET_PIN_MAX_ATTEMPTS = 3
WALLET_PIN_LOCKOUT_MINUTES = 5
ADMIN_REAUTH_MINUTES = 10


def _now():
    return datetime.now(timezone.utc)


def client_from_session(cur):
    client_id = session.get("minebank_client_id")
    if not client_id:
        return None
    cur.execute(
        "SELECT id,email,role,status,date_of_birth,wallet_pin_hash,wallet_pin_failed_attempts,"
        "wallet_pin_locked_until,admin_reauth_at FROM bank_clients WHERE id=%s",
        (client_id,),
    )
    return cur.fetchone()


def login_client(email, password, captcha_answer=None):
    # Login must fail gracefully when the database is temporarily unavailable.
    try:
        ensure_security_schema()
    except Exception as exc:
        print(f"MineBank security schema warning: {type(exc).__name__}: {exc}")
    conn = get_db_connection()
    if conn is None:
        return False, "MineBank is temporarily unavailable. Please try again in a moment."
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,email,password_hash,role,status,login_failed_attempts,login_blocked_until,login_captcha_required,password_changed_at
                               FROM bank_clients WHERE LOWER(email)=LOWER(%s)""",(email.strip(),))
                row=cur.fetchone()
                if not row:
                    record_login_failure(None,"UNKNOWN_ACCOUNT")
                    return False,"Invalid credentials."
                client_id=row[0]
                now=_now()
                if row[6] and row[6] <= now:
                    cur.execute("UPDATE bank_clients SET login_blocked_until=NULL WHERE id=%s",(client_id,))
                    row=list(row); row[6]=None
                if row[6] and row[6] > now:
                    seconds=max(1,int((row[6]-now).total_seconds()))
                    return False,f"Login temporarily blocked. Try again in {seconds} seconds."
                if row[7]:
                    if not check_login_challenge(captcha_answer):
                        set_login_challenge()
                        record_login_failure(client_id,"CAPTCHA_REQUIRED")
                        return False,"Complete the security challenge before continuing."
                if row[4] == "CLOSED":
                    record_login_failure(client_id,"DISABLED_ACCOUNT")
                    return False,"This account is disabled. Please contact the bank."
                if row[4] == "FROZEN":
                    security_event(client_id,"FROZEN_ACCOUNT_LOGIN","WARNING",{"message":"Frozen account login allowed for viewing and support access."})
                if not check_password_hash(row[2],password):
                    attempts,minutes=record_login_failure(client_id,"INVALID_PASSWORD")
                    if attempts>=3:
                        set_login_challenge()
                        return False,f"Too many failed attempts. Login is temporarily blocked for {minutes} minutes."
                    if attempts>=2:
                        set_login_challenge()
                        return False,"Invalid credentials. Complete the security challenge on the next attempt."
                    return False,"Invalid credentials."
                clear_login_failures(client_id)
                score,severity,reasons=__import__("bank_lib.minebank_security",fromlist=["suspicious_login_score"]).suspicious_login_score(
                    client_id=client_id,
                    ip_address=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
                    user_agent=request.headers.get("User-Agent","")[:500])
                if score>=60:
                    security_event(client_id,"SUSPICIOUS_LOGIN","CRITICAL",{"score":score,"reasons":reasons})
                    return False,"Login blocked for security review. Please contact the bank."
                if score>=30:
                    security_event(client_id,"SUSPICIOUS_LOGIN","HIGH",{"score":score,"reasons":reasons})
                session.clear()
                session.permanent=True
                session["minebank_client_id"]=row[0]
                session["minebank_role"]=row[3]
                session["minebank_email"]=row[1]
                try:
                    session["minebank_password_expired"] = (_now() - row[8]).days >= 180 if row[8] else True
                except Exception:
                    session["minebank_password_expired"] = False
                create_session(client_id)
                return True,None
    finally:
        from .database import release_db_connection
        release_db_connection(conn)

def logout_client():
    revoke_current_session()
    session.pop("minebank_client_id", None)
    session.pop("minebank_role", None)
    session.pop("minebank_email", None)
    session.pop("minebank_account_id", None)
    session.pop("minebank_admin_reauth", None)


def set_wallet_pin(client_id, pin):
    if not isinstance(pin, str) or not pin.isdigit() or len(pin) != 6:
        raise ValueError("Wallet PIN must contain exactly 6 digits.")
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bank_clients SET wallet_pin_hash=%s,wallet_pin_failed_attempts=0,"
                    "wallet_pin_locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (generate_password_hash(pin), client_id),
                )
    finally:
        from .database import release_db_connection
        release_db_connection(conn)


def verify_wallet_pin(client_id, pin):
    """Verify a Wallet PIN and apply the 3-attempt/5-minute lockout without nested DB transactions."""
    conn=get_db_connection()
    notification=None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT wallet_pin_hash,wallet_pin_failed_attempts,wallet_pin_locked_until FROM bank_clients WHERE id=%s FOR UPDATE",(client_id,))
                row=cur.fetchone()
                if not row or not row[0]:
                    return False,"Wallet PIN is not configured."
                now=_now()
                if row[2] and row[2]>now:
                    seconds=max(1,int((row[2]-now).total_seconds()))
                    return False,f"Wallet PIN temporarily locked. Try again in {seconds} seconds."
                if check_password_hash(row[0],pin):
                    cur.execute("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",(client_id,))
                    return True,None
                attempts=int(row[1] or 0)+1
                if attempts>=WALLET_PIN_MAX_ATTEMPTS:
                    locked=now+timedelta(minutes=WALLET_PIN_LOCKOUT_MINUTES)
                    cur.execute("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=%s WHERE id=%s",(locked,client_id))
                    notification=("Wallet PIN locked","Three failed Wallet PIN attempts triggered a 5-minute security lockout.","MineBank security alert","Three failed Wallet PIN attempts triggered a 5-minute lockout on your MineBank account.")
                    result=(False,"Wallet PIN temporarily locked for 5 minutes after three failed attempts.")
                else:
                    cur.execute("UPDATE bank_clients SET wallet_pin_failed_attempts=%s WHERE id=%s",(attempts,client_id))
                    if attempts>=2:
                        notification=("Security warning",f"Wallet PIN failed attempt {attempts} of 3.","MineBank security warning",f"Your MineBank Wallet PIN has failed {attempts} times. Review Security Centre if this was not you.")
                    result=(False,f"Invalid wallet PIN. Failed attempt {attempts} of 3.")
    finally:
        from .database import release_db_connection
        release_db_connection(conn)
    if notification:
        try:
            from bank_lib.minebank_requests import create_notification
            from bank_lib.minebank_features import queue_email
            title,message,subject,email_body=notification
            create_notification(client_id,"SECURITY",title,message,None)
            queue_email(client_id,"SECURITY",subject,email_body)
        except Exception:
            pass
    return result

def require_minebank_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("minebank_client_id"):
            return jsonify({"error": "MineBank login required"}), 401
        return f(*args, **kwargs)
    return wrapper


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("minebank_client_id"):
                return jsonify({"error": "MineBank login required"}), 401
            if session.get("minebank_role") not in roles:
                return jsonify({"error": "Insufficient permissions"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_admin_reauth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("minebank_role") != "ADMIN":
            return jsonify({"error": "Admin access required"}), 403
        stamp = session.get("minebank_admin_reauth")
        if not stamp or (_now().timestamp() - float(stamp)) > ADMIN_REAUTH_MINUTES * 60:
            return jsonify({"error": "Admin re-authentication required"}), 428
        return f(*args, **kwargs)
    return wrapper


def reauthenticate_admin(client_id, password):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT password_hash,role FROM bank_clients WHERE id=%s", (client_id,))
                row = cur.fetchone()
                if not row or row[1] != "ADMIN" or not check_password_hash(row[0], password):
                    return False
                session["minebank_admin_reauth"] = _now().timestamp()
                return True
    finally:
        from .database import release_db_connection
        release_db_connection(conn)


def create_account(client_id, account_type="PERSONAL", tier_code="PERSONAL", charge_opening_fee=True):
    account_type=account_type.upper()
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,opening_fee FROM account_tiers_v2 WHERE code=%s AND account_type=%s AND active=TRUE",(tier_code,account_type))
                tier=cur.fetchone()
                if not tier:
                    raise ValueError("Requested account tier is unavailable.")
                if account_type=="PERSONAL":
                    cur.execute("SELECT id FROM bank_accounts WHERE client_id=%s AND account_type='PERSONAL' AND status<>'CLOSED'",(client_id,))
                    if cur.fetchone():
                        raise ValueError("Client already has a Personal account.")
                else:
                    cur.execute("SELECT COUNT(*) FROM bank_accounts WHERE client_id=%s AND account_type='BUSINESS' AND status<>'CLOSED'",(client_id,))
                    if int(cur.fetchone()[0])>=5:
                        raise ValueError("A client may have a maximum of five active Business accounts.")
                cur.execute("SELECT nextval('muoc_account_number_seq')")
                number=f"MB-{cur.fetchone()[0]:08d}"
                cur.execute("""INSERT INTO bank_accounts(client_id,account_number,account_type,tier_id)
                               VALUES(%s,%s,%s,%s) RETURNING id,account_number""",(client_id,number,account_type,tier[0]))
                account_id,account_number=cur.fetchone()
                opening_fee=int(tier[1] or 0)
                if charge_opening_fee and opening_fee:
                    cur.execute("""SELECT id,balance FROM bank_accounts
                                   WHERE client_id=%s AND status='ACTIVE' AND id<>%s AND balance>=%s
                                   ORDER BY CASE WHEN account_type='PERSONAL' THEN 0 ELSE 1 END,id
                                   LIMIT 1 FOR UPDATE""",(client_id,account_id,opening_fee))
                    source=cur.fetchone()
                    if not source:
                        raise ValueError("Opening this account requires available Emerald in another MineBank account.")
                    cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(opening_fee,source[0]))
                    cur.execute("SELECT nextval('muoc_transaction_seq')")
                    txid=f"MUOC-{_now().year}-{cur.fetchone()[0]:06d}"
                    cur.execute("""INSERT INTO ledger_transactions
                                   (transaction_id,transaction_type,amount,fee,currency,sender_account_id,status,description)
                                   VALUES(%s,'ACCOUNT_OPENING_FEE',%s,0,'Emerald',%s,'COMPLETED',%s)""",
                                (txid,opening_fee,source[0],f"Opening fee for {account_number}"))
                if account_type=="BUSINESS":
                    cur.execute("""INSERT INTO minebank_business_members(account_id,client_id,role)
                                   VALUES(%s,%s,'OWNER') ON CONFLICT(account_id,client_id) DO NOTHING""",(account_id,client_id))
                return account_id,account_number
    finally:
        from .database import release_db_connection
        release_db_connection(conn)

def get_client_accounts(client_id):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.id,a.account_number,a.account_type,t.code,t.display_name,a.balance,a.status,"
                    "a.monthly_outgoing_used,t.monthly_outgoing_limit,t.max_balance,t.credit_enabled "
                    "FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id "
                    "WHERE a.client_id=%s AND a.status<>'CLOSED' ORDER BY a.account_type,a.id",
                    (client_id,),
                )
                return cur.fetchall()
    finally:
        from .database import release_db_connection
        release_db_connection(conn)


def request_tier_change(client_id, account_id, tier_code, requested_credit_limit=None):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT a.id,a.client_id,a.balance,a.account_type,t.code
                               FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
                               WHERE a.id=%s AND a.client_id=%s AND a.status<>'CLOSED'""",(account_id,client_id))
                account=cur.fetchone()
                if not account:
                    raise ValueError("Account not found.")
                cur.execute("""SELECT id,code,account_type,private_or_corporate,eligibility_config
                               FROM account_tiers_v2 WHERE code=%s AND active=TRUE""",(tier_code,))
                tier=cur.fetchone()
                if not tier or tier[2] != account[3]:
                    raise ValueError("Requested tier is unavailable for this account type.")
                payload={"tier_code":tier_code}
                if requested_credit_limit is not None:
                    payload["requested_credit_limit"]=int(requested_credit_limit)
                cur.execute("""INSERT INTO bank_requests_v2(client_id,account_id,request_type,payload)
                               VALUES(%s,%s,'TIER_CHANGE',%s::jsonb) RETURNING id""",
                            (client_id,account_id,__import__('json').dumps(payload)))
                return cur.fetchone()[0]
    finally:
        release_db_connection(conn)


def review_tier_change(request_id, reviewer_id, approve):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT r.id,r.client_id,r.account_id,r.payload,a.balance,a.account_type
                               FROM bank_requests_v2 r JOIN bank_accounts a ON a.id=r.account_id
                               WHERE r.id=%s AND r.request_type='TIER_CHANGE' AND r.status='PENDING' FOR UPDATE""",
                            (request_id,))
                row=cur.fetchone()
                if not row:
                    raise ValueError("Tier request not found.")
                status="APPROVED" if approve else "REJECTED"
                if approve:
                    tier_code=row[3].get("tier_code")
                    cur.execute("""SELECT id,eligibility_config FROM account_tiers_v2
                                   WHERE code=%s AND active=TRUE""",(tier_code,))
                    tier=cur.fetchone()
                    if not tier:
                        raise ValueError("Requested tier is unavailable.")
                    import datetime as _dt
                    cur.execute("SELECT date_of_birth FROM bank_clients WHERE id=%s",(row[1],))
                    dob=cur.fetchone()[0]
                    cfg=tier[1] or {}
                    minimum_age=int(cfg.get("minimum_age",0))
                    if minimum_age and dob:
                        today=_dt.date.today()
                        age=today.year-dob.year-((today.month,today.day)<(dob.month,dob.day))
                        if age<minimum_age:
                            raise ValueError("Client does not meet minimum age eligibility.")
                    if int(row[4]) < int(cfg.get("minimum_balance",0)):
                        raise ValueError("Client does not meet minimum balance eligibility.")
                    cur.execute("""SELECT COUNT(*) FROM ledger_transactions
                                   WHERE (sender_account_id=%s OR recipient_account_id=%s)
                                     AND status='COMPLETED' AND created_at>=CURRENT_TIMESTAMP-INTERVAL '30 days'""",
                                (row[2],row[2]))
                    if int(cur.fetchone()[0]) < int(cfg.get("minimum_operations",0)):
                        raise ValueError("Client does not meet minimum operations eligibility.")
                    cur.execute("SELECT id,max_balance,credit_enabled,default_credit_limit FROM account_tiers_v2 WHERE code=%s",(tier_code,))
                    target=cur.fetchone()
                    if not target:
                        raise ValueError("Requested tier is unavailable.")
                    if target[1] is not None and int(row[4]) > int(target[1]):
                        raise ValueError("Tier downgrade would violate the new maximum account balance.")
                    cur.execute("SELECT credit_limit,status FROM credit_facilities WHERE account_id=%s FOR UPDATE",(row[2],))
                    facility=cur.fetchone()
                    if facility and facility[1]=="ACTIVE" and int(facility[0]) > int(target[3] or 0):
                        if int(row[4]) < 0:
                            new_limit=max(0,int(target[3] or 0))
                            cur.execute("UPDATE credit_facilities SET credit_limit=%s WHERE account_id=%s",(new_limit,row[2]))
                        else:
                            cur.execute("UPDATE credit_facilities SET credit_limit=LEAST(credit_limit,%s) WHERE account_id=%s",(int(target[3] or 0),row[2]))
                    cur.execute("UPDATE bank_accounts SET tier_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                                (target[0],row[2]))
                cur.execute("""UPDATE bank_requests_v2 SET status=%s,reviewed_by=%s,reviewed_at=CURRENT_TIMESTAMP
                               WHERE id=%s""",(status,reviewer_id,request_id))
                return status
    finally:
        release_db_connection(conn)
