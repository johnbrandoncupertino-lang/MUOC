"""MineBank scheduled transfer executor."""
from datetime import timedelta
from .database import execute_query, execute_query_dict
from .minebank_core import transfer

def process_due_scheduled_transfers(limit=50):
    rows=execute_query_dict("""SELECT id,client_id,account_id,recipient_account_number,amount,schedule_type,
                                      next_run_at,end_at,description,reference,recurrence_config,funding_source
                               FROM minebank_scheduled_transfers
                               WHERE status='ACTIVE' AND next_run_at<=CURRENT_TIMESTAMP
                                 AND (last_attempt_at IS NULL OR last_attempt_at<CURRENT_TIMESTAMP-INTERVAL '4 minutes')
                               ORDER BY next_run_at LIMIT %s""",(limit,))
    results=[]
    for row in rows:
        sid=row["id"]; run_at=row["next_run_at"]
        execute_query("UPDATE minebank_scheduled_transfers SET last_attempt_at=CURRENT_TIMESTAMP WHERE id=%s",(sid,),commit=True)
        try:
            result=transfer(sender_account_id=row["account_id"],
                            recipient_account_number=row["recipient_account_number"],
                            amount=int(row["amount"]),
                            description=row["description"],reference=row["reference"],
                            actor_client_id=row["client_id"],
                            idempotency_key=f"SCHEDULE-{sid}-{int(run_at.timestamp())}",
                            transfer_kind="SCHEDULED",extra_fee=1,funding_source=(row.get("funding_source") or "BALANCE"))
            if result["status"]=="COMPLETED":
                if row["schedule_type"]=="ONCE":
                    execute_query("UPDATE minebank_scheduled_transfers SET status='COMPLETED',last_error=NULL WHERE id=%s",(sid,),commit=True)
                else:
                    cfg=row["recurrence_config"] or {}
                    if row["schedule_type"]=="DAILY": delta=timedelta(days=1)
                    elif row["schedule_type"]=="WEEKLY": delta=timedelta(days=7)
                    elif row["schedule_type"]=="MONTHLY": delta=timedelta(days=30)
                    else: delta=timedelta(days=max(1,int(cfg.get("interval_days",1))))
                    nxt=run_at+delta
                    if row["end_at"] and nxt>row["end_at"]:
                        execute_query("UPDATE minebank_scheduled_transfers SET status='COMPLETED',last_error=NULL WHERE id=%s",(sid,),commit=True)
                    else:
                        execute_query("UPDATE minebank_scheduled_transfers SET next_run_at=%s,last_error=NULL WHERE id=%s",(nxt,sid),commit=True)
                results.append({"id":sid,"status":result["status"]})
            else:
                message=("Transfer "+str(result.get("transaction_id",""))+" requires bank approval.")
                execute_query("UPDATE minebank_scheduled_transfers SET status='PAUSED',last_error=%s WHERE id=%s",(message,sid),commit=True)
                results.append({"id":sid,"status":result["status"],"transaction_id":result.get("transaction_id")})
        except Exception as exc:
            message=str(exc)[:500]
            execute_query("UPDATE minebank_scheduled_transfers SET last_error=%s WHERE id=%s",(message,sid),commit=True)
            execute_query("""INSERT INTO bank_notifications(client_id,account_id,notification_type,title,message)
                             VALUES(%s,%s,'SCHEDULED_PAYMENT_FAILED','Scheduled payment needs attention',%s)""",
                         (row["client_id"],row["account_id"],"A scheduled payment could not be executed: "+message),commit=True)
            results.append({"id":sid,"status":"PENDING_FUNDS","error":message})
    return results
