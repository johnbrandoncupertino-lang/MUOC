"""Secure, two-way MineBank customer support conversations."""

from bank_lib.database import execute_query, execute_query_dict


def create_support_thread(client_id, account_id, subject, body):
    """Create a customer conversation and its opening message atomically."""
    rows = execute_query(
        """WITH new_thread AS (
               INSERT INTO minebank_support_threads(client_id,account_id,subject)
               VALUES(%s,%s,%s) RETURNING id
           ), first_message AS (
               INSERT INTO minebank_support_messages(thread_id,sender_client_id,sender_role,body)
               SELECT id,%s,'CLIENT',%s FROM new_thread RETURNING thread_id
           )
           SELECT thread_id FROM first_message""",
        (client_id, account_id, subject, client_id, body),
        commit=True,
    )
    return int(rows[0][0]) if rows else None


def list_support_threads(client_id=None, status=None):
    """List customer-owned conversations or the shared staff queue."""
    status = status if status in {"OPEN", "CLOSED"} else None
    filters = []
    params = []
    if client_id is not None:
        filters.append("t.client_id=%s")
        params.append(client_id)
    if status:
        filters.append("t.status=%s")
        params.append(status)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    return execute_query_dict(
        """SELECT t.id,t.client_id,t.account_id,t.subject,t.status,t.created_at,t.updated_at,
                  c.email AS client_email,a.account_number,
                  COALESCE(last_message.body,'') AS last_message,
                  last_message.sender_role AS last_sender_role,
                  EXISTS (
                    SELECT 1 FROM minebank_support_messages unread
                    WHERE unread.thread_id=t.id AND unread.sender_role='STAFF'
                      AND (t.client_last_read_at IS NULL OR unread.created_at>t.client_last_read_at)
                  ) AS client_unread,
                  EXISTS (
                    SELECT 1 FROM minebank_support_messages unread
                    WHERE unread.thread_id=t.id AND unread.sender_role='CLIENT'
                      AND (t.staff_last_read_at IS NULL OR unread.created_at>t.staff_last_read_at)
                  ) AS staff_unread
           FROM minebank_support_threads t
           JOIN bank_clients c ON c.id=t.client_id
           LEFT JOIN bank_accounts a ON a.id=t.account_id
           LEFT JOIN LATERAL (
               SELECT body,sender_role FROM minebank_support_messages
               WHERE thread_id=t.id ORDER BY id DESC LIMIT 1
           ) last_message ON TRUE"""
        + where
        + " ORDER BY t.updated_at DESC,t.id DESC LIMIT 200",
        tuple(params),
    )


def get_support_thread(thread_id, client_id=None):
    """Get a thread, restricting customer lookups to their own records."""
    ownership = " AND t.client_id=%s" if client_id is not None else ""
    params = (thread_id, client_id) if client_id is not None else (thread_id,)
    rows = execute_query_dict(
        """SELECT t.id,t.client_id,t.account_id,t.subject,t.status,t.created_at,t.updated_at,
                  c.email AS client_email,a.account_number
           FROM minebank_support_threads t
           JOIN bank_clients c ON c.id=t.client_id
           LEFT JOIN bank_accounts a ON a.id=t.account_id
           WHERE t.id=%s""" + ownership,
        params,
    )
    return rows[0] if rows else None


def list_support_messages(thread_id):
    return execute_query_dict(
        """SELECT m.id,m.sender_client_id,m.sender_role,m.body,m.created_at,c.email AS sender_email
           FROM minebank_support_messages m
           LEFT JOIN bank_clients c ON c.id=m.sender_client_id
           WHERE m.thread_id=%s ORDER BY m.id""",
        (thread_id,),
    )


def mark_support_thread_read(thread_id, role, client_id=None):
    """Mark the other party's messages as read for this conversation."""
    if role == "STAFF":
        query = "UPDATE minebank_support_threads SET staff_last_read_at=CURRENT_TIMESTAMP WHERE id=%s"
        params = (thread_id,)
    elif role == "CLIENT" and client_id is not None:
        query = "UPDATE minebank_support_threads SET client_last_read_at=CURRENT_TIMESTAMP WHERE id=%s AND client_id=%s"
        params = (thread_id, client_id)
    else:
        return
    execute_query(query, params, fetch=False, commit=True)


def send_support_reply(thread_id, sender_id, sender_role, client_id, body):
    """Add a reply only when the thread is owned by the customer or staff."""
    if sender_role not in {"CLIENT", "STAFF"}:
        return False
    rows = execute_query(
        """WITH authorized_thread AS (
               UPDATE minebank_support_threads
               SET status='OPEN',updated_at=CURRENT_TIMESTAMP,
                   client_last_read_at=CASE WHEN %s='CLIENT' THEN CURRENT_TIMESTAMP ELSE client_last_read_at END,
                   staff_last_read_at=CASE WHEN %s='STAFF' THEN CURRENT_TIMESTAMP ELSE staff_last_read_at END
               WHERE id=%s AND (%s='STAFF' OR client_id=%s)
               RETURNING id
           )
           INSERT INTO minebank_support_messages(thread_id,sender_client_id,sender_role,body)
           SELECT id,%s,%s,%s FROM authorized_thread
           RETURNING id""",
        (sender_role, sender_role, thread_id, sender_role, client_id,
         sender_id, sender_role, body),
        commit=True,
    )
    return bool(rows)


def close_support_thread(thread_id, client_id=None):
    ownership = " AND client_id=%s" if client_id is not None else ""
    params = (thread_id, client_id) if client_id is not None else (thread_id,)
    rows = execute_query(
        """UPDATE minebank_support_threads SET status='CLOSED',updated_at=CURRENT_TIMESTAMP
           WHERE id=%s""" + ownership + " RETURNING id",
        params,
        commit=True,
    )
    return bool(rows)
