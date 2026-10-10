"""Small, serverless-safe PostgreSQL helpers for MineBank.

Queries in a Flask request share one short-lived connection, which is closed
when the request ends. Calls outside a request still own and close their own
connection. This avoids repeated connection setup without a process-wide pool.
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


def _query_connection():
    """Reuse a connection only for the lifetime of the current Flask request."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            conn = getattr(g, "_minebank_query_connection", None)
            if conn is None:
                conn = get_db_connection()
                if conn is not None:
                    g._minebank_query_connection = conn
            return conn, True
    except (ImportError, RuntimeError):
        pass
    return get_db_connection(), False


def execute_query(query, params=None, fetch=True, commit=False, cursor_factory=None):
    conn, request_scoped = _query_connection()
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
        if request_scoped:
            # If the server closed a broken connection, let a later query in
            # the same request obtain a fresh one. Flask teardown owns cleanup.
            if getattr(conn, "closed", False):
                try:
                    from flask import g, has_request_context
                    if has_request_context() and getattr(g, "_minebank_query_connection", None) is conn:
                        g._minebank_query_connection = None
                except (ImportError, RuntimeError):
                    pass
        else:
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
            # Existing deployments need lightweight profile/payment migrations too.
            try:
                execute_query("""ALTER TABLE bank_clients
                    ADD COLUMN IF NOT EXISTS discord_username VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS state VARCHAR(120),
                    ADD COLUMN IF NOT EXISTS main_language VARCHAR(80)""", fetch=False, commit=True)
                execute_query("""ALTER TABLE minebank_scheduled_transfers
                    ADD COLUMN IF NOT EXISTS funding_source VARCHAR(20) NOT NULL DEFAULT 'BALANCE'""", fetch=False, commit=True)
            except Exception as exc:
                print(f"MineBank profile/payment migration warning: {type(exc).__name__}: {exc}")
            try:
                from .credicheck import ensure_credicheck_schema
                ensure_credicheck_schema()
            except Exception as exc:
                print(f"CrediCheck schema probe warning: {type(exc).__name__}: {exc}")
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
        from .credicheck import ensure_credicheck_schema
        ensure_credicheck_schema()

        _SCHEMA_READY = True
        return True
    except Exception as exc:
        print(f"MineBank schema initialization warning: {type(exc).__name__}: {exc}")
        return False


_MESSAGE_SCHEMA_READY = False

def ensure_message_schema():
    """Ensure customer-message campaign storage exists on older production databases."""
    global _MESSAGE_SCHEMA_READY
    if _MESSAGE_SCHEMA_READY:
        return True
    try:
        execute_query("""CREATE TABLE IF NOT EXISTS minebank_message_campaigns (
            id BIGSERIAL PRIMARY KEY,
            created_by BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            subject VARCHAR(200) NOT NULL,
            body TEXT NOT NULL,
            recipient_filter JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""", fetch=False, commit=True)
        _MESSAGE_SCHEMA_READY = True
        return True
    except Exception as exc:
        print(f"MineBank message schema warning: {type(exc).__name__}: {exc}")
        return False



_LOAN_SCHEMA_READY = False

def ensure_loan_schema():
    """Ensure the Loans product tables exist on older MineBank databases."""
    global _LOAN_SCHEMA_READY
    if _LOAN_SCHEMA_READY:
        return True
    try:
        execute_query("""CREATE TABLE IF NOT EXISTS minebank_loans (
            id BIGSERIAL PRIMARY KEY,
            loan_number VARCHAR(40) UNIQUE NOT NULL,
            client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
            account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
            destination_account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE RESTRICT,
            principal BIGINT NOT NULL CHECK (principal >= 10000 AND principal <= 50000000),
            term_days INTEGER NOT NULL CHECK (term_days IN (12,24,48,96,192)),
            annual_interest_bps INTEGER NOT NULL,
            total_interest BIGINT NOT NULL DEFAULT 0,
            total_cost BIGINT NOT NULL DEFAULT 0,
            request_fee BIGINT NOT NULL,
            installment_amount BIGINT NOT NULL,
            installments_paid INTEGER NOT NULL DEFAULT 0,
            next_due_date DATE,
            status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
            approval_mode VARCHAR(24) NOT NULL DEFAULT 'BANK_REVIEW',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            approved_by BIGINT REFERENCES bank_clients(id),
            notes VARCHAR(1000)
        )""", fetch=False, commit=True)
        execute_query("CREATE INDEX IF NOT EXISTS minebank_loans_client_idx ON minebank_loans(client_id,status,requested_at DESC)", fetch=False, commit=True)
        execute_query("CREATE INDEX IF NOT EXISTS minebank_loans_account_idx ON minebank_loans(account_id,status)", fetch=False, commit=True)
        _LOAN_SCHEMA_READY = True
        return True
    except Exception as exc:
        print(f"MineBank loan schema warning: {type(exc).__name__}: {exc}")
        return False

_TRANSACTION_SCHEMA_READY = False

def ensure_transaction_schema():
    """Ensure transaction and CashLine extensions exist on older production databases."""
    global _TRANSACTION_SCHEMA_READY
    if _TRANSACTION_SCHEMA_READY:
        return True
    try:
        execute_query("ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS causal VARCHAR(500)", fetch=False, commit=True)
        execute_query("ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS category VARCHAR(60)", fetch=False, commit=True)
        execute_query("ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS transfer_kind VARCHAR(30)", fetch=False, commit=True)
        execute_query("ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS interest_annual_bps INTEGER NOT NULL DEFAULT 1830", fetch=False, commit=True)
        execute_query("ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS minimum_due_percent_bps INTEGER NOT NULL DEFAULT 500", fetch=False, commit=True)
        execute_query("ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS minimum_due_floor INTEGER NOT NULL DEFAULT 40", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS minebank_transaction_categories (
            id BIGSERIAL PRIMARY KEY,
            transaction_id VARCHAR(32) NOT NULL REFERENCES ledger_transactions(transaction_id) ON DELETE CASCADE,
            account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
            category VARCHAR(60) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(transaction_id,account_id,category)
        )""", fetch=False, commit=True)
        execute_query("CREATE INDEX IF NOT EXISTS minebank_transaction_categories_account_idx ON minebank_transaction_categories(account_id,created_at DESC)", fetch=False, commit=True)
        execute_query("""CREATE TABLE IF NOT EXISTS credit_statements (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            principal_amount BIGINT NOT NULL DEFAULT 0,
            interest_amount BIGINT NOT NULL DEFAULT 0,
            credit_fee BIGINT NOT NULL DEFAULT 0,
            other_costs BIGINT NOT NULL DEFAULT 0,
            total_due BIGINT NOT NULL DEFAULT 0,
            minimum_due BIGINT NOT NULL DEFAULT 40,
            amount_paid BIGINT NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            issued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            due_at TIMESTAMPTZ NOT NULL,
            UNIQUE(account_id,period_start,period_end)
        )""", fetch=False, commit=True)
        _TRANSACTION_SCHEMA_READY = True
        return True
    except Exception as exc:
        print(f"MineBank transaction/CashLine schema warning: {type(exc).__name__}: {exc}")
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
