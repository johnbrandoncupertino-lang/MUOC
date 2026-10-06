"""MineBank API v1 helpers for Minecraft/ComputerCraft and external clients."""
import hashlib
import secrets
from datetime import datetime, timezone
from werkzeug.security import check_password_hash, generate_password_hash
from .database import get_db_connection, release_db_connection


def create_api_credential(client_id, name, scopes, expires_at=None):
    raw = "mbk_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    digest = generate_password_hash(raw)
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO api_credentials(client_id,name,key_prefix,secret_hash,scopes,expires_at)
                               VALUES(%s,%s,%s,%s,%s::jsonb,%s) RETURNING id""",
                            (client_id,name,prefix,digest,__import__('json').dumps(scopes),expires_at))
                return {"id":cur.fetchone()[0],"key":raw,"key_prefix":prefix,"scopes":scopes,"expires_at":expires_at}
    finally:
        release_db_connection(conn)


def authenticate_api_credential(raw):
    if not raw or not raw.startswith("mbk_"):
        return None
    prefix=raw[:12]
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT id,client_id,secret_hash,scopes,expires_at FROM api_credentials
                               WHERE key_prefix=%s AND active=TRUE AND (expires_at IS NULL OR expires_at>CURRENT_TIMESTAMP)""",
                            (prefix,))
                for row in cur.fetchall():
                    if check_password_hash(row[2],raw):
                        return {"credential_id":row[0],"client_id":row[1],"scopes":row[3]}
                return None
    finally:
        release_db_connection(conn)


def revoke_api_credential(credential_id, client_id):
    conn=get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE api_credentials SET active=FALSE,revoked_at=CURRENT_TIMESTAMP WHERE id=%s AND client_id=%s",
                            (credential_id,client_id))
                return cur.rowcount == 1
    finally:
        release_db_connection(conn)
