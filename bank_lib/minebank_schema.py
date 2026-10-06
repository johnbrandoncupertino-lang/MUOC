from pathlib import Path

from .database import execute_query


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "extras" / "minebank_v2.sql"


def init_minebank_v2():
    """Apply the MineBank schema one statement at a time.

    Older production databases may already contain part of the schema. One
    failed optional migration must not prevent the remaining idempotent
    statements from running.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    errors = []

    for statement in statements:
        try:
            execute_query(statement, fetch=False, commit=True)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    # The portal cannot operate without these four core tables.
    core_tables = execute_query(
        """
        SELECT to_regclass('public.bank_clients'),
               to_regclass('public.account_tiers_v2'),
               to_regclass('public.bank_accounts'),
               to_regclass('public.ledger_transactions')
        """
    )
    if not core_tables or any(value is None for value in core_tables[0]):
        raise RuntimeError("MineBank core database tables are missing.")

    if errors:
        print(f"MineBank schema completed with {len(errors)} non-fatal migration warning(s).")

    return True
