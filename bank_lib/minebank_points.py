"""Transactional MinePoints ledger primitives.

This module deliberately does not define earning rates, qualifying events, reward
prices, or customer-facing redemption routes. Those rules are not approved yet.
MinePoints are kept entirely separate from Emerald balances and credit products.
"""
from bank_lib.database import get_db_connection, release_db_connection

_ENTRY_TYPES = {"AWARD", "REDEMPTION", "REVERSAL", "ADJUSTMENT"}


class MinePointsError(Exception):
    """Base exception for MinePoints ledger operations."""


class InsufficientPointsError(MinePointsError):
    """A debit would make the customer's points balance negative."""


class IdempotencyConflictError(MinePointsError):
    """An idempotency key was reused for a different ledger request."""


def _validate_entry(points_delta, entry_type, idempotency_key, reason, related_entry_id):
    if isinstance(points_delta, bool) or not isinstance(points_delta, int) or points_delta == 0:
        raise ValueError("points_delta must be a non-zero integer.")
    kind = str(entry_type or "").strip().upper()
    if kind not in _ENTRY_TYPES:
        raise ValueError("Unsupported MinePoints entry type.")
    if kind == "AWARD" and points_delta < 0:
        raise ValueError("AWARD entries must add points.")
    if kind == "REDEMPTION" and points_delta > 0:
        raise ValueError("REDEMPTION entries must deduct points.")
    if kind == "REVERSAL" and related_entry_id is None:
        raise ValueError("REVERSAL entries must reference the entry they reverse.")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128 or not key.isascii():
        raise ValueError("A valid ASCII idempotency key of at most 128 characters is required.")
    why = str(reason or "").strip()
    if not why or len(why) > 240:
        raise ValueError("A reason of 1–240 characters is required.")
    return kind, key, why


def get_points_balance(client_id):
    """Return the ledger-derived points balance for a customer."""
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id <= 0:
        raise ValueError("client_id must be a positive integer.")
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("MinePoints database is unavailable.")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM bank_clients WHERE id=%s", (client_id,))
                if cur.fetchone() is None:
                    raise ValueError("MineBank customer does not exist.")
                cur.execute(
                    "SELECT COALESCE(SUM(points_delta), 0) FROM minepoints_ledger WHERE client_id=%s",
                    (client_id,),
                )
                return int(cur.fetchone()[0])
    finally:
        release_db_connection(conn)


def record_points_entry(
    client_id,
    points_delta,
    entry_type,
    idempotency_key,
    reason,
    *,
    source_type=None,
    source_id=None,
    related_account_id=None,
    related_entry_id=None,
    rule_version=None,
    actor_client_id=None,
):
    """Append one ledger entry atomically and return the resulting points balance.

    The customer row is locked to serialize all balance-affecting requests for that
    customer. Replays with identical payloads return the original entry; reusing a key
    with different data is rejected. This is an internal primitive, not an award policy.
    """
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id <= 0:
        raise ValueError("client_id must be a positive integer.")
    kind, key, why = _validate_entry(points_delta, entry_type, idempotency_key, reason, related_entry_id)
    normalized = {
        "entry_type": kind,
        "points_delta": points_delta,
        "reason": why,
        "source_type": source_type,
        "source_id": str(source_id) if source_id is not None else None,
        "related_account_id": related_account_id,
        "related_entry_id": related_entry_id,
        "rule_version": rule_version,
        "actor_client_id": actor_client_id,
    }
    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("MinePoints database is unavailable.")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM bank_clients WHERE id=%s FOR UPDATE", (client_id,))
                if cur.fetchone() is None:
                    raise ValueError("MineBank customer does not exist.")
                cur.execute(
                    """SELECT id, entry_type, points_delta, reason, source_type, source_id,
                              related_account_id, related_entry_id, rule_version, actor_client_id
                       FROM minepoints_ledger WHERE client_id=%s AND idempotency_key=%s""",
                    (client_id, key),
                )
                existing = cur.fetchone()
                if existing is not None:
                    original = {
                        "entry_type": existing[1],
                        "points_delta": existing[2],
                        "reason": existing[3],
                        "source_type": existing[4],
                        "source_id": existing[5],
                        "related_account_id": existing[6],
                        "related_entry_id": existing[7],
                        "rule_version": existing[8],
                        "actor_client_id": existing[9],
                    }
                    if original != normalized:
                        raise IdempotencyConflictError("Idempotency key already belongs to a different MinePoints entry.")
                    cur.execute(
                        "SELECT COALESCE(SUM(points_delta), 0) FROM minepoints_ledger WHERE client_id=%s",
                        (client_id,),
                    )
                    balance = int(cur.fetchone()[0])
                    return {"entry_id": existing[0], "replayed": True, "points_balance": balance}
                cur.execute(
                    "SELECT COALESCE(SUM(points_delta), 0) FROM minepoints_ledger WHERE client_id=%s",
                    (client_id,),
                )
                balance = int(cur.fetchone()[0])
                if balance + points_delta < 0:
                    raise InsufficientPointsError("Insufficient MinePoints balance.")
                cur.execute(
                    """INSERT INTO minepoints_ledger
                       (client_id, entry_type, points_delta, idempotency_key, reason,
                        source_type, source_id, related_account_id, related_entry_id,
                        rule_version, actor_client_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (
                        client_id, kind, points_delta, key, why, source_type,
                        normalized["source_id"], related_account_id, related_entry_id,
                        rule_version, actor_client_id,
                    ),
                )
                entry_id = cur.fetchone()[0]
                return {
                    "entry_id": entry_id,
                    "replayed": False,
                    "points_balance": balance + points_delta,
                }
    finally:
        release_db_connection(conn)
