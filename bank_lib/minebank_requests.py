import json
from bank_lib.database import execute_query, execute_query_dict


def create_request(client_id, request_type, account_id=None, payload=None):
    payload = payload or {}
    row = execute_query(
        """INSERT INTO bank_requests_v2
           (client_id, account_id, request_type, status, payload)
           VALUES (%s, %s, %s, 'PENDING', %s::jsonb)
           RETURNING id""",
        (client_id, account_id, request_type, json.dumps(payload)),
        commit=True,
    )
    return row[0][0] if row else None


def list_requests(client_id, account_id=None, limit=200):
    if account_id is None:
        return execute_query_dict(
            """SELECT id, account_id, request_type, status, payload,
                      reviewed_by, reviewed_at, created_at
               FROM bank_requests_v2
               WHERE client_id=%s
               ORDER BY created_at DESC
               LIMIT %s""",
            (client_id, limit),
        )
    return execute_query_dict(
        """SELECT id, account_id, request_type, status, payload,
                  reviewed_by, reviewed_at, created_at
           FROM bank_requests_v2
           WHERE client_id=%s AND (account_id=%s OR account_id IS NULL)
           ORDER BY created_at DESC
           LIMIT %s""",
        (client_id, account_id, limit),
    )


def get_request(client_id, request_id):
    rows = execute_query_dict(
        """SELECT id, account_id, request_type, status, payload,
                  reviewed_by, reviewed_at, created_at
           FROM bank_requests_v2
           WHERE client_id=%s AND id=%s""",
        (client_id, request_id),
    )
    return rows[0] if rows else None


def count_pending_requests(client_id):
    rows = execute_query(
        "SELECT COUNT(*) FROM bank_requests_v2 WHERE client_id=%s AND status='PENDING'",
        (client_id,),
    )
    return rows[0][0] if rows else 0


def create_notification(client_id, notification_type, title, message, account_id=None):
    execute_query(
        """INSERT INTO bank_notifications
           (client_id, account_id, notification_type, title, message)
           VALUES (%s, %s, %s, %s, %s)""",
        (client_id, account_id, notification_type, title, message),
        commit=True,
    )
