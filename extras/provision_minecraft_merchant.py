"""Provision a dedicated MineBank Minecraft merchant credential.

Run locally from the repository root after applying extras/minebank_minecraft.sql.
The raw token is printed once and must be stored only on the trusted merchant terminal.
"""
import argparse
import hashlib
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_lib.database import get_db_connection, release_db_connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", type=int, required=True, help="MineBank client that owns the merchant account")
    parser.add_argument("--account-id", type=int, required=True, help="Active MineBank account that receives shop payments")
    parser.add_argument("--name", required=True, help="Merchant display name")
    args = parser.parse_args()

    name = args.name.strip()[:120]
    if not name:
        parser.error("--name must not be empty")

    token = "mbmerchant_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db_connection()
    if conn is None:
        raise SystemExit("MineBank database is unavailable; no merchant credential was created.")
    created = False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT a.id,a.client_id,a.status,c.status,a.account_type
                       FROM bank_accounts a
                       JOIN bank_clients c ON c.id=a.client_id
                       WHERE a.id=%s AND a.client_id=%s
                         AND a.account_type='BUSINESS'
                         AND EXISTS (
                             SELECT 1 FROM minebank_business_members bm
                             WHERE bm.account_id=a.id AND bm.client_id=c.id AND bm.role='OWNER'
                         )
                       FOR UPDATE OF a,c""",
                    (args.account_id, args.client_id),
                )
                row = cur.fetchone()
                if not row or row[2] != "ACTIVE" or row[3] != "ACTIVE" or row[4] != "BUSINESS":
                    raise SystemExit("The account must be an active Business account owned by the specified active client.")
                cur.execute(
                    """INSERT INTO minebank_minecraft_merchants
                       (client_id,account_id,token_hash,display_name,active)
                       VALUES(%s,%s,%s,%s,TRUE)
                       RETURNING id""",
                    (args.client_id, args.account_id, token_hash, name),
                )
                merchant_id = cur.fetchone()[0]
                created = True
        print("Merchant credential created.")
        print("Merchant ID:", merchant_id)
        print("Merchant name:", name)
        print("Receiving account ID:", args.account_id)
        print("TOKEN (shown once; copy securely now):")
        print(token)
        print("Never commit this token or place it on a player-accessible computer.")
    finally:
        release_db_connection(conn)


if __name__ == "__main__":
    main()
