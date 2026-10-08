"""MineBank CrediCheck: auditable in-game credit assessment and Dynamic CashLine state.

CrediCheck is a MineBank game-system/risk ledger. It never exposes its internal
score to customers. Credit-product decisions remain reviewable and auditable by
MineBank administration; this module supplies the scoring, capacity snapshots,
product-state rules and history used by the portal.
"""
from datetime import datetime, timezone
from .database import execute_query, execute_query_dict, ensure_transaction_schema

_READY = False
MIN_SCORE = 0

def ensure_credicheck_schema():
    global _READY
    if _READY:
        return True
    try:
        # CrediCheck operates on ledger/CashLine fields that were added over
        # time. Bring those lightweight transaction extensions up first.
        ensure_transaction_schema()
        execute_query("""CREATE TABLE IF NOT EXISTS credicheck_profiles (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT UNIQUE NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            score INTEGER NOT NULL DEFAULT 5,
            risk_level VARCHAR(32) NOT NULL DEFAULT 'GOOD',
            credit_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            last_evaluated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS credicheck_events (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            event_type VARCHAR(80) NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            previous_score INTEGER NOT NULL,
            new_score INTEGER NOT NULL,
            reference_type VARCHAR(60),
            reference_id VARCHAR(120),
            source VARCHAR(40) NOT NULL DEFAULT 'SYSTEM',
            description VARCHAR(1000),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS credicheck_decisions (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            product_type VARCHAR(40) NOT NULL,
            requested_amount BIGINT NOT NULL DEFAULT 0,
            decision VARCHAR(32) NOT NULL,
            score_snapshot INTEGER NOT NULL,
            risk_snapshot VARCHAR(32) NOT NULL,
            debt_snapshot BIGINT NOT NULL DEFAULT 0,
            capacity_snapshot BIGINT NOT NULL DEFAULT 0,
            decision_type VARCHAR(32) NOT NULL DEFAULT 'ASSESSMENT',
            reason VARCHAR(1500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS credicheck_overrides (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            admin_id BIGINT NOT NULL REFERENCES bank_clients(id),
            override_type VARCHAR(50) NOT NULL,
            previous_value VARCHAR(200),
            new_value VARCHAR(200),
            reason VARCHAR(1000) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS dynamic_cashlines (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
            status VARCHAR(24) NOT NULL DEFAULT 'INACTIVE',
            monthly_fee INTEGER NOT NULL DEFAULT 20,
            interest_annual_bps INTEGER NOT NULL DEFAULT 1830,
            activated_at TIMESTAMPTZ,
            last_interest_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            activated_by BIGINT REFERENCES bank_clients(id),
            closed_by BIGINT REFERENCES bank_clients(id),
            UNIQUE(account_id)
        )""", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS credicheck_product_access (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            product_type VARCHAR(40) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'AVAILABLE',
            reason VARCHAR(1000),
            updated_by BIGINT REFERENCES bank_clients(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(client_id, product_type)
        )""", fetch=False, commit=True)
        _READY = True
        return True
    except Exception as exc:
        print(f"CrediCheck schema warning: {type(exc).__name__}: {exc}")
        return False

def _risk(score):
    score = int(score)
    if score <= 5: return 'GOOD'
    if score <= 10: return 'STANDARD'
    if score <= 15: return 'MODERATE'
    if score <= 20: return 'HIGH'
    return 'NO_CREDIT'

def get_profile(client_id):
    ensure_credicheck_schema()
    rows = execute_query_dict("SELECT * FROM credicheck_profiles WHERE client_id=%s", (client_id,))
    if rows:
        return rows[0]
    execute_query("""INSERT INTO credicheck_profiles(client_id,score,risk_level,credit_status)
                     VALUES(%s,5,'GOOD','ACTIVE') ON CONFLICT(client_id) DO NOTHING""",
                  (client_id,), commit=True)
    return execute_query_dict("SELECT * FROM credicheck_profiles WHERE client_id=%s", (client_id,))[0]

def add_event(client_id, event_type, points, description='', reference_type=None,
              reference_id=None, source='SYSTEM'):
    ensure_credicheck_schema()
    current = get_profile(client_id)
    previous = int(current['score'])
    new_score = max(MIN_SCORE, previous + int(points))
    execute_query("""UPDATE credicheck_profiles
                     SET score=%s,risk_level=%s,last_evaluated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                     WHERE client_id=%s""",
                  (new_score,_risk(new_score),client_id), commit=True)
    execute_query("""INSERT INTO credicheck_events
                     (client_id,event_type,points,previous_score,new_score,reference_type,reference_id,source,description)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (client_id,event_type,int(points),previous,new_score,reference_type,
                   str(reference_id) if reference_id is not None else None,source,description[:1000]),
                  commit=True)
    return get_profile(client_id)

def debt_snapshot(account_id):
    rows = execute_query("""SELECT COALESCE(SUM(
        CASE
          WHEN l.transaction_type='TRANSFER' AND l.transfer_kind='CASHLINE'
               AND l.status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')
               AND l.sender_account_id=%s THEN l.amount+l.fee
          WHEN l.transaction_type='CREDIT_INTEREST' AND l.status='COMPLETED'
               AND l.recipient_account_id=%s THEN l.amount
          WHEN l.transaction_type='CREDIT_FEE' AND l.status='COMPLETED'
               AND l.recipient_account_id=%s THEN l.amount
          WHEN l.transaction_type='CREDIT_REPAYMENT' AND l.status='COMPLETED'
               AND l.recipient_account_id=%s THEN -l.amount
          ELSE 0 END),0)
        FROM ledger_transactions l
        WHERE l.sender_account_id=%s OR l.recipient_account_id=%s""",
        (account_id,account_id,account_id,account_id,account_id,account_id))
    return max(0,int(rows[0][0] or 0)) if rows else 0

def base_capacity(account):
    values = {
        'PERSONAL': 1000,
        'PERSONAL_PRO': 3000,
        'PERSONAL_PRIVATE': 10000,
        'BUSINESS': 5000,
        'BUSINESS_PRO': 25000,
        'CORPORATE': 100000,
    }
    return values.get(str(account.get('tier_code') or '').upper(), 0)

def automatic_credit_eligibility(client_id, account, product_type, requested_amount=0):
    """Return whether a request satisfies MineBank's automatic-credit rules.

    Automatic approval is limited to fixed Standard CashLine requests up to
    5,000 Emerald and Loans up to 20,000 Emerald.  Dynamic CashLine is never
    auto-approved by this helper.
    """
    ensure_credicheck_schema()
    amount = int(requested_amount or 0)
    product = str(product_type or "").upper()
    profile = get_profile(client_id)
    reasons = []

    if not account:
        reasons.append("No account was selected.")
    else:
        if str(account.get("status") or "").upper() not in {"ACTIVE", "ENABLED"}:
            reasons.append("The account is not active.")

    # CrediCheck access boundary: Good/Standard customers (0-10) only.
    if int(profile.get("score") or 0) > 10:
        reasons.append("CrediCheck credit eligibility is currently restricted.")
    if str(profile.get("credit_status") or "").upper() != "ACTIVE":
        reasons.append("Credit access is not currently active.")

    # Explicit product blocks/overrides always win over automatic approval.
    access = execute_query_dict(
        "SELECT status FROM credicheck_product_access WHERE client_id=%s AND product_type=%s",
        (client_id, product),
    )
    if access and str(access[0].get("status") or "").upper() not in {"AVAILABLE", "ACTIVE"}:
        reasons.append("This credit product is currently restricted.")

    if product == "CASHLINE":
        if amount < 300 or amount > 5000:
            reasons.append("Automatic Standard CashLine approval is limited to 300-5,000 Emerald.")
        if account:
            dynamic = execute_query_dict(
                "SELECT id FROM dynamic_cashlines WHERE account_id=%s AND status='ACTIVE'",
                (account["id"],),
            )
            if dynamic:
                reasons.append("Dynamic CashLine is active on this account.")
            facility = execute_query_dict(
                "SELECT status FROM credit_facilities WHERE account_id=%s AND status='ACTIVE'",
                (account["id"],),
            )
            if facility:
                reasons.append("An active Standard CashLine already exists on this account.")
    elif product == "LOAN":
        if amount <= 0 or amount > 20000:
            reasons.append("Automatic Loan approval is limited to Loans up to 20,000 Emerald.")
        # Any overdue active Loan blocks automatic approval.
        overdue = execute_query(
            "SELECT COUNT(*) FROM minebank_loans WHERE client_id=%s AND status='ACTIVE' AND next_due_date < CURRENT_DATE",
            (client_id,),
        )
        if overdue and int(overdue[0][0] or 0) > 0:
            reasons.append("There is an overdue Loan repayment.")
        # A defaulted Loan is a permanent block until administration restores access.
        defaulted = execute_query(
            "SELECT COUNT(*) FROM minebank_loans WHERE client_id=%s AND status='DEFAULTED'",
            (client_id,),
        )
        if defaulted and int(defaulted[0][0] or 0) > 0:
            reasons.append("A previous Loan is in default.")
    else:
        reasons.append("This product is not eligible for automatic approval.")

    return {"eligible": not reasons, "reasons": reasons, "score": int(profile.get("score") or 0)}

def assess(client_id, account, product_type, requested_amount=0):
    """Create a transparent internal assessment snapshot for Admin review."""
    profile = get_profile(client_id)
    score = int(profile['score'])
    risk = _risk(score)
    debt = debt_snapshot(account['id']) if account else 0
    base = base_capacity(account or {})
    multiplier = {0:1.50,1:1.50,2:1.50,3:1.25,4:1.25,5:1.25,6:1.0,7:1.0,8:.75,9:.75,10:.75}.get(score,0)
    capacity = int(base * multiplier)
    if debt > max(0, int((account or {}).get('balance') or 0)):
        capacity = int(capacity * .5)
    if score > 20:
        capacity = 0
    requested = int(requested_amount or 0)
    if risk in ('GOOD','STANDARD') and capacity >= requested and requested > 0:
        decision = 'RECOMMENDED'
        reason = 'MineBank rules indicate the request fits the current game-system capacity snapshot.'
    elif risk in ('HIGH','NO_CREDIT') or capacity == 0:
        decision = 'NOT_RECOMMENDED'
        reason = 'Current CrediCheck status indicates restricted credit access.'
    else:
        decision = 'REVIEW_REQUIRED'
        reason = 'The request requires additional administrative review under the current rules.'
    execute_query("""INSERT INTO credicheck_decisions
        (client_id,product_type,requested_amount,decision,score_snapshot,risk_snapshot,debt_snapshot,capacity_snapshot,decision_type,reason)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'ASSESSMENT',%s)""",
        (client_id,product_type,int(requested),decision,score,risk,debt,capacity,reason),commit=True)
    return {'decision':decision,'reason':reason,'score':score,'risk':risk,
            'debt':debt,'capacity':capacity}

def set_score(client_id, admin_id, new_score, reason):
    ensure_credicheck_schema()
    current = get_profile(client_id)
    value=max(MIN_SCORE,int(new_score))
    execute_query("""UPDATE credicheck_profiles SET score=%s,risk_level=%s,updated_at=CURRENT_TIMESTAMP
                     WHERE client_id=%s""",(value,_risk(value),client_id),commit=True)
    execute_query("""INSERT INTO credicheck_overrides
                     (client_id,admin_id,override_type,previous_value,new_value,reason)
                     VALUES(%s,%s,'SCORE',%s,%s,%s)""",
                  (client_id,admin_id,str(current['score']),str(value),reason[:1000]),commit=True)
    return get_profile(client_id)

def set_product_access(client_id, admin_id, product_type, status, reason):
    ensure_credicheck_schema()
    execute_query("""INSERT INTO credicheck_product_access(client_id,product_type,status,reason,updated_by)
                     VALUES(%s,%s,%s,%s,%s)
                     ON CONFLICT(client_id,product_type) DO UPDATE SET status=EXCLUDED.status,
                     reason=EXCLUDED.reason,updated_by=EXCLUDED.updated_by,updated_at=CURRENT_TIMESTAMP""",
                  (client_id,product_type,status,reason[:1000],admin_id),commit=True)

def activate_dynamic_cashline(client_id, account_id, admin_id):
    ensure_credicheck_schema()
    profile=get_profile(client_id)
    if int(profile['score']) > 10 or profile['credit_status'] != 'ACTIVE':
        raise ValueError('Customer is not currently eligible for Dynamic CashLine review.')
    existing=execute_query_dict("SELECT * FROM credit_facilities WHERE account_id=%s AND status='ACTIVE'",(account_id,))
    if existing and debt_snapshot(account_id)>0:
        raise ValueError('Standard CashLine must be fully repaid before Dynamic CashLine can be activated.')
    execute_query("""INSERT INTO dynamic_cashlines(client_id,account_id,status,monthly_fee,interest_annual_bps,activated_at,activated_by)
                     VALUES(%s,%s,'ACTIVE',20,CASE WHEN (SELECT account_type FROM bank_accounts WHERE id=%s)='PERSONAL' THEN 1830 ELSE 2370 END,CURRENT_TIMESTAMP,%s)
                     ON CONFLICT(account_id) DO UPDATE SET status='ACTIVE',closed_at=NULL,activated_at=CURRENT_TIMESTAMP,activated_by=%s""",
                  (client_id,account_id,account_id,admin_id,admin_id),commit=True)
    if existing:
        execute_query("UPDATE credit_facilities SET status='CLOSED' WHERE account_id=%s AND status='ACTIVE'",(account_id,),commit=True)
    execute_query("ALTER TABLE dynamic_cashlines ADD COLUMN IF NOT EXISTS last_interest_at TIMESTAMPTZ",fetch=False,commit=True)
    set_product_access(client_id,admin_id,'DYNAMIC_CASHLINE','ACTIVE','Activated by MineBank administration.')
    
def close_dynamic_cashline(client_id, account_id, admin_id):
    ensure_credicheck_schema()
    if debt_snapshot(account_id)>0:
        raise ValueError('Dynamic CashLine must be fully repaid before it can be closed.')
    execute_query("""UPDATE dynamic_cashlines SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,closed_by=%s
                     WHERE client_id=%s AND account_id=%s AND status='ACTIVE'""",(admin_id,client_id,account_id),commit=True)
    set_product_access(client_id,admin_id,'DYNAMIC_CASHLINE','AVAILABLE','Dynamic CashLine closed.')

def list_events(client_id,limit=100):
    ensure_credicheck_schema()
    return execute_query_dict("SELECT * FROM credicheck_events WHERE client_id=%s ORDER BY created_at DESC LIMIT %s",(client_id,limit))
