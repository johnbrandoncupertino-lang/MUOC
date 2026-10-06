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
             description=None, reference=None, currency=CURRENCY, idempotency_key=None):
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

                status = "PENDING_APPROVAL" if amount >= PENDING_APPROVAL_THRESHOLD else "COMPLETED"
                txid = next_transaction_id(cur)
                cur.execute("""UPDATE bank_accounts SET balance=balance-%s,monthly_outgoing_used=%s,
                               last_outgoing_at=CURRENT_TIMESTAMP,monthly_outgoing_period=%s,
                               updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                            (total,used+amount,period,sender[0]))
                if status == "COMPLETED":
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                                (amount,recipient[0]))
                ledger_id = create_ledger_transaction(
                    cur,transaction_id=txid,transaction_type="TRANSFER",amount=amount,fee=fee,
                    currency=currency,sender_account_id=sender[0],recipient_account_id=recipient[0],
                    status=status,description=description,reference_id=reference)
                if idempotency_key:
                    cur.execute("INSERT INTO transfer_idempotency(idempotency_key,transaction_id) VALUES(%s,%s)",
                                (idempotency_key,txid))
                return {"transaction_id":txid,"ledger_id":ledger_id,"status":status,"amount":amount,"fee":fee}


def approve_transfer(transaction_id, actor_user_id):
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
                return transaction_id
    finally:
        release_db_connection(conn)


def reject_transfer(transaction_id, actor_user_id, reason=None):
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
                if not account or account[6]!="ACTIVE":
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


def repay_credit(account_id, amount):
    if not isinstance(amount,int) or amount<=0:
        raise ValueError("Repayment must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if not account:
                    raise ValueError("Account not found.")
                debt=max(0,-int(account[5]))
                if debt<=0:
                    raise ValueError("Account has no outstanding credit debt.")
                if amount>debt:
                    raise ValueError("Repayment exceeds outstanding debt.")
                # Repayment is an explicit internal settlement: the client must
                # have the funds in a positive balance source in a real channel.
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (amount,account_id))
                txid=next_transaction_id(cur)
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_REPAYMENT",
                                              amount=amount,recipient_account_id=account_id)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount}
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
