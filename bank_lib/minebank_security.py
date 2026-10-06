"""MineBank security services: progressive login protection, sessions, audit and risk state."""
import hashlib, json, secrets
from datetime import datetime, timedelta, timezone
from flask import request, session
from .database import execute_query, execute_query_dict

PASSWORD_MAX_AGE_DAYS = 180
INACTIVITY_MINUTES = 10
ABSOLUTE_SESSION_HOURS = 24

def now():
    return datetime.now(timezone.utc)

def ensure_security_schema():
    execute_query("""CREATE TABLE IF NOT EXISTS minebank_security_events (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
        event_type VARCHAR(60) NOT NULL,
        severity VARCHAR(20) NOT NULL DEFAULT 'INFO',
        context JSONB NOT NULL DEFAULT '{}'::jsonb,
        ip_address VARCHAR(64),
        user_agent VARCHAR(500),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""", commit=True)
    execute_query("""CREATE INDEX IF NOT EXISTS minebank_security_events_client_idx
        ON minebank_security_events(client_id,created_at DESC)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS minebank_sessions (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
        session_token_hash VARCHAR(128) UNIQUE NOT NULL,
        ip_address VARCHAR(64),
        user_agent VARCHAR(500),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ
    )""", commit=True)
    execute_query("""CREATE INDEX IF NOT EXISTS minebank_sessions_client_idx
        ON minebank_sessions(client_id,revoked_at,last_activity_at DESC)""", commit=True)
    execute_query("""CREATE TABLE IF NOT EXISTS minebank_risk_events (
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT REFERENCES bank_accounts(id) ON DELETE SET NULL,
        client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
        risk_score INTEGER NOT NULL,
        severity VARCHAR(20) NOT NULL,
        reason VARCHAR(1000),
        context JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""", commit=True)
    execute_query("""CREATE INDEX IF NOT EXISTS minebank_risk_events_client_idx
        ON minebank_risk_events(client_id,created_at DESC)""", commit=True)
    execute_query("""ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS freeze_type VARCHAR(40)""", commit=True)
    execute_query("""ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP""", commit=True)
    execute_query("""ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_failed_attempts INTEGER NOT NULL DEFAULT 0""", commit=True)
    execute_query("""ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_blocked_until TIMESTAMPTZ""", commit=True)
    execute_query("""ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_captcha_required BOOLEAN NOT NULL DEFAULT FALSE""", commit=True)

def client_session_hash():
    token = session.get("minebank_session_token")
    return hashlib.sha256(token.encode()).hexdigest() if token else None

def create_session(client_id):
    token = secrets.token_urlsafe(48)
    session["minebank_session_token"] = token
    execute_query("""INSERT INTO minebank_sessions(client_id,session_token_hash,ip_address,user_agent,expires_at)
                     VALUES(%s,%s,%s,%s,%s)""",
                  (client_id,hashlib.sha256(token.encode()).hexdigest(),
                   (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
                   request.headers.get("User-Agent","")[:500],
                   now()+timedelta(hours=ABSOLUTE_SESSION_HOURS)),commit=True)

def revoke_current_session():
    h=client_session_hash()
    if h:
        execute_query("UPDATE minebank_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE session_token_hash=%s AND revoked_at IS NULL",(h,),commit=True)

def validate_session():
    cid=session.get("minebank_client_id")
    h=client_session_hash()
    if not cid or not h:
        return True
    rows=execute_query_dict("""SELECT s.id,s.created_at,s.last_activity_at,s.expires_at,s.revoked_at,c.status,c.password_changed_at
                               FROM minebank_sessions s JOIN bank_clients c ON c.id=s.client_id
                               WHERE s.client_id=%s AND s.session_token_hash=%s""",(cid,h))
    if not rows:
        session.clear(); return False
    s=rows[0]; current=now()
    if s["revoked_at"] or s["expires_at"] <= current:
        session.clear(); return False
    last=s["last_activity_at"]
    if last and (current-last).total_seconds() > INACTIVITY_MINUTES*60:
        execute_query("UPDATE minebank_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE id=%s",(s["id"],),commit=True)
        session.clear(); return False
    execute_query("UPDATE minebank_sessions SET last_activity_at=CURRENT_TIMESTAMP WHERE id=%s",(s["id"],),commit=True)
    return True

def security_event(client_id,event_type,severity="INFO",context=None):
    execute_query("""INSERT INTO minebank_security_events(client_id,event_type,severity,context,ip_address,user_agent)
                     VALUES(%s,%s,%s,%s::jsonb,%s,%s)""",
                  (client_id,event_type,severity,json.dumps(context or {}),
                   (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
                   request.headers.get("User-Agent","")[:500]),commit=True)

def login_challenge_required(client_id=None):
    if not client_id:
        return bool(session.get("login_captcha_required"))
    rows=execute_query_dict("SELECT login_captcha_required,login_blocked_until FROM bank_clients WHERE id=%s",(client_id,))
    if not rows: return False
    blocked=rows[0]["login_blocked_until"]
    if blocked and blocked <= now():
        execute_query("UPDATE bank_clients SET login_blocked_until=NULL WHERE id=%s",(client_id,),commit=True)
        blocked=None
    return bool(rows[0]["login_captcha_required"]) or bool(blocked and blocked>now())

def set_login_challenge():
    a=secrets.randbelow(8)+2; b=secrets.randbelow(8)+2
    session["login_captcha_answer"]=str(a+b)
    session["login_captcha_question"]=f"What is {a} + {b}?"
    return session["login_captcha_question"]

def check_login_challenge(answer):
    expected=session.get("login_captcha_answer")
    ok=bool(expected and secrets.compare_digest(str(answer or "").strip(),expected))
    if ok:
        session.pop("login_captcha_answer",None); session.pop("login_captcha_question",None)
    return ok

def record_login_failure(client_id=None, reason="INVALID_CREDENTIALS"):
    if client_id:
        rows=execute_query_dict("SELECT login_failed_attempts FROM bank_clients WHERE id=%s",(client_id,))
        attempts=int(rows[0]["login_failed_attempts"] or 0)+1 if rows else 1
        if attempts>=3:
            duration=5 if attempts<6 else (15 if attempts<9 else 60)
            execute_query("""UPDATE bank_clients SET login_failed_attempts=%s,login_captcha_required=TRUE,
                             login_blocked_until=%s WHERE id=%s""",
                         (attempts,now()+timedelta(minutes=duration),client_id),commit=True)
            security_event(client_id,"LOGIN_TEMP_BLOCK","HIGH",{"attempts":attempts,"minutes":duration,"reason":reason})
            return attempts, duration
        execute_query("UPDATE bank_clients SET login_failed_attempts=%s,login_captcha_required=%s WHERE id=%s",
                      (attempts,attempts>=2,client_id),commit=True)
        security_event(client_id,"LOGIN_FAILURE","WARNING" if attempts>=2 else "INFO",{"attempts":attempts,"reason":reason})
        return attempts,0
    security_event(None,"LOGIN_FAILURE","INFO",{"reason":reason})
    return 0,0

def clear_login_failures(client_id):
    execute_query("""UPDATE bank_clients SET login_failed_attempts=0,login_captcha_required=FALSE,
                     login_blocked_until=NULL,last_login=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                     WHERE id=%s""",(client_id,),commit=True)
    security_event(client_id,"LOGIN_SUCCESS","INFO",{})

def list_active_sessions(client_id):
    return execute_query_dict("""SELECT id,ip_address,user_agent,created_at,last_activity_at,expires_at
                                 FROM minebank_sessions
                                 WHERE client_id=%s AND revoked_at IS NULL AND expires_at>CURRENT_TIMESTAMP
                                 ORDER BY last_activity_at DESC""",(client_id,))

def terminate_session(client_id, session_id):
    execute_query("UPDATE minebank_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE id=%s AND client_id=%s AND revoked_at IS NULL",
                  (session_id,client_id),commit=True)
    security_event(client_id,"SESSION_TERMINATED","INFO",{"session_id":session_id})

def terminate_other_sessions(client_id):
    current=client_session_hash()
    execute_query("""UPDATE minebank_sessions SET revoked_at=CURRENT_TIMESTAMP
                     WHERE client_id=%s AND revoked_at IS NULL AND session_token_hash<>%s""",
                  (client_id,current or ""),commit=True)
    security_event(client_id,"OTHER_SESSIONS_TERMINATED","INFO",{})

def suspicious_login_score(*, client_id, ip_address, user_agent):
    score=0; reasons=[]
    recent=execute_query_dict("""SELECT COUNT(*) count FROM minebank_security_events
                                 WHERE client_id=%s AND event_type='LOGIN_FAILURE'
                                 AND created_at>=CURRENT_TIMESTAMP-INTERVAL '30 minutes'""",(client_id,))
    if recent and int(recent[0]["count"])>=2:
        score+=30; reasons.append("Recent failed logins")
    prior=execute_query_dict("""SELECT COUNT(*) count FROM minebank_security_events
                                WHERE client_id=%s AND event_type='LOGIN_SUCCESS'
                                AND ip_address=%s AND created_at>=CURRENT_TIMESTAMP-INTERVAL '24 hours'""",(client_id,ip_address))
    if prior and int(prior[0]["count"])==0:
        score+=20; reasons.append("New login network")
    if score>=60: severity="CRITICAL"
    elif score>=30: severity="HIGH"
    elif score>0: severity="MEDIUM"
    else: severity="LOW"
    return score,severity,reasons
