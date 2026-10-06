import os

from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or os.environ.get("POSTGRES_PRISMA_URL") or os.environ.get("POSTGRES_URL_NON_POOLING") or "EMPTY").strip()
MAX_CONNECTION_POOL = 10
DB_POOL = None


def _init_pool():
    """Lazily initialize/reinitialize the PostgreSQL pool for serverless runtimes."""
    global DB_POOL
    if DB_POOL is not None:
        return DB_POOL
    if not DATABASE_URL or DATABASE_URL == "EMPTY":
        print("Database URL environment variable is missing")
        return None
    try:
        DB_POOL = ThreadedConnectionPool(1, MAX_CONNECTION_POOL, DATABASE_URL)
        print("Database connection pool initialized successfully")
    except Exception as err:
        DB_POOL = None
        print(f"Error initializing database connection pool ({type(err).__name__}): {err}")
    return DB_POOL


_init_pool()


def get_pool():
    return _init_pool()
