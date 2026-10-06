from pathlib import Path

from .database import execute_query


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "extras" / "minebank_v2.sql"


def init_minebank_v2():
    """Apply the MineBank v2 schema idempotently to the current database."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    for statement in statements:
        execute_query(statement, fetch=False, commit=True)
    return True
