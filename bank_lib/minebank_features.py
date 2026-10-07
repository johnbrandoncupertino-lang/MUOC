"""MineBank client experience helpers: recipients, templates, schedules, preferences and analytics."""
import json
from datetime import datetime, timezone
from bank_lib.database import execute_query, execute_query_dict

def save_recipient(client_id, account_number, nickname):
    row=execute_query("""INSERT INTO minebank_saved_recipients(client_id,account_number,nickname)
                         VALUES(%s,%s,%s)
                         ON CONFLICT(client_id,account_number) DO UPDATE SET nickname=EXCLUDED.nickname
                         RETURNING id""",(client_id,account_number,nickname.strip()),commit=True)
    return row[0][0] if row else None

def delete_recipient(client_id, recipient_id):
    execute_query("DELETE FROM minebank_saved_recipients WHERE id=%s AND client_id=%s",(recipient_id,client_id),commit=True)

def list_recipients(client_id):
    return execute_query_dict("""SELECT id,account_number,nickname,created_at
                                 FROM minebank_saved_recipients WHERE client_id=%s
                                 ORDER BY nickname""",(client_id,))

def save_transfer_template(client_id, name, recipient_account_number, amount=None, description=None, reference=None):
    row=execute_query("""INSERT INTO minebank_transfer_templates(client_id,name,recipient_account_number,amount,description,reference)
                         VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (client_id,name.strip(),recipient_account_number.strip(),amount,description,reference),commit=True)
    return row[0][0] if row else None

def list_transfer_templates(client_id):
    return execute_query_dict("""SELECT id,name,recipient_account_number,amount,description,reference,created_at
                                 FROM minebank_transfer_templates WHERE client_id=%s ORDER BY name""",(client_id,))

def delete_transfer_template(client_id, template_id):
    execute_query("DELETE FROM minebank_transfer_templates WHERE id=%s AND client_id=%s",(template_id,client_id),commit=True)

def create_scheduled_transfer(client_id, account_id, recipient_account_number, amount, schedule_type,
                              next_run_at, end_at=None, description=None, reference=None):
    row=execute_query("""INSERT INTO minebank_scheduled_transfers
                         (client_id,account_id,recipient_account_number,amount,schedule_type,next_run_at,end_at,description,reference)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (client_id,account_id,recipient_account_number,amount,schedule_type,next_run_at,end_at,description,reference),commit=True)
    return row[0][0] if row else None

def list_scheduled_transfers(client_id):
    return execute_query_dict("""SELECT id,account_id,recipient_account_number,amount,schedule_type,next_run_at,end_at,
                                        description,reference,status,created_at
                                 FROM minebank_scheduled_transfers WHERE client_id=%s
                                 ORDER BY status,next_run_at""",(client_id,))

def set_scheduled_transfer_status(client_id, schedule_id, status):
    execute_query("""UPDATE minebank_scheduled_transfers SET status=%s
                     WHERE id=%s AND client_id=%s""",(status,schedule_id,client_id),commit=True)

def get_notification_preferences(client_id):
    rows=execute_query_dict("""SELECT email_enabled,transfer_email,security_email,request_email,statement_email
                               FROM minebank_notification_preferences WHERE client_id=%s""",(client_id,))
    return rows[0] if rows else {"email_enabled":False,"transfer_email":True,"security_email":True,
                                 "request_email":True,"statement_email":True}

def save_notification_preferences(client_id, email_enabled, transfer_email, security_email, request_email, statement_email):
    execute_query("""INSERT INTO minebank_notification_preferences
                     (client_id,email_enabled,transfer_email,security_email,request_email,statement_email)
                     VALUES(%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(client_id) DO UPDATE SET email_enabled=EXCLUDED.email_enabled,
                       transfer_email=EXCLUDED.transfer_email,security_email=EXCLUDED.security_email,
                       request_email=EXCLUDED.request_email,statement_email=EXCLUDED.statement_email""",
                  (client_id,email_enabled,transfer_email,security_email,request_email,statement_email),commit=True)

def list_notifications(client_id, limit=100):
    return execute_query_dict("""SELECT id,notification_type,title,message,read_at,created_at
                                 FROM bank_notifications WHERE client_id=%s
                                 ORDER BY created_at DESC LIMIT %s""",(client_id,limit))

def mark_notification_read(client_id, notification_id):
    execute_query("""UPDATE bank_notifications SET read_at=CURRENT_TIMESTAMP
                     WHERE id=%s AND client_id=%s""",(notification_id,client_id),commit=True)

def queue_email(client_id, notification_type, subject, body):
    prefs=get_notification_preferences(client_id)
    if not prefs["email_enabled"]:
        return None
    allowed={"TRANSFER":prefs["transfer_email"],"SECURITY":prefs["security_email"],
             "REQUEST":prefs["request_email"],"STATEMENT":prefs["statement_email"]}
    if not allowed.get(notification_type, True):
        return None
    row=execute_query("""SELECT email FROM bank_clients WHERE id=%s""",(client_id,))
    if not row:
        return None
    out=execute_query("""INSERT INTO minebank_email_outbox(client_id,email,notification_type,subject,body)
                         VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                      (client_id,row[0][0],notification_type,subject,body),commit=True)
    return out[0][0] if out else None

def analytics_for_account(account_id):
    rows=execute_query_dict("""SELECT transaction_type,
                                      COALESCE(SUM(CASE WHEN recipient_account_id=%s THEN amount ELSE 0 END),0) incoming,
                                      COALESCE(SUM(CASE WHEN sender_account_id=%s THEN amount+fee ELSE 0 END),0) outgoing,
                                      COUNT(*) operations
                               FROM ledger_transactions
                               WHERE (sender_account_id=%s OR recipient_account_id=%s)
                                 AND status='COMPLETED'
                                 AND created_at>=date_trunc('month',CURRENT_TIMESTAMP)
                               GROUP BY transaction_type ORDER BY operations DESC""",
                            (account_id,account_id,account_id,account_id))
    totals=execute_query("""SELECT COALESCE(SUM(CASE WHEN recipient_account_id=%s THEN amount ELSE 0 END),0),
                                   COALESCE(SUM(CASE WHEN sender_account_id=%s THEN amount+fee ELSE 0 END),0),
                                   COUNT(*)
                            FROM ledger_transactions
                            WHERE (sender_account_id=%s OR recipient_account_id=%s)
                              AND status='COMPLETED'
                              AND created_at>=date_trunc('month',CURRENT_TIMESTAMP)""",
                         (account_id,account_id,account_id,account_id))
    return {"by_type":rows,"incoming":totals[0][0] if totals else 0,
            "outgoing":totals[0][1] if totals else 0,"operations":totals[0][2] if totals else 0}

def preview_transfer(client_id, account_id, recipient_account_number, amount):
    if int(amount) <= 0:
        raise ValueError("Transfer amount must be positive.")
    rows=execute_query_dict("""SELECT a.id,a.client_id,a.account_number,a.account_type,a.tier_id,a.balance,a.status,
                                      a.monthly_outgoing_used,a.last_outgoing_at,t.code,t.monthly_outgoing_limit
                               FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
                               WHERE a.id=%s AND a.client_id=%s AND a.status='ACTIVE'""",(account_id,client_id))
    if not rows:
        raise ValueError("Sender account is not available.")
    recipient=execute_query_dict("SELECT id,account_number,client_id,status FROM bank_accounts WHERE account_number=%s",(recipient_account_number,))
    if not recipient or recipient[0]["id"] == account_id:
        raise ValueError("Recipient account is invalid.")
    sender=rows[0]
    fee=0 if sender["code"] in ("PERSONAL_PRIVATE","CORPORATE") or int(recipient[0]["client_id"])==int(sender["client_id"]) else (0 if int(amount)<=500 else 5)
    return {"amount":int(amount),"fee":fee,"total":int(amount)+fee,
            "recipient_account_number":recipient_account_number,"recipient_id":recipient[0]["id"],
            "remaining_balance":int(sender["balance"])-int(amount)-fee}
