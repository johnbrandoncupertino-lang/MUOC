"""Small environment helpers kept for compatibility with older MineBank imports."""
import os

DATABASE_ENV_NAMES = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
)


def get_database_url():
    for name in DATABASE_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""
