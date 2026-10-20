#!/usr/bin/env python3
"""Provision one MineClubs venue and trusted terminal.

Requires DATABASE_URL (or POSTGRES_URL). The generated terminal token is printed
once after commit; save it directly into the protected entrance computer config.
Only its SHA-256 hash is persisted in PostgreSQL.
"""
import argparse
import hashlib
import os
import re
import secrets
import sys

import psycopg2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="Unique venue code, e.g. padua-central")
    parser.add_argument("--name", required=True, help="Human-readable venue name")
    parser.add_argument("--terminal-label", default="Main entrance", help="Terminal label")
    parser.add_argument(
        "--activate", action="store_true",
        help="Actually create the venue/terminal as ACTIVE; without this flag, only validate arguments.",
    )
    args = parser.parse_args()

    code = args.code.strip().lower()
    name = args.name.strip()
    label = args.terminal_label.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", code):
        parser.error("--code must be 2-48 lowercase letters, digits, or hyphens, starting with a letter/digit")
    if not name or len(name) > 120:
        parser.error("--name must be 1-120 characters")
    if not label or len(label) > 80:
        parser.error("--terminal-label must be 1-80 characters")

    if not args.activate:
        print("DRY RUN — no database changes made.")
        print(f"Venue code: {code}")
        print(f"Venue name: {name}")
        print(f"Terminal label: {label}")
        print("Re-run with --activate to create the active venue and terminal.")
        return 0

    database_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
        or os.environ.get("POSTGRES_URL_NON_POOLING")
        or ""
    ).strip()
    if not database_url:
        print("ERROR: set DATABASE_URL (or a supported POSTGRES_URL variable).", file=sys.stderr)
        return 2

    token = "mbclub_" + secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = None
    try:
        conn = psycopg2.connect(database_url, connect_timeout=8)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id,status FROM minebank_club_locations WHERE code=%s FOR UPDATE",
                    (code,),
                )
                existing = cur.fetchone()
                if existing:
                    club_id, status = existing
                    if status != "ACTIVE":
                        raise RuntimeError(
                            f"Venue {code!r} already exists with status {status}; reactivate it through the reviewed operator process."
                        )
                else:
                    cur.execute(
                        """INSERT INTO minebank_club_locations(code,display_name,status)
                           VALUES(%s,%s,'ACTIVE') RETURNING id""",
                        (code, name),
                    )
                    club_id = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO minebank_club_terminals
                       (club_id,label,token_hash,status)
                       VALUES(%s,%s,%s,'ACTIVE') RETURNING id""",
                    (club_id, label, token_hash),
                )
                terminal_id = cur.fetchone()[0]
        # Do not print the plaintext token until PostgreSQL confirms the commit.
        print("Provisioning committed.")
        print(f"Venue ID: {club_id}")
        print(f"Terminal ID: {terminal_id}")
        print(f"Venue code: {code}")
        print(f"Terminal label: {label}")
        print("")
        print("ONE-TIME TERMINAL TOKEN — copy it to the protected entrance computer now:")
        print(token)
        print("")
        print("The plaintext token is not stored in MineBank's database. Do not put it in source control, chat, or logs.")
        return 0
    except Exception as exc:
        print(f"ERROR: provisioning failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
