"""Small environment helpers kept for compatibility with older MineBank imports."""
import os

DATABASE_ENV_NAMES = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
)

# Compatibility name for legacy bank_lib modules. The active MineBank code
# intentionally does not use a process-wide connection pool.
DB_POOL = None


def get_database_url():
    for name in DATABASE_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""
