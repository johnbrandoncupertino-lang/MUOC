"""MineBank v2 core financial primitives.

All monetary values are integer Emeralds. Financial mutations are transactional,
ledger-first, and designed to be called by web/API layers.
"""
from datetime import datetime, timezone
from decimal import Decimal

from .database import get_db_connection

PENDING_APPROVAL_THRESHOLD = 5000
STANDARD_CREDIT_INTEREST_MONTHLY_BPS = 700


def _now():
    return datetime.now(timezone.utc)


def _lock_account(cur, account_id):
    cur.execute(
        """SELECT id, client_id, account_number, account_type, tier_id, balance,
                  status, monthly_outgoing_used, monthly_outgoing_period
           FROM bank_accounts WHERE id=%s FOR UPDATE""",
        (account_id,),
    )
    return cur.fetchone()


def _lock_recipient(cur, account_number):
    cur.execute(
        """SELECT id, client_id, account_number, account_type, tier_id, balance, status,
                  monthly_outgoing_used, monthly_outgoing_period
           FROM bank_accounts WHERE account_number=%s FOR UPDATE""",
        (account_number,),
    )
    return cur.fetchone()


def create_ledger_transaction(cur, *, transaction_id, transaction_type,
                              amount, fee, currency, sender_account_id=None,
                              recipient_account_id=None, status="COMPLETED",
                              description=None, reference_id=None):
    cur.execute(
        """INSERT INTO ledger_transactions
           (transaction_id, transaction_type, amount, fee, currency,
            sender_account_id, recipient_account_id, status, description, reference_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (transaction_id, transaction_type, amount, fee, currency,
         sender_account_id, recipient_account_id, status, description, reference_id),
    )
    return cur.fetchone()[0]


def next_transaction_id(cur):
    cur.execute("SELECT nextval('muoc_transaction_seq')")
    seq = cur.fetchone()[0]
    return f"MUOC-{_now().year}-{seq:06d}"


def _reset_month_if_needed(cur, account):
    period = _now().strftime("%Y-%m")
    if account[8] != period:
        cur.execute(
            "UPDATE bank_accounts SET monthly_outgoing_used=0, monthly_outgoing_period=%s WHERE id=%s",
            (period, account[0]),
        )
        return 0, period
    return account[7], period


def transfer(*, sender_account_id, recipient_account_number, amount,
             wallet_pin_hash=None, description=None, reference=None, currency="Emerald"):
    """Create an instant or approval-pending transfer.

    PIN verification belongs in the authentication layer; this service receives
    an already-authorized operation.
    """
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError("Transfer amount must be a positive integer Emerald amount.")

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                sender = _lock_account(cur, sender_account_id)
                if not sender:
                    raise ValueError("Sender account not found.")
                recipient = _lock_recipient(cur, recipient_account_number)
                if not recipient:
                    raise ValueError("Recipient account not found.")
                if sender[0] == recipient[0]:
                    raise ValueError("Self-transfers are not allowed.")
                if sender[6] != "ACTIVE":
                    raise ValueError("Sender account is not active.")
                if recipient[6] not in ("ACTIVE", "LIMITED"):
                    raise ValueError("Recipient account cannot receive transfers.")

                used, period = _reset_month_if_needed(cur, sender)
                cur.execute(
                    "SELECT monthly_outgoing_limit FROM account_tiers_v2 WHERE id=%s",
                    (sender[4],),
                )
                tier = cur.fetchone()
                monthly_limit = tier[0] if tier else None
                if monthly_limit is not None and used + amount > monthly_limit:
                    raise ValueError("Monthly outgoing transfer limit exceeded.")

                # Fee calculation is deliberately delegated to the configurable
                # fee engine in production; zero is the safe base until a rule matches.
                fee = 0
                total = amount + fee
                if sender[5] - total < -(get_credit_limit(cur, sender[0])):
                    raise ValueError("Insufficient available balance/credit.")

                pending = amount >= PENDING_APPROVAL_THRESHOLD
                status = "PENDING_APPROVAL" if pending else "COMPLETED"
                txid = next_transaction_id(cur)

                cur.execute(
                    """UPDATE bank_accounts
                       SET balance=balance-%s, monthly_outgoing_used=%s,
                           monthly_outgoing_period=%s, updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s""",
                    (total, used + amount, period, sender[0]),
                )
                if not pending:
                    cur.execute(
                        """UPDATE bank_accounts
                           SET balance=balance+%s, updated_at=CURRENT_TIMESTAMP
                           WHERE id=%s""",
                        (amount, recipient[0]),
                    )

                ledger_id = create_ledger_transaction(
                    cur, transaction_id=txid, transaction_type="TRANSFER",
                    amount=amount, fee=fee, currency=currency,
                    sender_account_id=sender[0], recipient_account_id=recipient[0],
                    status=status, description=description, reference_id=reference,
                )
                return {"transaction_id": txid, "ledger_id": ledger_id,
                        "status": status, "amount": amount, "fee": fee}
    finally:
        from .database import release_db_connection
        release_db_connection(conn)


def get_credit_limit(cur, account_id):
    cur.execute("SELECT credit_limit FROM credit_facilities WHERE account_id=%s AND status='ACTIVE'", (account_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def approve_transfer(transaction_id, actor_user_id):
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database unavailable")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, sender_account_id, recipient_account_id, amount, fee, status
                       FROM ledger_transactions WHERE transaction_id=%s FOR UPDATE""",
                    (transaction_id,),
                )
                tx = cur.fetchone()
                if not tx or tx[5] != "PENDING_APPROVAL":
                    raise ValueError("Pending transfer not found.")
                cur.execute(
                    "UPDATE bank_accounts SET balance=balance+%s WHERE id=%s",
                    (tx[3], tx[2]),
                )
                cur.execute(
                    """UPDATE ledger_transactions SET status='COMPLETED', approved_by=%s,
                       approved_at=CURRENT_TIMESTAMP WHERE id=%s""",
                    (actor_user_id, tx[0]),
                )
                return transaction_id
    finally:
        from .database import release_db_connection
        release_db_connection(conn)
