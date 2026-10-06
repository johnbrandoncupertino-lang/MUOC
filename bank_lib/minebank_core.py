"""MineBank v2 financial engine.

All monetary values are integer Emeralds. Mutations are ledger-first and run
inside database transactions. Web/API layers perform identity and PIN checks.
"""
from datetime import datetime, timezone

from .database import get_db_connection, release_db_connection

PENDING_APPROVAL_THRESHOLD = 5000
STANDARD_CREDIT_INTEREST_MONTHLY_BPS = 700
CURRENCY = "Emerald"


def _now():
    return datetime.now(timezone.utc)



def _assert_banking_unlocked(cur, client_id):
    cur.execute("SELECT 1 FROM minebank_banking_locks WHERE client_id=%s AND unlocked_at IS NULL", (client_id,))
    if cur.fetchone():
        raise ValueError("Banking operations are temporarily locked for this client.")

def _lock_account(cur, account_id):
    cur.execute("""SELECT id,client_id,account_number,account_type,tier_id,balance,status,
                          monthly_outgoing_used,monthly_outgoing_period,last_outgoing_at
                   FROM bank_accounts WHERE id=%s FOR UPDATE""", (account_id,))
    return cur.fetchone()


def _lock_recipient(cur, account_number):
    cur.execute("""SELECT id,client_id,account_number,account_type,tier_id,balance,status,
                          monthly_outgoing_used,monthly_outgoing_period,last_outgoing_at
                   FROM bank_accounts WHERE account_number=%s FOR UPDATE""", (account_number,))
    return cur.fetchone()


def audit_event(cur, *, actor_client_id=None, action, target_type=None, target_id=None,
                account_id=None, transaction_id=None, ip_address=None, context=None):
    cur.execute("""INSERT INTO audit_events
                   (actor_client_id,action,target_type,target_id,account_id,transaction_id,ip_address,context)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (actor_client_id,action,target_type,target_id,account_id,transaction_id,
                 ip_address,__import__('json').dumps(context or {})))


def next_transaction_id(cur):
    cur.execute("SELECT nextval('muoc_transaction_seq')")
    return f"MUOC-{_now().year}-{cur.fetchone()[0]:06d}"


def create_ledger_transaction(cur, *, transaction_id, transaction_type, amount, fee=0,
                              currency=CURRENCY, sender_account_id=None,
                              recipient_account_id=None, status="COMPLETED",
                              description=None, reference_id=None, metadata=None):
    cur.execute(
        """INSERT INTO ledger_transactions
           (transaction_id,transaction_type,amount,fee,currency,sender_account_id,
            recipient_account_id,status,description,reference_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (transaction_id,transaction_type,amount,fee,currency,sender_account_id,
         recipient_account_id,status,description,reference_id),
    )
    return cur.fetchone()[0]


def _reset_month_if_needed(cur, account):
    period = _now().strftime("%Y-%m")
    if account[8] != period:
        cur.execute("UPDATE bank_accounts SET monthly_outgoing_used=0,monthly_outgoing_period=%s WHERE id=%s",
                    (period, account[0]))
        return 0, period
    return int(account[7]), period


def _fee_for_transfer(cur, account, amount):
    cur.execute(
        """SELECT percentage_bps,fixed_amount FROM fee_rules
           WHERE active=TRUE AND transaction_type='TRANSFER'
             AND (account_type IS NULL OR account_type=%s)
             AND (tier_code IS NULL OR tier_code=(SELECT code FROM account_tiers_v2 WHERE id=%s))
             AND (min_amount IS NULL OR %s>=min_amount)
             AND (max_amount IS NULL OR %s<=max_amount)
           ORDER BY priority ASC,id ASC LIMIT 1""",
        (account[3],account[4],amount,amount),
    )
    rule = cur.fetchone()
    if not rule:
        return 0
    return (amount * int(rule[0] or 0) + 9999) // 10000 + int(rule[1] or 0)


def get_credit_limit(cur, account_id):
    cur.execute("SELECT credit_limit FROM credit_facilities WHERE account_id=%s AND status='ACTIVE'",
                (account_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def transfer(*, sender_account_id, recipient_account_number, amount,
             description=None, reference=None, currency=CURRENCY, idempotency_key=None, actor_client_id=None, ip_address=None):
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError("Transfer amount must be a positive integer Emerald amount.")
    if not recipient_account_number:
        raise ValueError("Recipient account number is required.")

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                if idempotency_key:
                    cur.execute("""SELECT transaction_id FROM transfer_idempotency
                                   WHERE idempotency_key=%s FOR UPDATE""", (idempotency_key,))
                    existing = cur.fetchone()
                    if existing:
                        cur.execute("SELECT transaction_id,status,amount,fee FROM ledger_transactions WHERE transaction_id=%s",
                                    (existing[0],))
                        row = cur.fetchone()
                        return {"transaction_id": row[0], "status": row[1], "amount": row[2], "fee": row[3],
                                "idempotent_replay": True}

                if actor_client_id is not None:
                    _assert_banking_unlocked(cur, actor_client_id)
                sender = _lock_account(cur, sender_account_id)
                recipient = _lock_recipient(cur, recipient_account_number)
                if not sender or not recipient:
                    raise ValueError("Sender or recipient account not found.")
                if sender[0] == recipient[0]:
                    raise ValueError("Self-transfers are not allowed.")
                if sender[6] != "ACTIVE":
                    raise ValueError("Sender account is not active.")
                if recipient[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Recipient account cannot receive transfers.")

                used, period = _reset_month_if_needed(cur, sender)
                cur.execute("SELECT monthly_outgoing_limit FROM account_tiers_v2 WHERE id=%s", (sender[4],))
                monthly_limit = cur.fetchone()[0]
                if monthly_limit is not None and used + amount > int(monthly_limit):
                    raise ValueError("Monthly outgoing transfer limit exceeded.")
                if sender[9] and (_now() - sender[9]).total_seconds() < 30:
                    raise ValueError("Outgoing transfers require a 30-second cooldown.")

                fee = _fee_for_transfer(cur, sender, amount)
                total = amount + fee
                credit_limit = get_credit_limit(cur, sender[0])
                if int(sender[5]) - total < -credit_limit:
                    raise ValueError("Insufficient available balance/credit.")

                risk_score = 0
                risk_reasons = []
                try:
                    cur.execute("""SELECT COUNT(*) FROM ledger_transactions
                                   WHERE sender_account_id=%s AND created_at>CURRENT_TIMESTAMP-INTERVAL '10 minutes'""",
                                (sender[0],))
                    if int(cur.fetchone()[0]) >= 10:
                        risk_score += 35
                        risk_reasons.append("High transaction velocity")
                    if amount >= 50000:
                        risk_score += 25
                        risk_reasons.append("Large transaction amount")
                    elif amount >= 10000:
                        risk_score += 10
                        risk_reasons.append("Elevated transaction amount")
                    cur.execute("""SELECT COUNT(*) FROM ledger_transactions
                                   WHERE sender_account_id=%s AND recipient_account_id=%s""",(sender[0],recipient[0]))
                    if int(cur.fetchone()[0]) == 0:
                        risk_score += 20
                        risk_reasons.append("New recipient")
                    if risk_score:
                        cur.execute("""INSERT INTO minebank_risk_events(account_id,client_id,risk_score,severity,reason,context)
                                       VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
                                    (sender[0],sender[1],risk_score,
                                     "HIGH" if risk_score>=60 else ("MEDIUM" if risk_score>=30 else "LOW"),
                                     "; ".join(risk_reasons),__import__('json').dumps({"amount":amount})))
                except Exception:
                    pass
                status = "PENDING_APPROVAL" if amount >= PENDING_APPROVAL_THRESHOLD or risk_score >= 60 else "COMPLETED"
                txid = next_transaction_id(cur)
                cur.execute("""UPDATE bank_accounts SET balance=balance-%s,monthly_outgoing_used=%s,
                               last_outgoing_at=CURRENT_TIMESTAMP,monthly_outgoing_period=%s,
                               updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                            (total,used+amount,period,sender[0]))
                if status == "COMPLETED":
                    cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s", (recipient[4],))
                    max_balance=cur.fetchone()[0]
                    if max_balance is not None and int(recipient[5])+amount>int(max_balance):
                        raise ValueError("Recipient account balance limit exceeded.")
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                                (amount,recipient[0]))
                ledger_id = create_ledger_transaction(
                    cur,transaction_id=txid,transaction_type="TRANSFER",amount=amount,fee=fee,
                    currency=currency,sender_account_id=sender[0],recipient_account_id=recipient[0],
                    status=status,description=description,reference_id=reference)
                if idempotency_key:
                    cur.execute("INSERT INTO transfer_idempotency(idempotency_key,client_id,transaction_id) VALUES(%s,%s,%s)",
                                (idempotency_key,sender[1],txid))
                audit_event(cur,actor_client_id=actor_client_id,action="TRANSFER_CREATED",target_type="TRANSACTION",target_id=txid,account_id=sender[0],transaction_id=txid,ip_address=ip_address,context={"recipient_account":recipient[2],"status":status})
                return {"transaction_id":txid,"ledger_id":ledger_id,"status":status,"amount":amount,"fee":fee}
    finally:
        release_db_connection(conn)


def approve_transfer(transaction_id, actor_user_id, ip_address=None):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,recipient_account_id,amount,status FROM ledger_transactions
                               WHERE transaction_id=%s FOR UPDATE""", (transaction_id,))
                tx = cur.fetchone()
                if not tx or tx[3] != "PENDING_APPROVAL":
                    raise ValueError("Pending transfer not found.")
                recipient = _lock_account(cur, tx[1])
                if not recipient or recipient[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Recipient account cannot receive the transfer.")
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (tx[2],tx[1]))
                cur.execute("""UPDATE ledger_transactions SET status='COMPLETED',approved_by=%s,
                               approved_at=CURRENT_TIMESTAMP WHERE id=%s""", (actor_user_id,tx[0]))
                audit_event(cur,actor_client_id=actor_user_id,action="TRANSFER_APPROVED",target_type="TRANSACTION",target_id=transaction_id,account_id=tx[1],transaction_id=transaction_id,ip_address=ip_address)
                return transaction_id
    finally:
        release_db_connection(conn)


def reject_transfer(transaction_id, actor_user_id, reason=None, ip_address=None):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,sender_account_id,amount,fee,status FROM ledger_transactions
                               WHERE transaction_id=%s FOR UPDATE""", (transaction_id,))
                tx = cur.fetchone()
                if not tx or tx[4] != "PENDING_APPROVAL":
                    raise ValueError("Pending transfer not found.")
                sender = _lock_account(cur, tx[1])
                if not sender:
                    raise ValueError("Sender account not found.")
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (tx[2]+tx[3],tx[2],tx[1]))
                cur.execute("""UPDATE ledger_transactions SET status='REJECTED',approved_by=%s,
                               approved_at=CURRENT_TIMESTAMP,description=COALESCE(description,'') || %s
                               WHERE id=%s""", (actor_user_id, f" [Rejected: {reason or 'No reason supplied'}]",tx[0]))
                audit_event(cur,actor_client_id=actor_user_id,action="TRANSFER_REJECTED",target_type="TRANSACTION",target_id=transaction_id,account_id=tx[1],transaction_id=transaction_id,ip_address=ip_address,context={"reason":reason or ""})
                return transaction_id
    finally:
        release_db_connection(conn)


def deposit(account_id, amount, actor_user_id=None, description=None):
    if not isinstance(amount,int) or amount <= 0:
        raise ValueError("Deposit must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if account: _assert_banking_unlocked(cur, account[1])
                if not account or account[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Account cannot receive deposits.")
                cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s",(account[4],))
                max_balance=cur.fetchone()[0]
                if max_balance is not None and account[5]+amount>int(max_balance):
                    raise ValueError("Account balance limit exceeded.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id))
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="DEPOSIT",amount=amount,
                                              recipient_account_id=account_id,description=description)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount,"fee":0}
    finally:
        release_db_connection(conn)


def withdraw(account_id, amount, actor_user_id=None, description=None):
    if not isinstance(amount,int) or amount<=0:
        raise ValueError("Withdrawal must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if account: _assert_banking_unlocked(cur, account[1])
                if not account or account[6]!="ENABLED":
                    raise ValueError("Account is not active.")
                credit=get_credit_limit(cur,account_id)
                if account[5]-amount < -credit:
                    raise ValueError("Insufficient available balance/credit.")
                cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s",(account[4],))
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id))
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="WITHDRAWAL",amount=amount,
                                              sender_account_id=account_id,description=description)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount,"fee":0}
    finally:
        release_db_connection(conn)


def repay_credit(account_id, amount, source_account_id):
    if not isinstance(amount,int) or amount<=0:
        raise ValueError("Repayment must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                debt_account=_lock_account(cur,account_id)
                if debt_account: _assert_banking_unlocked(cur, debt_account[1])
                source=_lock_account(cur,source_account_id)
                if not debt_account or not source:
                    raise ValueError("Account not found.")
                if debt_account[1] != source[1]:
                    raise ValueError("Repayment source must belong to the same client.")
                debt=max(0,-int(debt_account[5]))
                if debt<=0:
                    raise ValueError("Account has no outstanding credit debt.")
                if amount>debt:
                    raise ValueError("Repayment exceeds outstanding debt.")
                if int(source[5]) < amount:
                    raise ValueError("Source account does not have enough positive balance.")
                if source_account_id == account_id:
                    raise ValueError("Repayment requires a separate source account.")
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (amount,source_account_id))
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (amount,account_id))
                txid=next_transaction_id(cur)
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_REPAYMENT",
                                              amount=amount,sender_account_id=source_account_id,
                                              recipient_account_id=account_id)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount}
    finally:
        release_db_connection(conn)


def draw_credit(account_id, amount, actor_client_id=None, ip_address=None):
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError("Credit draw must be a positive integer Emerald amount.")
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account = _lock_account(cur, account_id)
                if account: _assert_banking_unlocked(cur, account[1])
                if not account or account[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Account is not active.")
                if actor_client_id is not None and account[1] != actor_client_id:
                    raise ValueError("Account does not belong to the logged-in client.")
                cur.execute("""SELECT credit_limit,status FROM credit_facilities
                               WHERE account_id=%s FOR UPDATE""", (account_id,))
                facility = cur.fetchone()
                if not facility or facility[1] != "ACTIVE":
                    raise ValueError("No active credit facility is available.")
                available = int(facility[0]) + min(0, int(account[5]))
                if amount > available:
                    raise ValueError(f"Insufficient available credit. Available: {max(0, available)} Emerald.")
                cur.execute("""UPDATE bank_accounts
                               SET balance=balance-%s, updated_at=CURRENT_TIMESTAMP
                               WHERE id=%s""", (amount, account_id))
                txid = next_transaction_id(cur)
                lid = create_ledger_transaction(
                    cur, transaction_id=txid, transaction_type="CREDIT_DRAW",
                    amount=amount, recipient_account_id=account_id,
                    description="Credit draw"
                )
                audit_event(cur, actor_client_id, "CREDIT_DRAW", "ACCOUNT", str(account_id),
                            account_id=account_id, transaction_id=txid, ip_address=ip_address,
                            context={"amount": amount})
                return {"transaction_id": txid, "ledger_id": lid, "status": "COMPLETED",
                        "amount": amount, "available_credit": max(0, available - amount)}
    finally:
        release_db_connection(conn)


def activate_credit(account_id, requested_limit):
    if not isinstance(requested_limit,int) or requested_limit<=0:
        raise ValueError("Credit limit must be a positive integer.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if account: _assert_banking_unlocked(cur, account[1])
                if not account or account[6]!="ENABLED":
                    raise ValueError("Account is not active.")
                cur.execute("SELECT credit_enabled,default_credit_limit,private_or_corporate FROM account_tiers_v2 WHERE id=%s",
                            (account[4],))
                tier=cur.fetchone()
                if not tier or not tier[0]:
                    raise ValueError("This account tier does not support credit.")
                cur.execute("SELECT id FROM credit_facilities WHERE account_id=%s FOR UPDATE",(account_id,))
                existing=cur.fetchone()
                fee=credit_activation_fee(requested_limit)
                if existing:
                    cur.execute("UPDATE credit_facilities SET credit_limit=%s,activation_fee_monthly=%s,status='ACTIVE' WHERE account_id=%s",
                                (requested_limit,fee,account_id))
                else:
                    cur.execute("""INSERT INTO credit_facilities(account_id,credit_limit,interest_monthly_bps,
                                   activation_fee_monthly,status) VALUES(%s,%s,%s,%s,'ACTIVE')""",
                                (account_id,requested_limit,STANDARD_CREDIT_INTEREST_MONTHLY_BPS,fee))
                return {"account_id":account_id,"credit_limit":requested_limit,"monthly_activation_fee":fee,
                        "interest_monthly_bps":STANDARD_CREDIT_INTEREST_MONTHLY_BPS,"status":"ACTIVE"}
    finally:
        release_db_connection(conn)


def freeze_overdue_credit(account_id):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if not account:
                    raise ValueError("Account not found.")
                debt=max(0,-int(account[5]))
                if debt<=0:
                    return {"status":"CURRENT"}
                cur.execute("""UPDATE credit_facilities SET status='SUSPENDED',overdue_since=COALESCE(overdue_since,CURRENT_TIMESTAMP)
                               WHERE account_id=%s AND status='ACTIVE'""",(account_id,))
                cur.execute("UPDATE bank_accounts SET status='FROZEN',updated_at=CURRENT_TIMESTAMP WHERE id=%s",(account_id,))
                cur.execute("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                               VALUES(%s,%s,'CREDIT_OVERDUE','Credit line suspended',
                               'Credit debt was not repaid within the required period; the account is frozen pending bank intervention.')""",
                            (account[1],account_id))
                return {"status":"FROZEN","account_id":account_id}
    finally:
        release_db_connection(conn)


def chargeback(transaction_id, actor_user_id, reason):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,sender_account_id,recipient_account_id,amount,fee,status
                               FROM ledger_transactions WHERE transaction_id=%s FOR UPDATE""",(transaction_id,))
                tx=cur.fetchone()
                if not tx or tx[4] not in ("COMPLETED",):
                    raise ValueError("Only completed transactions can be charged back.")
                if tx[1] is None or tx[2] is None:
                    raise ValueError("Transaction is not reversible as a transfer.")
                recipient=_lock_account(cur,tx[2])
                if not recipient:
                    raise ValueError("Recipient account not found.")
                # Recipient may become negative; the bank is notified via the
                # audit/event layer added below.
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (tx[3],tx[2]))
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (tx[3],tx[1]))
                reversal=next_transaction_id(cur)
                lid=create_ledger_transaction(cur,transaction_id=reversal,transaction_type="CHARGEBACK",
                                              amount=tx[3],sender_account_id=tx[2],recipient_account_id=tx[1],
                                              description=f"Reversal of {transaction_id}: {reason}")
                return {"transaction_id":reversal,"ledger_id":lid,"status":"COMPLETED","amount":tx[3]}
    finally:
        release_db_connection(conn)


def credit_activation_fee(credit_limit):
    return 40 if int(credit_limit)>5000 else 10


def accrue_monthly_credit_interest(account_id):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if not account:
                    raise ValueError("Account not found.")
                debt=max(0,-int(account[5]))
                if debt<=0:
                    return {"interest":0,"status":"NO_DEBT"}
                cur.execute("SELECT interest_monthly_bps,last_interest_at FROM credit_facilities WHERE account_id=%s AND status='ACTIVE' FOR UPDATE",
                            (account_id,))
                facility=cur.fetchone()
                if not facility:
                    raise ValueError("Active credit facility not found.")
                if facility[1] is not None and facility[1].strftime("%Y-%m") == _now().strftime("%Y-%m"):
                    return {"interest":0,"status":"ALREADY_ACCRUED"}
                bps=int(facility[0] or STANDARD_CREDIT_INTEREST_MONTHLY_BPS)
                interest=(debt*bps+9999)//10000
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (interest,account_id))
                txid=next_transaction_id(cur)
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_INTEREST",amount=interest,
                                              recipient_account_id=account_id,description="Monthly credit interest")
                cur.execute("UPDATE credit_facilities SET last_interest_at=CURRENT_TIMESTAMP WHERE account_id=%s",(account_id,))
                return {"transaction_id":txid,"ledger_id":lid,"interest":interest,"status":"COMPLETED"}
    finally:
        release_db_connection(conn)


def process_monthly_billing():
    """Apply configured tier/credit fees and monthly credit interest once per account."""
    conn=get_db_connection()
    results=[]
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT a.id,a.client_id,a.tier_id,a.balance,c.credit_limit,c.activation_fee_monthly,
                                      c.status,c.last_interest_at,c.overdue_since
                               FROM bank_accounts a
                               JOIN account_tiers_v2 t ON t.id=a.tier_id
                               LEFT JOIN credit_facilities c ON c.account_id=a.id
                               WHERE a.status IN ('ACTIVE','LIMITED')""")
                accounts=cur.fetchall()
                period=_now().strftime("%Y-%m")
                for a in accounts:
                    account_id,client_id,tier_id,balance,credit_limit,credit_fee,credit_status,last_interest,overdue=a
                    cur.execute("SELECT monthly_fee FROM account_tiers_v2 WHERE id=%s",(tier_id,))
                    tier_fee=int(cur.fetchone()[0] or 0)
                    fees=[]
                    if tier_fee:
                        cur.execute("""INSERT INTO account_billing(account_id,billing_type,amount,billing_period,status)
                                       VALUES(%s,'TIER_FEE',%s,%s,'COMPLETED')
                                       ON CONFLICT(account_id,billing_type,billing_period) DO NOTHING""",
                                    (account_id,tier_fee,period))
                        if cur.rowcount:
                            cur.execute("UPDATE bank_accounts SET balance=balance-%s WHERE id=%s",(tier_fee,account_id))
                            txid=next_transaction_id(cur)
                            create_ledger_transaction(cur,transaction_id=txid,transaction_type="TIER_FEE",
                                                      amount=tier_fee,recipient_account_id=account_id,
                                                      description=f"Monthly {period} tier fee")
                            fees.append(tier_fee)
                    if credit_status=="ACTIVE" and credit_fee:
                        cur.execute("""INSERT INTO account_billing(account_id,billing_type,amount,billing_period,status)
                                       VALUES(%s,'CREDIT_FEE',%s,%s,'COMPLETED')
                                       ON CONFLICT(account_id,billing_type,billing_period) DO NOTHING""",
                                    (account_id,int(credit_fee),period))
                        if cur.rowcount:
                            cur.execute("UPDATE bank_accounts SET balance=balance-%s WHERE id=%s",(int(credit_fee),account_id))
                            txid=next_transaction_id(cur)
                            create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_FEE",
                                                      amount=int(credit_fee),recipient_account_id=account_id,
                                                      description=f"Monthly credit activation fee {period}")
                            fees.append(int(credit_fee))
                    cur.execute("SELECT balance FROM bank_accounts WHERE id=%s FOR UPDATE",(account_id,))
                    current_balance=int(cur.fetchone()[0])
                    if credit_status=="ACTIVE" and current_balance<0:
                        debt=-current_balance
                        if not overdue:
                            cur.execute("UPDATE credit_facilities SET overdue_since=CURRENT_TIMESTAMP WHERE account_id=%s",(account_id,))
                            cur.execute("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                                           VALUES(%s,%s,'CREDIT_OVERDUE','Credit repayment due',
                                           'Your account has outstanding credit debt. Repay it within one month to avoid suspension and account freeze.')""",
                                        (client_id,account_id))
                        else:
                            cur.execute("""SELECT CASE WHEN overdue_since <= CURRENT_TIMESTAMP - INTERVAL '1 month'
                                           THEN TRUE ELSE FALSE END FROM credit_facilities WHERE account_id=%s""",(account_id,))
                            if cur.fetchone()[0]:
                                cur.execute("UPDATE credit_facilities SET status='SUSPENDED' WHERE account_id=%s",(account_id,))
                                cur.execute("UPDATE bank_accounts SET status='FROZEN' WHERE id=%s",(account_id,))
                                cur.execute("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                                               VALUES(%s,%s,'ACCOUNT_FROZEN','Account frozen',
                                               'Credit debt remained unpaid for one month. All credit lines are suspended pending bank intervention.')""",
                                            (client_id,account_id))
                    results.append({"account_id":account_id,"fees":fees})
        return results
    finally:
        release_db_connection(conn)
