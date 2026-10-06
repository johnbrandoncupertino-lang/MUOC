"""MineBank bonus suite: advanced banking features kept outside the core ledger.

The module is intentionally additive. It uses the existing ledger/account/security
services and provides idempotent schema installation for already-live deployments.
"""
import json
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import request, session, jsonify, render_template

from .database import get_db_connection, release_db_connection
from .minebank_core import audit_event, next_transaction_id, create_ledger_transaction, transfer
from .minebank_requests import create_notification

BONUS_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS minebank_business_members (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL CHECK (role IN ('OWNER','EMPLOYEE','READ_ONLY')),
    permissions JSONB NOT NULL DEFAULT '{}'::jsonb,
    spending_limit BIGINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id,client_id)
);
CREATE TABLE IF NOT EXISTS minebank_payroll_batches (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    created_by BIGINT NOT NULL REFERENCES bank_clients(id),
    period_label VARCHAR(40) NOT NULL,
    total_amount BIGINT NOT NULL CHECK(total_amount>0),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','PAID','REJECTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_payroll_items (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES minebank_payroll_batches(id) ON DELETE CASCADE,
    recipient_account_number VARCHAR(32) NOT NULL,
    amount BIGINT NOT NULL CHECK(amount>0),
    description VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS minebank_direct_debits (
    id BIGSERIAL PRIMARY KEY,
    payer_account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    merchant_account_number VARCHAR(32) NOT NULL,
    merchant_name VARCHAR(160) NOT NULL,
    maximum_amount BIGINT NOT NULL CHECK(maximum_amount>0),
    monthly_limit BIGINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    reference VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    payer_account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    merchant_account_number VARCHAR(32) NOT NULL,
    merchant_name VARCHAR(160) NOT NULL,
    amount BIGINT NOT NULL CHECK(amount>0),
    frequency VARCHAR(20) NOT NULL CHECK(frequency IN ('WEEKLY','MONTHLY')),
    next_charge_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','PAUSED','CANCELLED')),
    reference VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_loans (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    client_id BIGINT NOT NULL REFERENCES bank_clients(id),
    amount BIGINT NOT NULL CHECK(amount BETWEEN 5000 AND 10000000),
    interest_bps INTEGER NOT NULL CHECK(interest_bps>=0),
    term_months INTEGER NOT NULL CHECK(term_months BETWEEN 1 AND 120),
    collateral_required BIGINT NOT NULL DEFAULT 0,
    collateral_note VARCHAR(500),
    purpose VARCHAR(500),
    score INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','ACTIVE','PAID','REJECTED','DEFAULTED')),
    outstanding_principal BIGINT NOT NULL DEFAULT 0,
    outstanding_interest BIGINT NOT NULL DEFAULT 0,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_loan_payments (
    id BIGSERIAL PRIMARY KEY,
    loan_id BIGINT NOT NULL REFERENCES minebank_loans(id) ON DELETE CASCADE,
    payer_account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    amount BIGINT NOT NULL CHECK(amount>0),
    principal BIGINT NOT NULL DEFAULT 0,
    interest BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_risk_events (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES bank_accounts(id),
    client_id BIGINT REFERENCES bank_clients(id),
    risk_score INTEGER NOT NULL,
    severity VARCHAR(20) NOT NULL,
    reason VARCHAR(255) NOT NULL,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_trusted_devices (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    device_token_hash VARCHAR(128) NOT NULL,
    label VARCHAR(120) NOT NULL,
    user_agent VARCHAR(500),
    ip_address VARCHAR(64),
    trusted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_security_events (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES bank_clients(id),
    event_type VARCHAR(60) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'INFO',
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_banking_locks (
    client_id BIGINT PRIMARY KEY REFERENCES bank_clients(id) ON DELETE CASCADE,
    reason VARCHAR(255),
    locked_by BIGINT REFERENCES bank_clients(id),
    locked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    unlocked_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_announcements (
    id BIGSERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    message TEXT NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'INFO',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    starts_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ends_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS minebank_private_threads (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    subject VARCHAR(200) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_private_messages (
    id BIGSERIAL PRIMARY KEY,
    thread_id BIGINT NOT NULL REFERENCES minebank_private_threads(id) ON DELETE CASCADE,
    sender_client_id BIGINT REFERENCES bank_clients(id),
    sender_role VARCHAR(20) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_automation_alerts (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES bank_accounts(id),
    client_id BIGINT REFERENCES bank_clients(id),
    alert_type VARCHAR(60) NOT NULL,
    message VARCHAR(500) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'INFO',
    acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS minebank_risk_events_account_idx ON minebank_risk_events(account_id,created_at);
CREATE INDEX IF NOT EXISTS minebank_security_events_client_idx ON minebank_security_events(client_id,created_at);
CREATE INDEX IF NOT EXISTS minebank_subscriptions_due_idx ON minebank_subscriptions(status,next_charge_at);
CREATE INDEX IF NOT EXISTS minebank_direct_debits_active_idx ON minebank_direct_debits(active);
"""

def ensure_bonus_schema():
    conn=get_db_connection()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(BONUS_SCHEMA)
        return True
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        release_db_connection(conn)

def _ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")[:64]

def _json(row):
    return dict(row) if row else None

def _rowdict(cur):
    cols=[d.name for d in cur.description]
    return [dict(zip(cols,r)) for r in cur.fetchall()]

def _active_business(cur, client_id, account_id):
    cur.execute("""SELECT bm.role,bm.permissions,bm.spending_limit,ba.client_id
                   FROM minebank_business_members bm JOIN bank_accounts ba ON ba.id=bm.account_id
                   WHERE bm.client_id=%s AND bm.account_id=%s AND bm.active=TRUE
                     AND ba.account_type='BUSINESS' AND ba.status='ACTIVE'""",(client_id,account_id))
    return cur.fetchone()

def business_permission(client_id, account_id, permission, amount=0):
    conn=get_db_connection()
    if conn is None: return False
    try:
        with conn:
            with conn.cursor() as cur:
                row=_active_business(cur,client_id,account_id)
                if not row: return False
                role,perms,limit,_=row
                if role=="OWNER": return True
                if role=="READ_ONLY": return permission in ("VIEW","STATEMENTS")
                if not (perms or {}).get(permission,False): return False
                return limit is None or amount<=int(limit)
    finally: release_db_connection(conn)

def add_business_member(account_id, actor_id, client_id, role="EMPLOYEE", permissions=None, spending_limit=None):
    if role not in ("OWNER","EMPLOYEE","READ_ONLY"): raise ValueError("Invalid business role.")
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                actor=_active_business(cur,actor_id,account_id)
                if not actor or actor[0]!="OWNER": raise PermissionError("Only the Owner can manage business users.")
                cur.execute("""INSERT INTO minebank_business_members(account_id,client_id,role,permissions,spending_limit)
                               VALUES(%s,%s,%s,%s::jsonb,%s)
                               ON CONFLICT(account_id,client_id) DO UPDATE SET role=EXCLUDED.role,
                               permissions=EXCLUDED.permissions,spending_limit=EXCLUDED.spending_limit,active=TRUE""",
                            (account_id,client_id,role,json.dumps(permissions or {}),spending_limit))
                audit_event(cur,actor_client_id=actor_id,action="BUSINESS_MEMBER_UPDATED",target_type="CLIENT",
                            target_id=client_id,account_id=account_id,ip_address=_ip(),
                            context={"role":role,"permissions":permissions or {},"spending_limit":spending_limit})
    finally: release_db_connection(conn)

def request_loan(client_id, account_id, amount, interest_bps, term_months, purpose="", collateral_required=0, collateral_note=""):
    amount=int(amount)
    if amount<5000 or amount>10000000: raise ValueError("Loan amount must be between 5,000 and 10,000,000 Emerald.")
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT ba.account_type,bc.role FROM bank_accounts ba JOIN bank_clients bc ON bc.id=ba.client_id
                               WHERE ba.id=%s AND ba.client_id=%s AND ba.status='ACTIVE'""",(account_id,client_id))
                row=cur.fetchone()
                if not row: raise ValueError("Account not found.")
                account_type=row[0]
                max_amount=500000 if account_type=="PERSONAL" else 10000000
                if amount>max_amount: raise ValueError("Loan amount exceeds this account's maximum.")
                cur.execute("""SELECT COUNT(*),COALESCE(AVG(balance),0),COALESCE(SUM(monthly_outgoing_used),0)
                               FROM bank_accounts WHERE client_id=%s""",(client_id,))
                cnt,avg_bal,volume=cur.fetchone()
                score=max(0,min(100,int(50+min(float(avg_bal)/1000,20)+min(float(volume)/1000,15)+min(cnt*5,15))))
                cur.execute("""INSERT INTO minebank_loans(account_id,client_id,amount,interest_bps,term_months,
                               collateral_required,collateral_note,purpose,score,outstanding_principal)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0) RETURNING id""",
                            (account_id,client_id,amount,int(interest_bps),int(term_months),int(collateral_required or 0),
                             collateral_note,purpose,score))
                loan_id=cur.fetchone()[0]
                audit_event(cur,actor_client_id=client_id,action="LOAN_REQUESTED",target_type="LOAN",target_id=loan_id,
                            account_id=account_id,ip_address=_ip(),context={"amount":amount,"score":score})
                return loan_id,score
    finally: release_db_connection(conn)

def approve_loan(loan_id, operator_id, approve=True, interest_bps=None, collateral_required=None):
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT account_id,client_id,amount,status,interest_bps,collateral_required FROM minebank_loans WHERE id=%s FOR UPDATE",(loan_id,))
                row=cur.fetchone()
                if not row or row[3]!="PENDING": raise ValueError("Loan is not pending.")
                if approve:
                    ib=int(interest_bps if interest_bps is not None else row[4])
                    coll=int(collateral_required if collateral_required is not None else row[5])
                    due=datetime.now(timezone.utc)+timedelta(days=30*12)
                    cur.execute("""UPDATE minebank_loans SET status='ACTIVE',interest_bps=%s,collateral_required=%s,
                                   outstanding_principal=amount,approved_at=CURRENT_TIMESTAMP,due_at=%s WHERE id=%s""",
                                (ib,coll,due,loan_id))
                    txid=next_transaction_id(cur)
                    create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_DISBURSEMENT",
                                              amount=row[2],recipient_account_id=row[0],status="COMPLETED",
                                              description=f"Loan #{loan_id}")
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(row[2],row[0]))
                    audit_event(cur,actor_client_id=operator_id,action="LOAN_APPROVED",target_type="LOAN",target_id=loan_id,
                                account_id=row[0],transaction_id=txid,ip_address=_ip())
                else:
                    cur.execute("UPDATE minebank_loans SET status='REJECTED' WHERE id=%s",(loan_id,))
                    audit_event(cur,actor_client_id=operator_id,action="LOAN_REJECTED",target_type="LOAN",target_id=loan_id,
                                account_id=row[0],ip_address=_ip())
                create_notification(row[1],"LOAN_UPDATE","Loan #{} {}".format(loan_id,"approved" if approve else "rejected"))
    finally: release_db_connection(conn)

def repay_loan(loan_id, payer_account_id, amount, actor_id):
    amount=int(amount)
    if amount<=0: raise ValueError("Payment must be positive.")
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT client_id,outstanding_principal,outstanding_interest,status FROM minebank_loans WHERE id=%s FOR UPDATE",(loan_id,))
                loan=cur.fetchone()
                if not loan or loan[3]!="ACTIVE": raise ValueError("Loan is not active.")
                cur.execute("SELECT balance,client_id,status FROM bank_accounts WHERE id=%s FOR UPDATE",(payer_account_id,))
                payer=cur.fetchone()
                if not payer or payer[1]!=actor_id or payer[2]!="ACTIVE": raise ValueError("Invalid repayment account.")
                if payer[0]<amount: raise ValueError("Insufficient balance.")
                interest=min(amount,int(loan[2])); principal=amount-interest
                principal=min(principal,int(loan[1]))
                actual=interest+principal
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(actual,payer_account_id))
                txid=next_transaction_id(cur)
                create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_REPAYMENT",amount=actual,
                                          sender_account_id=payer_account_id,status="COMPLETED",description=f"Loan #{loan_id} repayment")
                newp=int(loan[1])-principal; newi=int(loan[2])-interest
                status="PAID" if newp==0 and newi==0 else "ACTIVE"
                cur.execute("UPDATE minebank_loans SET outstanding_principal=%s,outstanding_interest=%s,status=%s WHERE id=%s",
                            (newp,newi,status,loan_id))
                cur.execute("INSERT INTO minebank_loan_payments(loan_id,payer_account_id,amount,principal,interest) VALUES(%s,%s,%s,%s,%s)",
                            (loan_id,payer_account_id,actual,principal,interest))
                audit_event(cur,actor_client_id=actor_id,action="LOAN_REPAYMENT",target_type="LOAN",target_id=loan_id,
                            account_id=payer_account_id,transaction_id=txid,ip_address=_ip())
                return txid
    finally: release_db_connection(conn)

def calculate_risk(account_id, amount=0, recipient_is_new=False):
    conn=get_db_connection()
    if conn is None: return 0,[]
    try:
        with conn:
            with conn.cursor() as cur:
                score=0; reasons=[]
                cur.execute("""SELECT COUNT(*) FROM ledger_transactions WHERE sender_account_id=%s
                               AND created_at>CURRENT_TIMESTAMP-INTERVAL '10 minutes'""",(account_id,))
                velocity=int(cur.fetchone()[0])
                if velocity>=10: score+=35; reasons.append("High transaction velocity.")
                if amount>=50000: score+=25; reasons.append("Large transaction amount.")
                elif amount>=10000: score+=10; reasons.append("Elevated transaction amount.")
                if recipient_is_new: score+=20; reasons.append("New recipient.")
                cur.execute("""SELECT COUNT(*) FROM minebank_risk_events WHERE account_id=%s AND created_at>CURRENT_TIMESTAMP-INTERVAL '30 days'""",(account_id,))
                if int(cur.fetchone()[0])>=3: score+=20; reasons.append("Recent risk history.")
                severity="LOW" if score<30 else ("MEDIUM" if score<60 else "HIGH")
                if score:
                    cur.execute("""INSERT INTO minebank_risk_events(account_id,risk_score,severity,reason,context)
                                   VALUES(%s,%s,%s,%s,%s::jsonb)""",(account_id,score,severity,"; ".join(reasons),json.dumps({"amount":amount})))
                return score,reasons
    finally: release_db_connection(conn)

def emergency_lock(client_id, reason="Client requested emergency banking lock"):
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO minebank_banking_locks(client_id,reason)
                               VALUES(%s,%s) ON CONFLICT(client_id) DO UPDATE SET reason=EXCLUDED.reason,
                               locked_at=CURRENT_TIMESTAMP,unlocked_at=NULL""",(client_id,reason))
                cur.execute("""INSERT INTO minebank_security_events(client_id,event_type,severity,context,ip_address)
                               VALUES(%s,'EMERGENCY_BANKING_LOCK','HIGH',%s::jsonb,%s)""",
                            (client_id,json.dumps({"reason":reason}),_ip()))
                audit_event(cur,actor_client_id=client_id,action="EMERGENCY_BANKING_LOCK",target_type="CLIENT",
                            target_id=client_id,ip_address=_ip(),context={"reason":reason})
                create_notification(client_id,"SECURITY","Emergency banking lock activated. Banking operations are suspended.")
    finally: release_db_connection(conn)

def assert_banking_unlocked(client_id):
    if banking_locked(client_id):
        raise ValueError("Banking operations are temporarily locked for this client.")

def banking_locked(client_id):
    conn=get_db_connection()
    if conn is None: return True
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM minebank_banking_locks WHERE client_id=%s AND unlocked_at IS NULL",(client_id,))
                return bool(cur.fetchone())
    finally: release_db_connection(conn)

def unlock_banking(client_id, operator_id):
    conn=get_db_connection()
    if conn is None: raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE minebank_banking_locks SET unlocked_at=CURRENT_TIMESTAMP WHERE client_id=%s AND unlocked_at IS NULL",(client_id,))
                cur.execute("""INSERT INTO minebank_security_events(client_id,event_type,severity,context,ip_address)
                               VALUES(%s,'EMERGENCY_BANKING_UNLOCK','HIGH',%s::jsonb,%s)""",
                            (client_id,json.dumps({"operator":operator_id}),_ip()))
                audit_event(cur,actor_client_id=operator_id,action="EMERGENCY_BANKING_UNLOCK",target_type="CLIENT",
                            target_id=client_id,ip_address=_ip())
    finally: release_db_connection(conn)

def bank_statistics():
    conn=get_db_connection()
    if conn is None: return {}
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT COUNT(*) FROM bank_clients WHERE role='CLIENT' AND status<>'CLOSED'"""); clients=cur.fetchone()[0]
                cur.execute("""SELECT COUNT(*),COALESCE(SUM(balance),0) FROM bank_accounts WHERE status<>'CLOSED'"""); accounts,balance=cur.fetchone()
                cur.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0) FROM ledger_transactions WHERE created_at>=date_trunc('month',CURRENT_TIMESTAMP)"""); txs,volume=cur.fetchone()
                cur.execute("""SELECT COUNT(*),COALESCE(SUM(outstanding_principal),0) FROM minebank_loans WHERE status='ACTIVE'"""); loans,debt=cur.fetchone()
                cur.execute("""SELECT COUNT(*) FROM minebank_risk_events WHERE severity='HIGH' AND resolved=FALSE"""); risks=cur.fetchone()[0]
                return {"clients":clients,"accounts":accounts,"total_balance":int(balance),"month_transactions":txs,
                        "month_volume":int(volume),"active_loans":loans,"loan_principal":int(debt),"open_high_risk":risks}
    finally: release_db_connection(conn)

def process_due_schedules(limit=100):
    """Process due subscriptions through the canonical transfer ledger."""
    conn=get_db_connection()
    if conn is None: return {"processed":0,"errors":1}
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,payer_account_id,merchant_account_number,amount,frequency,next_charge_at,reference,
                                      (SELECT client_id FROM bank_accounts WHERE id=payer_account_id)
                               FROM minebank_subscriptions
                               WHERE status='ACTIVE' AND next_charge_at<=CURRENT_TIMESTAMP
                               ORDER BY next_charge_at LIMIT %s FOR UPDATE SKIP LOCKED""",(limit,))
                rows=cur.fetchall()
        processed=errors=0
        for sid,account,recipient,amount,freq,next_at,ref,client_id in rows:
            try:
                result=transfer(sender_account_id=account,recipient_account_number=recipient,amount=int(amount),
                                description="Scheduled subscription payment",reference=ref,
                                actor_client_id=client_id,ip_address=None)
                conn2=get_db_connection()
                try:
                    with conn2:
                        with conn2.cursor() as cur2:
                            interval="1 week" if freq=="WEEKLY" else "1 month"
                            cur2.execute("UPDATE minebank_subscriptions SET next_charge_at=next_charge_at+%s::interval WHERE id=%s AND status='ACTIVE'",
                                         (interval,sid))
                finally:
                    release_db_connection(conn2)
                processed+=1
            except Exception as exc:
                errors+=1
                conn2=get_db_connection()
                try:
                    with conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute("""INSERT INTO minebank_automation_alerts(account_id,client_id,alert_type,message,severity)
                                            VALUES(%s,%s,'SUBSCRIPTION_FAILURE',%s,'WARNING')""",
                                         (account,client_id,str(exc)[:500]))
                finally:
                    release_db_connection(conn2)
        return {"processed":processed,"errors":errors}
    finally:
        release_db_connection(conn)

def register_bonus_routes(app):
    ensure_bonus_schema()

    def staff_required(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if session.get("minebank_role") not in ("OPERATOR","ADMIN"):
                return jsonify(error="Bank staff required."),403
            return fn(*args,**kwargs)
        return wrapped

    @app.route("/portal/bonus")
    def minebank_bonus_home():
        if not session.get("minebank_client_id"):
            return jsonify(error="Login required."),401
        cid=session["minebank_client_id"]
        return render_template("minebank_bonus.html", stats=bank_statistics() if session.get("minebank_role")=="ADMIN" else {},
                               locked=banking_locked(cid), announcements=_announcements(cid),
                               portal_active="bonus", is_logged_in=True,
                               is_admin=session.get("minebank_role")=="ADMIN")

    @app.post("/portal/bonus/business/member")
    def minebank_bonus_business_member():
        cid=session.get("minebank_client_id")
        if not cid: return jsonify(error="Login required."),401
        try:
            add_business_member(int(request.form["account_id"]),cid,int(request.form["client_id"]),
                                request.form.get("role","EMPLOYEE"),
                                json.loads(request.form.get("permissions","{}")),
                                int(request.form["spending_limit"]) if request.form.get("spending_limit") else None)
            return jsonify(ok=True)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/loan")
    def minebank_bonus_loan():
        cid=session.get("minebank_client_id")
        try:
            lid,score=request_loan(cid,int(request.form["account_id"]),int(request.form["amount"]),
                                   int(request.form.get("interest_bps",700)),int(request.form.get("term_months",12)),
                                   request.form.get("purpose",""),int(request.form.get("collateral_required",0) or 0),
                                   request.form.get("collateral_note",""))
            return jsonify(ok=True,loan_id=lid,score=score)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/loan/<int:loan_id>/review")
    @staff_required
    def minebank_bonus_loan_review(loan_id):
        try:
            approve_loan(loan_id,session["minebank_client_id"],request.form.get("decision")=="approve",
                         request.form.get("interest_bps"),request.form.get("collateral_required"))
            return jsonify(ok=True)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/loan/<int:loan_id>/repay")
    def minebank_bonus_loan_repay(loan_id):
        try:
            txid=repay_loan(loan_id,int(request.form["account_id"]),int(request.form["amount"]),session["minebank_client_id"])
            return jsonify(ok=True,transaction_id=txid)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/risk/check")
    def minebank_bonus_risk_check():
        try:
            score,reasons=calculate_risk(int(request.form["account_id"]),int(request.form.get("amount",0)),
                                         request.form.get("new_recipient")=="1")
            return jsonify(score=score,reasons=reasons,severity="LOW" if score<30 else ("MEDIUM" if score<60 else "HIGH"))
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/emergency-lock")
    def minebank_bonus_emergency_lock():
        try:
            emergency_lock(session["minebank_client_id"],request.form.get("reason","Client requested emergency banking lock"))
            return jsonify(ok=True)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/emergency-unlock")
    @staff_required
    def minebank_bonus_emergency_unlock():
        try:
            unlock_banking(int(request.form["client_id"]),session["minebank_client_id"])
            return jsonify(ok=True)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/direct-debit")
    def minebank_bonus_direct_debit():
        try:
            conn=get_db_connection()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO minebank_direct_debits(payer_account_id,merchant_account_number,merchant_name,
                                   maximum_amount,monthly_limit,reference) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (int(request.form["account_id"]),request.form["merchant_account_number"],
                                 request.form["merchant_name"],int(request.form["maximum_amount"]),
                                 int(request.form["monthly_limit"]) if request.form.get("monthly_limit") else None,
                                 request.form.get("reference","")))
                    did=cur.fetchone()[0]
            release_db_connection(conn)
            return jsonify(ok=True,id=did)
        except Exception as e: return jsonify(error=str(e)),400

    @app.post("/portal/bonus/subscription")
    def minebank_bonus_subscription():
        try:
            conn=get_db_connection()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO minebank_subscriptions(payer_account_id,merchant_account_number,merchant_name,
                                   amount,frequency,next_charge_at,end_at,reference)
                                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (int(request.form["account_id"]),request.form["merchant_account_number"],
                                 request.form["merchant_name"],int(request.form["amount"]),request.form.get("frequency","MONTHLY"),
                                 request.form["next_charge_at"],request.form.get("end_at") or None,request.form.get("reference","")))
                    sid=cur.fetchone()[0]
            release_db_connection(conn)
            return jsonify(ok=True,id=sid)
        except Exception as e: return jsonify(error=str(e)),400

    @app.get("/admin/minebank/bonus/statistics")
    @staff_required
    def minebank_bonus_statistics():
        return jsonify(bank_statistics())

    @app.post("/admin/minebank/bonus/process-schedules")
    @staff_required
    def minebank_bonus_process():
        return jsonify(process_due_schedules())

    @app.post("/api/internal/minebank/process-schedules")
    def minebank_bonus_cron_process():
        import os
        expected=os.environ.get("MUOC_CRON_TOKEN")
        supplied=request.headers.get("Authorization","")
        if not expected or supplied != "Bearer "+expected:
            return jsonify(error="Unauthorized."),401
        return jsonify(process_due_schedules())

    @app.post("/admin/minebank/bonus/announcement")
    @staff_required
    def minebank_bonus_announcement():
        try:
            conn=get_db_connection()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO minebank_announcements(title,message,severity,starts_at,ends_at)
                                   VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                                (request.form["title"],request.form["message"],request.form.get("severity","INFO"),
                                 request.form.get("starts_at") or datetime.now(timezone.utc),
                                 request.form.get("ends_at") or None))
                    aid=cur.fetchone()[0]
            release_db_connection(conn)
            return jsonify(ok=True,id=aid)
        except Exception as e: return jsonify(error=str(e)),400

    @app.get("/admin/minebank/bonus/loans")
    @staff_required
    def minebank_bonus_loans():
        conn=get_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT id,account_id,client_id,amount,interest_bps,term_months,collateral_required,
                                   purpose,score,status,outstanding_principal,outstanding_interest,requested_at
                                   FROM minebank_loans ORDER BY requested_at DESC LIMIT 200""")
                    return jsonify(loans=_rowdict(cur))
        finally: release_db_connection(conn)

def _announcements(client_id):
    conn=get_db_connection()
    if conn is None: return []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,title,message,severity,starts_at,ends_at FROM minebank_announcements
                               WHERE active=TRUE AND starts_at<=CURRENT_TIMESTAMP
                               AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP)
                               ORDER BY starts_at DESC LIMIT 10""")
                return _rowdict(cur)
    finally: release_db_connection(conn)

# Compatibility alias used by app.py.
install_bonus_routes = register_bonus_routes
