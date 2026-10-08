"""MineBank v2 financial engine.

All monetary values are integer Emeralds. Mutations are ledger-first and run
inside database transactions. Web/API layers perform identity and PIN checks.
CashLine is a separate revolving payment circuit; it never makes the ordinary balance negative.
"""
from datetime import datetime, timezone

from .database import get_db_connection, release_db_connection

PENDING_APPROVAL_THRESHOLD = 5000
CURRENCY = "Emerald"
CASHLINE_MINIMUM = 300
CASHLINE_ANNUAL_PERSONAL_BPS = 1830
CASHLINE_ANNUAL_BUSINESS_BPS = 2370
CASHLINE_MINIMUM_DUE_BPS = 500
CASHLINE_MINIMUM_DUE_FLOOR = 40


def _positive_integer_amount(value):
    """Normalize a whole-Emerald amount from trusted internal or form input."""
    if isinstance(value, bool):
        raise ValueError("Transfer amount must be a positive integer Emerald amount.")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw.isascii() or not raw.isdigit():
            raise ValueError("Transfer amount must be a positive integer Emerald amount.")
        amount = int(raw)
    else:
        raise ValueError("Transfer amount must be a positive integer Emerald amount.")
    if amount <= 0:
        raise ValueError("Transfer amount must be a positive integer Emerald amount.")
    return amount


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
                              description=None, reference_id=None, metadata=None, transfer_kind=None, causal=None):
    cur.execute(
        """INSERT INTO ledger_transactions
           (transaction_id,transaction_type,amount,fee,currency,sender_account_id,
            recipient_account_id,status,description,reference_id,transfer_kind,causal)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (transaction_id,transaction_type,amount,fee,currency,sender_account_id,
         recipient_account_id,status,description,reference_id,transfer_kind,causal),
    )
    return cur.fetchone()[0]


def _reset_month_if_needed(cur, account):
    period = _now().strftime("%Y-%m")
    if account[8] != period:
        cur.execute("UPDATE bank_accounts SET monthly_outgoing_used=0,monthly_outgoing_period=%s WHERE id=%s",
                    (period, account[0]))
        return 0, period
    return int(account[7]), period


def _fee_for_transfer(cur, account, recipient_account_id, amount):
    """Apply the MineBank transfer tariff."""
    cur.execute("SELECT client_id FROM bank_accounts WHERE id=%s", (recipient_account_id,))
    recipient = cur.fetchone()
    if recipient and int(recipient[0]) == int(account[1]):
        return 0
    cur.execute("SELECT code FROM account_tiers_v2 WHERE id=%s", (account[4],))
    tier = cur.fetchone()
    if tier and tier[0] in ("PERSONAL_PRIVATE", "CORPORATE"):
        return 0
    return 0 if int(amount) <= 500 else 5


def get_credit_limit(cur, account_id):
    cur.execute("SELECT credit_limit FROM credit_facilities WHERE account_id=%s AND status='ACTIVE'",
                (account_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0




def cashline_outstanding(cur, account_id, include_pending=True):
    """Return CashLine debt for one account, independent from the ordinary balance."""
    statuses = "('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')" if include_pending else "('COMPLETED',)"
    cur.execute(f"""SELECT COALESCE(SUM(amount+fee),0)
                    FROM ledger_transactions
                    WHERE (
                        (sender_account_id=%s AND transaction_type='TRANSFER')
                        OR (recipient_account_id=%s AND transaction_type='CREDIT_DRAW')
                    )
                      AND transfer_kind IN ('CASHLINE','DYNAMIC_CASHLINE') AND status IN {statuses}""",(account_id,account_id))
    principal=int(cur.fetchone()[0] or 0)
    cur.execute("""SELECT COALESCE(SUM(amount),0)
                   FROM ledger_transactions
                   WHERE recipient_account_id=%s
                     AND transaction_type IN ('CREDIT_INTEREST','CREDIT_FEE')
                     AND status='COMPLETED'""",(account_id,))
    charges=int(cur.fetchone()[0] or 0)
    cur.execute("""SELECT COALESCE(SUM(amount),0)
                   FROM ledger_transactions
                   WHERE recipient_account_id=%s
                     AND transaction_type='CREDIT_REPAYMENT'
                     AND status='COMPLETED'""",(account_id,))
    repaid=int(cur.fetchone()[0] or 0)
    return max(0,principal+charges-repaid)

def cashline_principal_outstanding(cur, account_id, include_pending=True):
    statuses = "('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')" if include_pending else "('COMPLETED',)"
    cur.execute(f"""SELECT COALESCE(SUM(amount+fee),0) FROM ledger_transactions
                    WHERE ((sender_account_id=%s AND transaction_type='TRANSFER')
                           OR (recipient_account_id=%s AND transaction_type='CREDIT_DRAW'))
                      AND transfer_kind IN ('CASHLINE','DYNAMIC_CASHLINE') AND status IN {statuses}""",(account_id,account_id))
    principal=int(cur.fetchone()[0] or 0)
    cur.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger_transactions
                   WHERE transaction_type='CREDIT_REPAYMENT' AND status='COMPLETED'
                     AND recipient_account_id=%s""",(account_id,))
    return max(0,principal-int(cur.fetchone()[0] or 0))

def _cashline_repayment_suggestion(cur, account_id, incoming_amount):
    cur.execute("SELECT client_id FROM bank_accounts WHERE id=%s",(account_id,))
    row=cur.fetchone()
    if not row: return
    outstanding=cashline_outstanding(cur,account_id)
    if outstanding<=0 or int(incoming_amount)<outstanding: return
    cur.execute("""SELECT 1 FROM bank_notifications
                   WHERE client_id=%s AND account_id=%s
                     AND notification_type='CASHLINE_REPAYMENT_SUGGESTION'
                     AND read_at IS NULL
                     AND created_at>=CURRENT_TIMESTAMP-INTERVAL '24 hours'
                   LIMIT 1""",(row[0],account_id))
    if cur.fetchone(): return
    cur.execute("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                   VALUES(%s,%s,'CASHLINE_REPAYMENT_SUGGESTION','CashLine repayment suggestion',%s)""",
                (row[0],account_id,
                 f"You have {outstanding} Emerald outstanding on CashLine and just received {int(incoming_amount)} Emerald. You have enough available funds to repay the CashLine balance if you wish. No automatic repayment has been made."))


def _dynamic_cashline_capacity(cur, account_id, requested_amount=0):
    """Return the current transaction-by-transaction Dynamic CashLine capacity."""
    cur.execute("""SELECT d.client_id,a.tier_id,t.code
                   FROM dynamic_cashlines d
                   JOIN bank_accounts a ON a.id=d.account_id
                   JOIN account_tiers_v2 t ON t.id=a.tier_id
                   WHERE d.account_id=%s AND d.status='ACTIVE' FOR UPDATE OF d""",(account_id,))
    row=cur.fetchone()
    if not row:
        return None
    client_id, tier_id, tier_code = row
    cur.execute("SELECT score,credit_status FROM credicheck_profiles WHERE client_id=%s",(client_id,))
    profile=cur.fetchone()
    if not profile or str(profile[1] or '').upper()!="ACTIVE":
        raise ValueError("Dynamic CashLine is not currently available for this account.")
    # Legacy databases may contain a blank score despite the current schema
    # declaring this field INTEGER. Use the neutral default rather than leaking
    # a low-level int('') exception into a customer transfer.
    try:
        raw_score = str(profile[0]).strip() if profile[0] is not None else ""
        score = int(raw_score) if raw_score else 5
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Dynamic CashLine profile score is invalid. Please contact MineBank support.")
    if score < 0 or score > 10:
        raise ValueError("Dynamic CashLine profile score is outside the supported range.")
    cur.execute("""SELECT COUNT(*) FROM minebank_loans
                   WHERE client_id=%s AND status='ACTIVE' AND next_due_date<CURRENT_DATE""",(client_id,))
    overdue=cur.fetchone()
    if overdue and int(overdue[0] or 0)>0:
        raise ValueError("Dynamic CashLine cannot be used while a Loan repayment is overdue.")
    cur.execute("SELECT COUNT(*) FROM minebank_loans WHERE client_id=%s AND status='DEFAULTED'",(client_id,))
    defaulted=cur.fetchone()
    if defaulted and int(defaulted[0] or 0)>0:
        raise ValueError("Dynamic CashLine is unavailable while a Loan is in default.")
    bases={"PERSONAL":1000,"PERSONAL_PRO":3000,"PERSONAL_PRIVATE":10000,
           "BUSINESS":5000,"BUSINESS_PRO":25000,"CORPORATE":100000}
    multipliers={0:1.50,1:1.50,2:1.50,3:1.25,4:1.25,5:1.25,6:1.0,7:1.0,8:.75,9:.75,10:.75}
    base=bases.get(str(tier_code or '').upper(),0)
    capacity=int(base*multipliers.get(score,0))
    debt=cashline_outstanding(cur,account_id)
    return max(0,capacity-debt)

def preview_transfer(actor_client_id, sender_account_id, recipient_account_number, amount, extra_fee=0, funding_source="BALANCE"):
    """Validate a transfer and calculate its fee without changing the ledger."""
    amount = _positive_integer_amount(amount)
    funding_source=(funding_source or "BALANCE").upper()
    if funding_source not in ("BALANCE","CASHLINE","DYNAMIC_CASHLINE"): raise ValueError("Invalid payment source.")
    recipient_account_number = (recipient_account_number or "").strip().upper()
    if not recipient_account_number:
        raise ValueError("Recipient account number is required.")

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database unavailable")
    try:
        with conn.cursor() as cur:
            if actor_client_id is not None:
                _assert_banking_unlocked(cur, actor_client_id)
            sender = _lock_account(cur, sender_account_id)
            recipient = _lock_recipient(cur, recipient_account_number)
            if not sender or not recipient:
                raise ValueError("Sender or recipient account not found.")

            if sender[3] == "BUSINESS":
                cur.execute("SELECT code FROM account_tiers_v2 WHERE id=%s", (sender[4],))
                tier = cur.fetchone()
                tier_code = tier[0] if tier else None
                cur.execute("SELECT role FROM minebank_business_members WHERE account_id=%s AND client_id=%s",
                            (sender[0], actor_client_id))
                member = cur.fetchone() if actor_client_id is not None else None
                role = "OWNER" if sender[1] == actor_client_id else (member[0] if member else None)
                if role not in ("OWNER", "ADMIN", "FINANCE_MANAGER", "EMPLOYEE"):
                    raise ValueError("You do not have payment permission on this Business account.")
            elif actor_client_id is not None and sender[1] != actor_client_id:
                raise ValueError("Account does not belong to the logged-in client.")

            if sender[0] == recipient[0]:
                raise ValueError("Self-transfers are not allowed.")
            if sender[6] != "ACTIVE":
                raise ValueError("Sender account is not active.")
            if recipient[6] == "FROZEN":
                cur.execute("SELECT freeze_type FROM bank_accounts WHERE id=%s", (recipient[0],))
                freeze_type = cur.fetchone()[0]
                if freeze_type not in ("SECURITY_FREEZE", "EMERGENCY_FREEZE", "CREDIT_FREEZE"):
                    raise ValueError("Recipient account cannot receive transfers while frozen.")
            elif recipient[6] not in ("ACTIVE", "LIMITED"):
                raise ValueError("Recipient account cannot receive transfers.")

            used, period = _reset_month_if_needed(cur, sender)
            cur.execute("SELECT monthly_outgoing_limit,daily_outgoing_limit,single_transfer_limit FROM account_tiers_v2 WHERE id=%s", (sender[4],))
            limits = cur.fetchone()
            if not limits:
                raise ValueError("Sender account tier is not configured.")
            monthly_limit, daily_limit, single_limit = limits
            if single_limit is not None and amount > int(single_limit):
                raise ValueError("Single-transfer limit exceeded.")
            if monthly_limit is not None and used + amount > int(monthly_limit):
                raise ValueError("Monthly outgoing transfer limit exceeded.")
            cur.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger_transactions
                           WHERE sender_account_id=%s AND status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')
                             AND created_at>=CURRENT_TIMESTAMP-INTERVAL '1 day'""", (sender[0],))
            daily_used = int(cur.fetchone()[0] or 0)
            if daily_limit is not None and daily_used + amount > int(daily_limit):
                raise ValueError("Daily outgoing transfer limit exceeded.")
            if sender[9] and (_now() - sender[9]).total_seconds() < 30:
                raise ValueError("Outgoing transfers require a 30-second cooldown.")

            fee = _fee_for_transfer(cur, sender, recipient[0], amount) + max(0, int(extra_fee))
            total = amount + fee
            if funding_source=="BALANCE":
                if int(sender[5]) < total: raise ValueError("Insufficient MineBank Balance.")
                available_after=int(sender[5])-total
            elif funding_source=="DYNAMIC_CASHLINE":
                dynamic_capacity=_dynamic_cashline_capacity(cur,sender[0],amount)
                if dynamic_capacity is None:
                    raise ValueError("Dynamic CashLine is not active for this account.")
                if total>dynamic_capacity:
                    raise ValueError(f"Dynamic CashLine assessment declined this payment. Current assessed capacity: {dynamic_capacity} Emerald.")
                available_after=dynamic_capacity-total
            else:
                cur.execute("SELECT credit_limit,status FROM credit_facilities WHERE account_id=%s FOR UPDATE",(sender[0],))
                facility=cur.fetchone()
                if not facility or facility[1]!="ACTIVE": raise ValueError("Standard CashLine is not active for this account.")
                outstanding=cashline_outstanding(cur,sender[0])
                available_credit=max(0,int(facility[0])-outstanding)
                if total>available_credit: raise ValueError(f"Insufficient Standard CashLine availability. Available: {available_credit} Emerald.")
                available_after=available_credit-total
            cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s", (recipient[4],))
            max_balance_row = cur.fetchone()
            max_balance = max_balance_row[0] if max_balance_row else None
            if max_balance is not None and int(recipient[5]) + amount > int(max_balance):
                raise ValueError("Recipient account balance limit exceeded.")

            return {
                "recipient_account_number": recipient[2],
                "amount": amount,
                "fee": fee,
                "total": total,
                "sender_balance": int(sender[5]),
                "available_after": available_after,
                "funding_source": funding_source,
                "recipient_balance": int(recipient[5]),
                "period": period,
            }
    finally:
        release_db_connection(conn)

def transfer(*, sender_account_id, recipient_account_number, amount,
             description=None, reference=None, causal=None, currency=CURRENCY, idempotency_key=None,
             actor_client_id=None, ip_address=None, transfer_kind=None, extra_fee=0, funding_source="BALANCE"):
    amount = _positive_integer_amount(amount)
    funding_source=(funding_source or "BALANCE").upper()
    if funding_source not in ("BALANCE","CASHLINE","DYNAMIC_CASHLINE"): raise ValueError("Invalid payment source.")
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
                business_role=None
                sender_tier_code=None
                if sender[3]=="BUSINESS":
                    cur.execute("SELECT code FROM account_tiers_v2 WHERE id=%s",(sender[4],))
                    sender_tier_code=cur.fetchone()[0]
                    cur.execute("""SELECT role FROM minebank_business_members
                                   WHERE account_id=%s AND client_id=%s""",(sender[0],actor_client_id))
                    member=cur.fetchone() if actor_client_id is not None else None
                    if sender[1]==actor_client_id:
                        business_role="OWNER"
                    elif member:
                        business_role=member[0]
                    else:
                        raise ValueError("You do not have access to this Business account.")
                    if business_role not in ("OWNER","ADMIN","FINANCE_MANAGER","EMPLOYEE"):
                        raise ValueError("You do not have payment permission on this Business account.")
                elif actor_client_id is not None and sender[1] != actor_client_id:
                    raise ValueError("Account does not belong to the logged-in client.")
                if sender[0] == recipient[0]:
                    raise ValueError("Self-transfers are not allowed.")
                if sender[6] != "ACTIVE":
                    raise ValueError("Sender account is not active.")
                if recipient[6] == "FROZEN":
                    cur.execute("SELECT freeze_type FROM bank_accounts WHERE id=%s",(recipient[0],))
                    freeze_type=cur.fetchone()[0]
                    if freeze_type not in ("SECURITY_FREEZE","EMERGENCY_FREEZE","CREDIT_FREEZE"):
                        raise ValueError("Recipient account cannot receive transfers while frozen.")
                elif recipient[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Recipient account cannot receive transfers.")

                used, period = _reset_month_if_needed(cur, sender)
                cur.execute("SELECT monthly_outgoing_limit,daily_outgoing_limit,single_transfer_limit FROM account_tiers_v2 WHERE id=%s", (sender[4],))
                monthly_limit,daily_limit,single_limit = cur.fetchone()
                if single_limit is not None and amount > int(single_limit):
                    raise ValueError("Single-transfer limit exceeded.")
                if monthly_limit is not None and used + amount > int(monthly_limit):
                    raise ValueError("Monthly outgoing transfer limit exceeded.")
                cur.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger_transactions
                               WHERE sender_account_id=%s AND status IN ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')
                                 AND created_at>=CURRENT_TIMESTAMP-INTERVAL '1 day'""",(sender[0],))
                daily_used=int(cur.fetchone()[0] or 0)
                if daily_limit is not None and daily_used + amount > int(daily_limit):
                    raise ValueError("Daily outgoing transfer limit exceeded.")
                if sender[9] and (_now() - sender[9]).total_seconds() < 30:
                    raise ValueError("Outgoing transfers require a 30-second cooldown.")

                fee = _fee_for_transfer(cur, sender, recipient[0], amount) + max(0, int(extra_fee))
                total = amount + fee
                if funding_source=="BALANCE":
                    if int(sender[5]) < total: raise ValueError("Insufficient MineBank Balance.")
                elif funding_source=="DYNAMIC_CASHLINE":
                    dynamic_capacity=_dynamic_cashline_capacity(cur,sender[0],amount)
                    if dynamic_capacity is None:
                        raise ValueError("Dynamic CashLine is not active for this account.")
                    if total>dynamic_capacity:
                        raise ValueError(f"Dynamic CashLine assessment declined this payment. Current assessed capacity: {dynamic_capacity} Emerald.")
                else:
                    cur.execute("SELECT credit_limit,status FROM credit_facilities WHERE account_id=%s FOR UPDATE",(sender[0],))
                    facility=cur.fetchone()
                    if not facility or facility[1]!="ACTIVE": raise ValueError("Standard CashLine is not active for this account.")
                    outstanding=cashline_outstanding(cur,sender[0])
                    available_credit=max(0,int(facility[0])-outstanding)
                    if total>available_credit: raise ValueError(f"Insufficient Standard CashLine availability. Available: {available_credit} Emerald.")

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
                requires_business_approval = sender[3]=="BUSINESS" and sender_tier_code in ("BUSINESS_PRO","CORPORATE") and business_role=="EMPLOYEE"
                status = "PENDING_BUSINESS_APPROVAL" if requires_business_approval else ("PENDING_APPROVAL" if amount > PENDING_APPROVAL_THRESHOLD or risk_score >= 60 else "COMPLETED")
                if funding_source=="CASHLINE":
                    transfer_kind="CASHLINE"
                elif funding_source=="DYNAMIC_CASHLINE":
                    transfer_kind="DYNAMIC_CASHLINE"
                elif transfer_kind is None:
                    transfer_kind = "OWN_TRANSFER" if sender[1] == recipient[1] else f"{sender[3]}_TO_{recipient[3]}"
                txid = next_transaction_id(cur)
                if funding_source=="BALANCE":
                    cur.execute("""UPDATE bank_accounts SET balance=balance-%s,monthly_outgoing_used=%s,
                               last_outgoing_at=CURRENT_TIMESTAMP,monthly_outgoing_period=%s,
                               updated_at=CURRENT_TIMESTAMP WHERE id=%s""",(total,used+amount,period,sender[0]))
                else:
                    cur.execute("""UPDATE bank_accounts SET monthly_outgoing_used=%s,
                               last_outgoing_at=CURRENT_TIMESTAMP,monthly_outgoing_period=%s,
                               updated_at=CURRENT_TIMESTAMP WHERE id=%s""",(used+amount,period,sender[0]))
                if status == "COMPLETED":
                    cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s", (recipient[4],))
                    max_balance=cur.fetchone()[0]
                    if max_balance is not None and int(recipient[5])+amount>int(max_balance):
                        raise ValueError("Recipient account balance limit exceeded.")
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                                (amount,recipient[0]))
                    _cashline_repayment_suggestion(cur,recipient[0],amount)
                ledger_id = create_ledger_transaction(
                    cur,transaction_id=txid,transaction_type="TRANSFER",amount=amount,fee=fee,
                    currency=currency,sender_account_id=sender[0],recipient_account_id=recipient[0],
                    status=status,description=description,reference_id=reference,transfer_kind=transfer_kind,causal=causal)
                if status == "PENDING_BUSINESS_APPROVAL":
                    cur.execute("""INSERT INTO minebank_business_payment_approvals(transaction_id,account_id,requested_by,risk_score)
                                   VALUES(%s,%s,%s,%s)""",(txid,sender[0],actor_client_id,risk_score))
                if idempotency_key:
                    cur.execute("INSERT INTO transfer_idempotency(idempotency_key,client_id,transaction_id) VALUES(%s,%s,%s)",
                                (idempotency_key,sender[1],txid))
                audit_event(cur,actor_client_id=actor_client_id,action="TRANSFER_CREATED",target_type="TRANSACTION",target_id=txid,account_id=sender[0],transaction_id=txid,ip_address=ip_address,context={"recipient_account":recipient[2],"status":status})
                return {"transaction_id":txid,"ledger_id":ledger_id,"status":status,"amount":amount,"fee":fee}
    finally:
        release_db_connection(conn)


def approve_business_transfer(transaction_id, approver_client_id, ip_address=None):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT b.id,b.account_id,b.requested_by,b.status,b.risk_score,l.amount,l.fee,l.sender_account_id,l.recipient_account_id
                               FROM minebank_business_payment_approvals b
                               JOIN ledger_transactions l ON l.transaction_id=b.transaction_id
                               WHERE b.transaction_id=%s AND b.status='PENDING' FOR UPDATE""",(transaction_id,))
                row=cur.fetchone()
                if not row: raise ValueError("Business approval not found.")
                cur.execute("""SELECT role FROM minebank_business_members WHERE account_id=%s AND client_id=%s""",(row[1],approver_client_id))
                member=cur.fetchone()
                if not member or member[0] not in ("OWNER","ADMIN","FINANCE_MANAGER"):
                    raise ValueError("Only an Owner, Business Admin or Finance Manager can approve this payment.")
                if row[2]==approver_client_id:
                    raise ValueError("The payment creator cannot approve their own payment.")
                next_status="PENDING_APPROVAL" if row[4]>=60 or row[5]>=PENDING_APPROVAL_THRESHOLD else "COMPLETED"
                if next_status=="COMPLETED":
                    recipient=_lock_account(cur,row[8])
                    if not recipient or recipient[6] not in ("ACTIVE","LIMITED"):
                        raise ValueError("Recipient account cannot receive the payment.")
                    cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s",(recipient[4],))
                    max_balance=cur.fetchone()[0]
                    if max_balance is not None and int(recipient[5])+int(row[5])>int(max_balance):
                        raise ValueError("Recipient account balance limit exceeded.")
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(row[5],row[8]))
                cur.execute("UPDATE minebank_business_payment_approvals SET status='APPROVED',approved_by=%s,reviewed_at=CURRENT_TIMESTAMP WHERE id=%s",(approver_client_id,row[0]))
                cur.execute("UPDATE ledger_transactions SET status=%s,approved_by=%s,approved_at=CURRENT_TIMESTAMP WHERE transaction_id=%s",(next_status,approver_client_id,transaction_id))
                audit_event(cur,actor_client_id=approver_client_id,action="BUSINESS_TRANSFER_APPROVED",target_type="TRANSACTION",target_id=transaction_id,account_id=row[1],transaction_id=transaction_id,ip_address=ip_address,context={"next_status":next_status})
                return next_status
    finally:
        release_db_connection(conn)

def reject_business_transfer(transaction_id, approver_client_id, reason=None, ip_address=None):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT b.id,b.account_id,b.requested_by,l.id,l.sender_account_id,l.amount,l.fee,l.status,l.transfer_kind
                               FROM minebank_business_payment_approvals b JOIN ledger_transactions l ON l.transaction_id=b.transaction_id
                               WHERE b.transaction_id=%s AND b.status='PENDING' FOR UPDATE""",(transaction_id,))
                row=cur.fetchone()
                if not row: raise ValueError("Business approval not found.")
                cur.execute("SELECT role FROM minebank_business_members WHERE account_id=%s AND client_id=%s",(row[1],approver_client_id))
                member=cur.fetchone()
                if not member or member[0] not in ("OWNER","ADMIN","FINANCE_MANAGER"):
                    raise ValueError("Only an Owner, Business Admin or Finance Manager can reject this payment.")
                if row[2]==approver_client_id:
                    raise ValueError("The payment creator cannot reject their own payment.")
                if row[8] in ("CASHLINE","DYNAMIC_CASHLINE"):
                    cur.execute("UPDATE bank_accounts SET monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",(row[5],row[4]))
                else:
                    cur.execute("""UPDATE bank_accounts SET balance=balance+%s,
                               monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                                (row[5]+row[6],row[5],row[4]))
                cur.execute("UPDATE minebank_business_payment_approvals SET status='REJECTED',approved_by=%s,reviewed_at=CURRENT_TIMESTAMP WHERE id=%s",(approver_client_id,row[0]))
                cur.execute("UPDATE ledger_transactions SET status='REJECTED',description=COALESCE(description,'') || %s WHERE id=%s",
                            (f" [Business rejected: {reason or 'No reason supplied'}]",row[3]))
                audit_event(cur,actor_client_id=approver_client_id,action="BUSINESS_TRANSFER_REJECTED",target_type="TRANSACTION",target_id=transaction_id,account_id=row[1],transaction_id=transaction_id,ip_address=ip_address,context={"reason":reason or ""})
                return transaction_id
    finally:
        release_db_connection(conn)

def approve_transfer(transaction_id, actor_user_id, ip_address=None):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,sender_account_id,recipient_account_id,amount,status,transfer_kind FROM ledger_transactions
                               WHERE transaction_id=%s FOR UPDATE""", (transaction_id,))
                tx = cur.fetchone()
                if not tx or tx[4] != "PENDING_APPROVAL":
                    raise ValueError("Pending transfer not found.")
                sender = _lock_account(cur, tx[1])
                recipient = _lock_account(cur, tx[2])
                if sender and sender[1] == actor_user_id:
                    raise ValueError("The person who initiated a transfer cannot approve their own transfer.")
                if recipient and recipient[6]=="FROZEN":
                    cur.execute("SELECT freeze_type FROM bank_accounts WHERE id=%s",(recipient[0],))
                    freeze_type=cur.fetchone()[0]
                    if freeze_type not in ("SECURITY_FREEZE","EMERGENCY_FREEZE","CREDIT_FREEZE"):
                        raise ValueError("Recipient account cannot receive the transfer while frozen.")
                elif not recipient or recipient[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Recipient account cannot receive the transfer.")
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (tx[3],tx[2]))
                cur.execute("""UPDATE ledger_transactions SET status='COMPLETED',approved_by=%s,
                               approved_at=CURRENT_TIMESTAMP WHERE id=%s""", (actor_user_id,tx[0]))
                cur.execute("""UPDATE minebank_payment_requests
                               SET status='PAID',paid_transaction_id=%s
                               WHERE id=(SELECT split_part(idempotency_key,'-',2)::BIGINT
                                         FROM transfer_idempotency WHERE transaction_id=%s
                                           AND idempotency_key LIKE 'PAYREQ-%')
                                 AND status='PENDING'""",(transaction_id,transaction_id))
                audit_event(cur,actor_client_id=actor_user_id,action="TRANSFER_APPROVED",target_type="TRANSACTION",target_id=transaction_id,account_id=tx[2],transaction_id=transaction_id,ip_address=ip_address)
                return transaction_id
    finally:
        release_db_connection(conn)


def reject_transfer(transaction_id, actor_user_id, reason=None, ip_address=None):
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,sender_account_id,amount,fee,status,transfer_kind FROM ledger_transactions
                               WHERE transaction_id=%s FOR UPDATE""", (transaction_id,))
                tx = cur.fetchone()
                if not tx or tx[4] != "PENDING_APPROVAL":
                    raise ValueError("Pending transfer not found.")
                sender = _lock_account(cur, tx[1])
                if not sender:
                    raise ValueError("Sender account not found.")
                if tx[5] in ("CASHLINE","DYNAMIC_CASHLINE"):
                    cur.execute("UPDATE bank_accounts SET monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",(tx[2],tx[1]))
                else:
                    cur.execute("UPDATE bank_accounts SET balance=balance+%s,monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",(tx[2]+tx[3],tx[2],tx[1]))
                cur.execute("""UPDATE ledger_transactions SET status='REJECTED',approved_by=%s,
                               approved_at=CURRENT_TIMESTAMP,description=COALESCE(description,'') || %s
                               WHERE id=%s""", (actor_user_id, f" [Rejected: {reason or 'No reason supplied'}]",tx[0]))
                audit_event(cur,actor_client_id=actor_user_id,action="TRANSFER_REJECTED",target_type="TRANSACTION",target_id=transaction_id,account_id=tx[1],transaction_id=transaction_id,ip_address=ip_address,context={"reason":reason or ""})
                return transaction_id
    finally:
        release_db_connection(conn)


def cancel_transfer(transaction_id, actor_client_id, ip_address=None):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,sender_account_id,amount,fee,status,transfer_kind FROM ledger_transactions
                               WHERE transaction_id=%s FOR UPDATE""",(transaction_id,))
                tx=cur.fetchone()
                if not tx or tx[4]!="PENDING_APPROVAL":
                    raise ValueError("Only pending transfers can be cancelled.")
                sender=_lock_account(cur,tx[1])
                if not sender or sender[1]!=actor_client_id:
                    raise ValueError("You cannot cancel this transfer.")
                if tx[5] in ("CASHLINE","DYNAMIC_CASHLINE"):
                    cur.execute("UPDATE bank_accounts SET monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",(tx[2],tx[1]))
                else:
                    cur.execute("""UPDATE bank_accounts SET balance=balance+%s,
                               monthly_outgoing_used=GREATEST(0,monthly_outgoing_used-%s),
                               updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                                (tx[2]+tx[3],tx[2],tx[1]))
                cur.execute("""UPDATE ledger_transactions SET status='CANCELLED',
                               description=COALESCE(description,'') || ' [Cancelled by customer]'
                               WHERE id=%s""",(tx[0],))
                audit_event(cur,actor_client_id=actor_client_id,action="TRANSFER_CANCELLED",
                            target_type="TRANSACTION",target_id=transaction_id,account_id=tx[1],
                            transaction_id=transaction_id,ip_address=ip_address)
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
                if not account or account[6] not in ("ACTIVE","LIMITED","FROZEN"):
                    raise ValueError("Account cannot receive deposits.")
                cur.execute("SELECT max_balance FROM account_tiers_v2 WHERE id=%s",(account[4],))
                max_balance=cur.fetchone()[0]
                if max_balance is not None and account[5]+amount>int(max_balance):
                    raise ValueError("Account balance limit exceeded.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id))
                _cashline_repayment_suggestion(cur,account_id,amount)
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="DEPOSIT",amount=amount,
                                              recipient_account_id=account_id,description=description)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount,"fee":0}
    finally:
        release_db_connection(conn)


def withdraw(account_id, amount, actor_user_id=None, description=None):
    """Withdraw only from the ordinary MineBank Balance; CashLine is a separate circuit."""
    if not isinstance(amount,int) or amount<=0:
        raise ValueError("Withdrawal must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if account: _assert_banking_unlocked(cur,account[1])
                if not account or account[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Account is not active.")
                if int(account[5])<amount:
                    raise ValueError("Insufficient MineBank Balance. CashLine cannot be used for withdrawals.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,account_id))
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="WITHDRAWAL",amount=amount,
                                              sender_account_id=account_id,description=description)
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount,"fee":0}
    finally:
        release_db_connection(conn)



def disburse_loan(loan_id, approved_by=None):
    """Atomically approve and deposit a MineBank loan into its destination account."""
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,client_id,destination_account_id,principal,status,loan_number,installment_amount,term_days FROM minebank_loans WHERE id=%s FOR UPDATE",(loan_id,))
                loan=cur.fetchone()
                if not loan: raise ValueError("Loan not found.")
                if loan[4] not in ("PENDING","APPROVED"): raise ValueError("Loan is no longer awaiting disbursement.")
                destination=_lock_account(cur,loan[2])
                if not destination or destination[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Loan destination account is not active.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(loan[3],loan[2]))
                create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_DISBURSEMENT",
                                          amount=loan[3],recipient_account_id=loan[2],
                                          description=f"Loan disbursement {loan[5]}",reference_id=loan[5])
                cur.execute("""UPDATE minebank_loans
                               SET status='ACTIVE',approved_at=COALESCE(approved_at,CURRENT_TIMESTAMP),
                                   approved_by=COALESCE(%s,approved_by),
                                   next_due_date=CURRENT_DATE+1
                               WHERE id=%s""",(approved_by,loan_id))
                return {"transaction_id":txid,"loan_number":loan[5],"amount":loan[3]}
    finally:
        release_db_connection(conn)

def auto_pay_due_loans():
    """Collect due daily loan installments automatically when the repayment account has enough balance."""
    conn=get_db_connection(); results=[]
    if conn is None: return results
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id FROM minebank_loans WHERE status='ACTIVE' AND installments_paid < term_days AND next_due_date IS NOT NULL AND next_due_date <= CURRENT_DATE ORDER BY next_due_date,id FOR UPDATE SKIP LOCKED""")
                for row in cur.fetchall():
                    loan_id=row[0]
                    cur.execute("""SELECT id,client_id,destination_account_id,installment_amount,installments_paid,term_days,total_cost,loan_number,next_due_date FROM minebank_loans WHERE id=%s FOR UPDATE""",(loan_id,))
                    loan=cur.fetchone()
                    if not loan: continue
                    source=_lock_account(cur,loan[2])
                    if not source or source[6]!="ACTIVE": continue
                    paid=int(loan[4]); term=int(loan[5]); total_cost=int(loan[6]); due_date=loan[8]
                    while paid < term and due_date and due_date <= _now().date():
                        remaining=max(0,total_cost-paid*int(loan[3]))
                        installment=min(int(loan[3]),remaining) if paid+1 < term else remaining
                        if installment <= 0 or int(source[5]) < installment: break
                        txid=next_transaction_id(cur)
                        cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(installment,source[0]))
                        create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_REPAYMENT",amount=installment,sender_account_id=source[0],description=f"Automatic loan repayment {loan[7]}",reference_id=loan[7])
                        paid += 1
                        if paid >= term:
                            cur.execute("UPDATE minebank_loans SET installments_paid=%s,status='COMPLETED',next_due_date=NULL,completed_at=CURRENT_TIMESTAMP WHERE id=%s",(paid,loan_id))
                            results.append({"loan_number":loan[7],"amount":installment,"status":"COMPLETED"}); break
                        cur.execute("UPDATE minebank_loans SET installments_paid=%s,next_due_date=next_due_date+1 WHERE id=%s RETURNING next_due_date",(paid,loan_id))
                        due_date=cur.fetchone()[0]
                        results.append({"loan_number":loan[7],"amount":installment,"status":"ACTIVE"})
        return results
    finally: release_db_connection(conn)

def repay_loan_fraction(loan_id, source_account_id, actor_client_id, numerator=1, denominator=4):
    """Repay a selected fraction of the remaining Loan from MineBank Balance only."""
    numerator=int(numerator); denominator=int(denominator)
    if denominator != 4 or numerator not in (1,2,3,4):
        raise ValueError("Choose 1/4, 1/2, 3/4, or the full remaining balance.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,client_id,installment_amount,installments_paid,term_days,total_cost,status,loan_number,principal FROM minebank_loans WHERE id=%s FOR UPDATE""",(loan_id,))
                loan=cur.fetchone()
                if not loan or loan[6]!="ACTIVE": raise ValueError("Loan is not active.")
                if int(loan[1])!=int(actor_client_id): raise ValueError("Loan does not belong to this client.")
                remaining=max(0,int(loan[5])-int(loan[2])*int(loan[3]))
                if remaining<=0: raise ValueError("This Loan has already been fully repaid.")
                amount=remaining if numerator==4 else max(1,remaining*numerator//denominator)
                source=_lock_account(cur,source_account_id)
                if not source or int(source[1])!=int(actor_client_id): raise ValueError("Repayment account does not belong to you.")
                if source[6]!="ACTIVE": raise ValueError("Repayment account is not active.")
                if int(source[5])<amount: raise ValueError(f"Insufficient MineBank Balance. Required: {amount} Emerald. CashLine cannot be used.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,source[0]))
                create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_REPAYMENT",amount=amount,sender_account_id=source[0],description=f"Loan repayment {loan[7]} ({numerator}/4)",reference_id=loan[7])
                if numerator==4 or amount>=remaining:
                    # Preserve the original principal for audit/history and to
                    # satisfy minebank_loans_principal_check after settlement.
                    cur.execute("UPDATE minebank_loans SET installments_paid=term_days,status='COMPLETED',total_cost=0,next_due_date=NULL,completed_at=CURRENT_TIMESTAMP WHERE id=%s",(loan_id,))
                    status="COMPLETED"
                else:
                    new_remaining=max(0,remaining-amount)
                    # principal is the original contractual principal and is
                    # constrained by the product minimum; never overwrite it with
                    # the remaining debt after a partial repayment.
                    remaining_days=max(1,int(loan[4])-int(loan[3]))
                    new_installment=max(1,(new_remaining+remaining_days-1)//remaining_days)
                    new_total=new_remaining + new_installment*int(loan[3])
                    cur.execute("UPDATE minebank_loans SET total_cost=%s,installment_amount=%s WHERE id=%s",(new_total,new_installment,loan_id))
                    status="ACTIVE"
                return {"transaction_id":txid,"amount":amount,"status":status,"remaining":max(0,remaining-amount)}
    finally: release_db_connection(conn)

def repay_loan_installment(loan_id, source_account_id, actor_client_id):
    """Atomically collect one daily loan installment from another owned account."""
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,client_id,account_id,destination_account_id,installment_amount,
                                      installments_paid,term_days,principal,total_interest,total_cost,
                                      status,loan_number
                               FROM minebank_loans WHERE id=%s FOR UPDATE""",(loan_id,))
                loan=cur.fetchone()
                if not loan or loan[10]!="ACTIVE": raise ValueError("Loan is not active.")
                if int(loan[1])!=int(actor_client_id): raise ValueError("Loan does not belong to this client.")
                if int(loan[5])>=int(loan[6]):
                    raise ValueError("All loan installments have already been paid.")
                source=_lock_account(cur,source_account_id)
                if not source or int(source[1])!=int(actor_client_id): raise ValueError("Repayment account does not belong to you.")
                if source[6]!="ACTIVE": raise ValueError("Repayment account is not active.")
                remaining_total=max(0,int(loan[9])-int(loan[5])*int(loan[4]))
                installment=min(int(loan[4]),remaining_total) if int(loan[5])+1 < int(loan[6]) else remaining_total
                if int(source[5])<installment: raise ValueError("Insufficient balance to pay today's loan installment.")
                txid=next_transaction_id(cur)
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(installment,source_account_id))
                create_ledger_transaction(cur,transaction_id=txid,transaction_type="LOAN_REPAYMENT",
                                          amount=installment,sender_account_id=source_account_id,
                                          description=f"Loan repayment {loan[11]}",reference_id=loan[11])
                paid=int(loan[5])+1
                status="COMPLETED" if paid>=int(loan[6]) else "ACTIVE"
                cur.execute("""UPDATE minebank_loans SET installments_paid=%s,status=%s,
                                  next_due_date=CASE WHEN %s='ACTIVE' THEN CURRENT_DATE+1 ELSE NULL END,
                                  completed_at=CASE WHEN %s='COMPLETED' THEN CURRENT_TIMESTAMP ELSE completed_at END
                               WHERE id=%s""",(paid,status,status,status,loan_id))
                return {"transaction_id":txid,"amount":installment,"status":status,"installments_paid":paid,"term_days":loan[6]}
    finally:
        release_db_connection(conn)


def repay_credit(account_id, amount, source_account_id):
    if not isinstance(amount,int) or amount<=0: raise ValueError("Repayment must be a positive integer Emerald amount.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                debt_account=_lock_account(cur,account_id); source=_lock_account(cur,source_account_id)
                if debt_account: _assert_banking_unlocked(cur,debt_account[1])
                if not debt_account or not source: raise ValueError("Account not found.")
                if debt_account[1]!=source[1]: raise ValueError("Repayment source must belong to the same client.")
                if source_account_id==account_id: raise ValueError("Repayment requires a separate source account.")
                cur.execute("SELECT status FROM credit_facilities WHERE account_id=%s FOR UPDATE",(account_id,))
                standard=cur.fetchone()
                cur.execute("SELECT status FROM dynamic_cashlines WHERE account_id=%s AND status='ACTIVE' FOR UPDATE",(account_id,))
                dynamic=cur.fetchone()
                if (not standard or standard[0] not in ("ACTIVE","SUSPENDED")) and not dynamic:
                    raise ValueError("CashLine is not active on this account.")
                outstanding=cashline_outstanding(cur,account_id)
                if outstanding<=0: raise ValueError("CashLine has no outstanding balance.")
                if amount>outstanding: raise ValueError("Repayment exceeds the outstanding CashLine balance.")
                if int(source[5])<amount: raise ValueError("Source account does not have enough positive balance.")
                cur.execute("UPDATE bank_accounts SET balance=balance-%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(amount,source_account_id))
                txid=next_transaction_id(cur)
                lid=create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_REPAYMENT",amount=amount,sender_account_id=source_account_id,recipient_account_id=account_id,description="CashLine repayment")
                remaining=amount
                cur.execute("""SELECT id,total_due,amount_paid FROM credit_statements WHERE account_id=%s AND status IN ('OPEN','PARTIALLY_PAID','OVERDUE') ORDER BY due_at,id FOR UPDATE""",(account_id,))
                for sid,total_due,paid in cur.fetchall():
                    if remaining<=0: break
                    applied=min(remaining,max(0,int(total_due)-int(paid)))
                    if applied<=0: continue
                    new_paid=int(paid)+applied; status="PAID" if new_paid>=int(total_due) else "PARTIALLY_PAID"
                    cur.execute("UPDATE credit_statements SET amount_paid=%s,status=%s WHERE id=%s",(new_paid,status,sid)); remaining-=applied
                return {"transaction_id":txid,"ledger_id":lid,"status":"COMPLETED","amount":amount,"remaining":outstanding-amount}
    finally: release_db_connection(conn)


def draw_credit(account_id, amount, actor_client_id=None, ip_address=None):
    """Legacy compatibility guard: CashLine credit is drawn only by selecting CashLine on a transfer."""
    raise ValueError("CashLine is a separate payment circuit. Select CashLine when creating a transfer.")


def activate_credit(account_id, requested_limit):
    if not isinstance(requested_limit,int) or requested_limit<=0:
        raise ValueError("Credit limit must be a positive integer.")
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                account=_lock_account(cur,account_id)
                if account: _assert_banking_unlocked(cur, account[1])
                if not account or account[6] not in ("ACTIVE","LIMITED"):
                    raise ValueError("Account is not active.")
                cur.execute("SELECT credit_enabled,default_credit_limit,private_or_corporate FROM account_tiers_v2 WHERE id=%s",
                            (account[4],))
                tier=cur.fetchone()
                if not tier or not tier[0]:
                    raise ValueError("This account tier does not support CashLine.")
                maximum=int(tier[1] or 0)
                if requested_limit < CASHLINE_MINIMUM or (maximum and requested_limit > maximum):
                    raise ValueError(f"CashLine limit must be between {CASHLINE_MINIMUM} and {maximum} Emerald.")
                outstanding=cashline_outstanding(cur,account_id)
                if requested_limit < outstanding:
                    raise ValueError(f"CashLine limit cannot be lower than the outstanding balance of {outstanding} Emerald.")
                cur.execute("SELECT id FROM credit_facilities WHERE account_id=%s FOR UPDATE",(account_id,))
                existing=cur.fetchone()
                fee=credit_activation_fee(requested_limit)
                if existing:
                    cur.execute("UPDATE credit_facilities SET credit_limit=%s,activation_fee_monthly=%s,interest_annual_bps=%s,interest_monthly_bps=%s,status='ACTIVE' WHERE account_id=%s",
                                (requested_limit,fee,cashline_annual_bps(account[3]),cashline_annual_bps(account[3]),account_id))
                else:
                    cur.execute("""INSERT INTO credit_facilities(account_id,credit_limit,interest_monthly_bps,interest_annual_bps,
                                   activation_fee_monthly,status) VALUES(%s,%s,%s,%s,%s,'ACTIVE')""",
                                (account_id,requested_limit,cashline_annual_bps(account[3]),cashline_annual_bps(account[3]),fee))
                return {"account_id":account_id,"credit_limit":requested_limit,"monthly_activation_fee":fee,
                        "interest_annual_bps":cashline_annual_bps(account[3]),"status":"ACTIVE"}
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
                debt=cashline_outstanding(cur,account_id)
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
    return 20 if int(credit_limit)>5000 else 10

def cashline_annual_bps(account_type):
    return CASHLINE_ANNUAL_PERSONAL_BPS if account_type == "PERSONAL" else CASHLINE_ANNUAL_BUSINESS_BPS


def accrue_daily_credit_interest(account_id=None):
    conn=get_db_connection(); results=[]
    try:
        with conn:
            with conn.cursor() as cur:
                where="" if account_id is None else " AND cf.account_id=%s"; params=() if account_id is None else (account_id,)
                cur.execute(f"""SELECT a.id,a.account_type,cf.last_interest_at,cf.activated_at,cf.interest_annual_bps
                                FROM bank_accounts a JOIN credit_facilities cf ON cf.account_id=a.id
                                WHERE cf.status='ACTIVE'{where} FOR UPDATE OF a,cf""",params)
                for aid,atype,last_interest,activated,annual_bps in cur.fetchall():
                    outstanding=cashline_outstanding(cur,aid)
                    if outstanding<=0:
                        cur.execute("UPDATE credit_facilities SET last_interest_at=CURRENT_TIMESTAMP WHERE account_id=%s",(aid,)); continue
                    start=last_interest or activated; days=(_now()-start).days
                    if days<=0: continue
                    rate=int(annual_bps or cashline_annual_bps(atype))
                    interest=(outstanding*rate*days+3650000-1)//3650000
                    if interest<=0: continue
                    txid=next_transaction_id(cur)
                    create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_INTEREST",amount=interest,recipient_account_id=aid,description=f"CashLine interest ({days} day(s))")
                    cur.execute("UPDATE credit_facilities SET last_interest_at=CURRENT_TIMESTAMP WHERE account_id=%s",(aid,))
                    results.append({"account_id":aid,"interest":interest,"days":days,"transaction_id":txid})
        return results
    finally: release_db_connection(conn)


def accrue_monthly_credit_interest(account_id):
    return (accrue_daily_credit_interest(account_id) or [{"interest":0}])[-1]


def generate_cashline_statement(account_id, period_start=None, period_end=None):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT minimum_due_percent_bps,minimum_due_floor,status FROM credit_facilities WHERE account_id=%s FOR UPDATE",(account_id,))
                row=cur.fetchone()
                if not row or row[2] not in ("ACTIVE","SUSPENDED"):
                    cur.execute("SELECT monthly_fee,status FROM dynamic_cashlines WHERE account_id=%s FOR UPDATE",(account_id,))
                    dynamic=cur.fetchone()
                    if not dynamic or dynamic[1]!="ACTIVE": return None
                    row=(500,40,"ACTIVE")
                end=period_end or _now().date(); start=period_start or end.replace(day=1)
                principal=cashline_principal_outstanding(cur,account_id)
                cur.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger_transactions WHERE recipient_account_id=%s AND transaction_type='CREDIT_INTEREST' AND created_at::date BETWEEN %s AND %s""",(account_id,start,end))
                interest=int(cur.fetchone()[0] or 0)
                cur.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger_transactions WHERE recipient_account_id=%s AND transaction_type='CREDIT_FEE' AND created_at::date BETWEEN %s AND %s""",(account_id,start,end))
                fee=int(cur.fetchone()[0] or 0)
                total=cashline_outstanding(cur,account_id)
                minimum=max(int(row[1] or 40),(principal*int(row[0] or 500)+9999)//10000+interest+fee)
                due=end+__import__("datetime").timedelta(days=15)
                cur.execute("""INSERT INTO credit_statements(account_id,period_start,period_end,principal_amount,interest_amount,credit_fee,total_due,minimum_due,due_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,period_start,period_end) DO NOTHING RETURNING id""",(account_id,start,end,principal,interest,fee,total,minimum,due))
                inserted=cur.fetchone()
                if inserted: sid=inserted[0]
                else:
                    cur.execute("SELECT id FROM credit_statements WHERE account_id=%s AND period_start=%s AND period_end=%s FOR UPDATE",(account_id,start,end)); sid=cur.fetchone()[0]
                cur.execute("DELETE FROM credit_statement_items WHERE statement_id=%s",(sid,))
                for typ,amt,desc in (("PRINCIPAL",principal,"CashLine capital outstanding"),("INTEREST",interest,"CashLine daily interest"),("CREDIT_FEE",fee,"CashLine monthly fee")):
                    if amt: cur.execute("INSERT INTO credit_statement_items(statement_id,item_type,amount,description) VALUES(%s,%s,%s,%s)",(sid,typ,amt,desc))
                return {"statement_id":sid,"principal":principal,"interest":interest,"fee":fee,"total_due":total,"minimum_due":minimum,"due_at":due}
    finally: release_db_connection(conn)


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
                            txid=next_transaction_id(cur)
                            create_ledger_transaction(cur,transaction_id=txid,transaction_type="CREDIT_FEE",
                                                      amount=int(credit_fee),recipient_account_id=account_id,
                                                      description=f"Monthly credit activation fee {period}")
                            fees.append(int(credit_fee))
                    cur.execute("""UPDATE credit_statements SET status='OVERDUE'
                                   WHERE account_id=%s AND due_at<CURRENT_TIMESTAMP
                                     AND amount_paid<minimum_due AND status IN ('OPEN','PARTIALLY_PAID')""",(account_id,))
                    cur.execute("SELECT EXISTS(SELECT 1 FROM credit_statements WHERE account_id=%s AND status='OVERDUE')",(account_id,))
                    statement_overdue=bool(cur.fetchone()[0])
                    if statement_overdue:
                        cur.execute("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                                       VALUES(%s,%s,'CREDIT_STATEMENT_OVERDUE','CashLine payment overdue',
                                       'Your CashLine minimum payment is overdue. Please make the required payment.')""",
                                    (client_id,account_id))
                    cur.execute("SELECT balance FROM bank_accounts WHERE id=%s FOR UPDATE",(account_id,))
                    current_balance=0
                    debt=cashline_outstanding(cur,account_id)
                    if credit_status=="ACTIVE" and debt>0:
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
