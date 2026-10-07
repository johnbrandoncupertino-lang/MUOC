"""Small, serverless-safe PostgreSQL helpers for MineBank.

The portal uses one short-lived PostgreSQL connection per query. This is
deliberately simpler and more reliable on Vercel than keeping a process-wide
connection pool alive across warm/cold serverless invocations.
"""
import os

import psycopg2
import psycopg2.extras


def database_url():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("POSTGRES_PRISMA_URL")
        or os.environ.get("POSTGRES_URL_NON_POOLING")
        or ""
    ).strip()


def get_db_connection():
    url = database_url()
    if not url:
        print("MineBank database URL is not configured")
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=8)
        conn.autocommit = False
        return conn
    except Exception as exc:
        print(f"MineBank database connection failed: {type(exc).__name__}: {exc}")
        return None


def release_db_connection(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def execute_query(query, params=None, fetch=True, commit=False, cursor_factory=None):
    conn = get_db_connection()
    if conn is None:
        return None
    cur = None
    try:
        cur = conn.cursor(cursor_factory=cursor_factory) if cursor_factory else conn.cursor()
        cur.execute(query, params)
        result = cur.fetchall() if fetch and cur.description else None
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"Database error: {type(exc).__name__}: {exc}")
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        release_db_connection(conn)


def execute_query_dict(query, params=None, fetch=True, commit=False):
    return execute_query(
        query,
        params=params,
        fetch=fetch,
        commit=commit,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def init_db():
    return ensure_minebank_schema()


_SCHEMA_READY = False


def ensure_minebank_schema():
    """Bootstrap/migrate MineBank, but never repeat DDL on an initialized database."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True

    # Existing production databases already contain the MineBank core tables.
    # Checking the catalog is dramatically cheaper than replaying CREATE/ALTER
    # statements during login or every cold request.
    try:
        rows = execute_query("""
            SELECT
                to_regclass('public.bank_clients') IS NOT NULL
                AND to_regclass('public.bank_accounts') IS NOT NULL
                AND to_regclass('public.account_tiers_v2') IS NOT NULL
                AND to_regclass('public.ledger_transactions') IS NOT NULL
                AND to_regclass('public.bank_notifications') IS NOT NULL
        """)
        if rows and rows[0][0]:
            _SCHEMA_READY = True
            return True
    except Exception as exc:
        print(f"MineBank schema probe warning: {type(exc).__name__}: {exc}")

    if not check_db_connection():
        return False

    try:
        execute_query(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id SERIAL PRIMARY KEY,
                bank_name VARCHAR(100) NOT NULL,
                currency_name VARCHAR(50) NOT NULL,
                admin_password VARCHAR(200) NOT NULL DEFAULT '',
                allow_leaderboard BOOLEAN DEFAULT TRUE,
                allow_public_logs BOOLEAN DEFAULT TRUE,
                allow_debts BOOLEAN DEFAULT FALSE,
                allow_self_review BOOLEAN DEFAULT FALSE,
                maximum_currency DOUBLE PRECISION DEFAULT 1000000.0
            )
            """,
            fetch=False,
            commit=True,
        )

        from .minebank_schema import init_minebank_v2
        init_minebank_v2()

        # Security tables/columns are part of the base login system.
        from .minebank_security import ensure_security_schema
        ensure_security_schema()

        _SCHEMA_READY = True
        return True
    except Exception as exc:
        print(f"MineBank schema initialization warning: {type(exc).__name__}: {exc}")
        return False


def check_db_connection():
    conn = get_db_connection()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)
    except Exception as exc:
        print(f"Database health check failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        release_db_connection(conn)


def is_db_initialized():
    try:
        rows = execute_query("SELECT COUNT(*) FROM settings")
        return bool(rows and rows[0][0] > 0)
    except Exception:
        return False
