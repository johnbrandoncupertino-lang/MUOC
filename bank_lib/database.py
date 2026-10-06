import psycopg2
import psycopg2.extras

from .global_vars import DB_POOL


# Database helper functions
# This module provides functions to connect to a PostgreSQL database,

# Get a connection from the pool
def get_db_connection():
    """Get a connection from the pool"""
    if DB_POOL is None:
        return None
    try:
        return DB_POOL.getconn()
    except Exception as e:
        print(f"Error getting database connection: {e}")
        return None


# Release a connection back to the pool
def release_db_connection(conn):
    """Release a connection back to the pool"""
    if DB_POOL is not None:
        DB_POOL.putconn(conn)


# Initialize database tables
def init_db():
    """Create database tables if they don't exist"""
    if DB_POOL is None:
        return False

    try:
        # Settings table
        execute_query("""
                      CREATE TABLE IF NOT EXISTS settings
                      (
                          id
                          SERIAL
                          PRIMARY
                          KEY,
                          bank_name
                          VARCHAR
                      (
                          100
                      ) NOT NULL,
                          currency_name VARCHAR
                      (
                          50
                      ) NOT NULL,
                          admin_password VARCHAR
                      (
                          200
                      ) NOT NULL,
                          allow_leaderboard BOOLEAN DEFAULT TRUE,
                          allow_public_logs BOOLEAN DEFAULT TRUE,
                          allow_debts BOOLEAN DEFAULT FALSE,
                          allow_self_review BOOLEAN DEFAULT FALSE,
                          maximum_currency FLOAT DEFAULT 1000000.0
                          )
                      """, commit=True)

        # Users table
        execute_query("""
                      CREATE TABLE IF NOT EXISTS users
                      (
                          id
                          SERIAL
                          PRIMARY
                          KEY,
                          wallet_name
                          VARCHAR
                      (
                          100
                      ) UNIQUE NOT NULL,
                          password VARCHAR
                      (
                          200
                      ) NOT NULL,
                          current_currency FLOAT DEFAULT 0.0,
                          is_frozen BOOLEAN DEFAULT FALSE,
                          created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                          last_login TIMESTAMP WITH TIME ZONE
                                                   )
                      """, commit=True)

        # Security, account tier, credit, and billing extensions
        execute_query("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS account_tier VARCHAR(30) NOT NULL DEFAULT 'Personal',
            ADD COLUMN IF NOT EXISTS credit_limit DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS credit_used DOUBLE PRECISION NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP WITH TIME ZONE
        """, commit=True)

        execute_query("""
            CREATE TABLE IF NOT EXISTS account_tiers
            (
                id SERIAL PRIMARY KEY,
                name VARCHAR(30) UNIQUE NOT NULL,
                monthly_fee DOUBLE PRECISION NOT NULL DEFAULT 0,
                credit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                default_credit_limit DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """, commit=True)

        execute_query("""
            INSERT INTO account_tiers (name, monthly_fee, credit_enabled, default_credit_limit)
            VALUES
                ('Personal', 0, TRUE, 1000),
                ('Business', 25, TRUE, 10000),
                ('Gold', 50, TRUE, 25000)
            ON CONFLICT (name) DO NOTHING
        """, commit=True)

        execute_query("""
            CREATE TABLE IF NOT EXISTS account_charges
            (
                id SERIAL PRIMARY KEY,
                wallet_name VARCHAR(100) NOT NULL,
                tier_name VARCHAR(30) NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                billing_period VARCHAR(7) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Charged',
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(wallet_name, billing_period)
            )
        """, commit=True)

        execute_query("""
            CREATE INDEX IF NOT EXISTS idx_account_charges_wallet
            ON account_charges(wallet_name, timestamp DESC)
        """, commit=True)

        # Logs table
        execute_query("""
                      CREATE TABLE IF NOT EXISTS logs
                      (
                          id
                          SERIAL
                          PRIMARY
                          KEY,
                          action
                          VARCHAR
                      (
                          100
                      ) NOT NULL,
                          details VARCHAR
                      (
                          500
                      ) NOT NULL,
                          timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                                  private_level VARCHAR (20) NOT NULL,
                          ip_address VARCHAR
                      (
                          45
                      )
                          )
                      """, commit=True)

        # Logs archive table
        execute_query("""
                      CREATE TABLE IF NOT EXISTS logs_archive
                      (
                          id
                          SERIAL
                          PRIMARY
                          KEY,
                          action
                          VARCHAR
                      (
                          100
                      ) NOT NULL,
                          details VARCHAR
                      (
                          500
                      ) NOT NULL,
                          timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                                  private_level VARCHAR (20) NOT NULL,
                          ip_address VARCHAR
                      (
                          45
                      )
                          )
                      """, commit=True)

        # Requests table
        execute_query("""
                      CREATE TABLE IF NOT EXISTS requests
                      (
                          id
                          SERIAL
                          PRIMARY
                          KEY,
                          request_type
                          VARCHAR
                      (
                          50
                      ) NOT NULL,
                          ticket_uuid VARCHAR
                      (
                          100
                      ) UNIQUE NOT NULL,
                          wallet_name VARCHAR
                      (
                          100
                      ) NOT NULL,
                          timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                                  category VARCHAR (50),
                          status VARCHAR
                      (
                          20
                      ) DEFAULT 'Pending',
                          reason VARCHAR
                      (
                          500
                      ),
                          ip_address VARCHAR
                      (
                          45
                      )
                          )
                      """, commit=True)

        from .minebank_schema import init_minebank_v2
        init_minebank_v2()
        return True
    except Exception as e:
        print(f"Error initializing database: {e}")
        return False


# Check database connection
def check_db_connection():
    """Check if database connection is working"""
    if DB_POOL is None:
        return False

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return False

        # Try a simple query to verify connection
        cur = conn.cursor()
        cur.execute("SELECT 1")
        result = cur.fetchone()
        cur.close()

        return result is not None and result[0] == 1
    except Exception as e:
        print(f"Database connection check failed: {e}")
        return False
    finally:
        if conn:
            release_db_connection(conn)


# Execute a query
def execute_query(query, params=None, fetch=True, commit=False, cursor_factory=None):
    """Execute a database query with proper connection handling"""
    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return None

        if cursor_factory:
            cur = conn.cursor(cursor_factory=cursor_factory)
        else:
            cur = conn.cursor()

        cur.execute(query, params)

        result = None
        if fetch:
            # Only fetch results for SELECT queries
            if cur.description:  # cur.description is None for non-SELECT queries
                result = cur.fetchall()

        if commit:
            conn.commit()

        cur.close()
        return result
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Database error: {e}")
        raise
    finally:
        if conn:
            release_db_connection(conn)


# Execute a query and return results as dictionaries
def execute_query_dict(query, params=None, fetch=True, commit=False):
    """Execute a query and return results as dictionaries"""
    return execute_query(query, params, fetch, commit, cursor_factory=psycopg2.extras.RealDictCursor)


# Check if the database is initialized
def is_db_initialized():
    """Check if the database has been initialized with settings"""
    if DB_POOL is None:
        return False

    try:
        # Check if settings table exists (Postgres example)
        result = execute_query(
            "SELECT to_regclass('public.settings')"
        )
        if not result or not result[0][0]:
            return False

        # Table exists, now check for rows
        result = execute_query("SELECT COUNT(*) FROM settings")
        return result and result[0][0] > 0

    except Exception as e:
        print(f"Error checking if DB is initialized: {e}")
        return False
