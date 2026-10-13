"""Feature-flagged, read-only-first Minecraft card API for MineBank.

The database migration lives in extras/minebank_minecraft.sql and is applied
manually after review. This module never runs DDL during a request.
"""
import hashlib
import hmac
import os
import secrets

from flask import Blueprint, current_app, jsonify, request, session

from .database import get_db_connection, release_db_connection
from .minebank_auth import login_client, logout_client, verify_wallet_pin

minecraft_bp = Blueprint("minebank_minecraft", __name__, url_prefix="/api/v1/minecraft")
MAX_ACTIVE_CARDS_PER_CLIENT = 5
MAX_CARD_ISSUANCES_PER_24H = 10


def minecraft_enabled():
    """Fail closed unless the operator explicitly opts in."""
    return os.environ.get("MINEBANK_MINECRAFT_ENABLED", "").strip().lower() == "true"


def hash_card_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorized_card_token():
    header = request.headers.get("Authorization", "")
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.startswith("mbmc_"):
        return None
    if len(token) < 40 or len(token) > 160:
        return None
    return token


def _request_is_secure():
    # Vercel terminates TLS before forwarding requests to Flask. Accept its
    # forwarded-proto marker as well as Flask's native secure flag.
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return request.is_secure or forwarded == "https"


def _transport_unavailable():
    if not _request_is_secure():
        return jsonify(error="Minecraft integration requires HTTPS."), 400
    return None


def _feature_unavailable():
    if not minecraft_enabled():
        return jsonify(error="Minecraft integration is disabled."), 404
    return None


def _authenticate_player(data):
    """Reuse the portal's login controls, then remove the temporary web session."""
    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))
    captcha_answer = data.get("captcha_answer")
    if not email or not password:
        return None, (jsonify(error="Invalid credentials."), 401)
    ok, message = login_client(email, password, captcha_answer)
    if not ok:
        # Preserve the existing login-protection message (including challenge
        # and temporary-block information); never log the supplied credentials.
        return None, (jsonify(error=message or "Invalid credentials."), 401)
    client_id = session.get("minebank_client_id")
    # Minecraft cards are customer instruments, not operator/admin credentials.
    # Fail closed if the existing login helper does not establish a normal client
    # identity in the session.
    if not client_id or session.get("minebank_role") != "CLIENT":
        try:
            logout_client()
        except Exception:
            session.clear()
        return None, (jsonify(error="Only an active MineBank customer can manage Minecraft cards."), 403)
    return client_id, None


def _clear_temporary_login_session():
    # login_client creates a regular persistent portal session. This API uses
    # the authenticated identity only for this request, so revoke that session
    # rather than returning a browser session cookie to a terminal.
    try:
        logout_client()
    except Exception as exc:
        current_app.logger.warning(
            "Minecraft temporary session cleanup failed: %s", type(exc).__name__
        )
        session.clear()


@minecraft_bp.post("/accounts")
def list_customer_accounts():
    """Authenticate a customer and list only their active accounts for card selection."""
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="A JSON request body is required."), 400
    client_id, failure = _authenticate_player(data)
    if failure:
        return failure

    try:
        pin = str(data.get("wallet_pin", ""))
        if len(pin) != 6 or not pin.isdigit():
            return jsonify(error="Wallet PIN must contain exactly six digits."), 400
        pin_ok, pin_message = verify_wallet_pin(client_id, pin)
        if not pin_ok:
            return jsonify(error=pin_message or "Wallet PIN verification failed."), 401

        conn = get_db_connection()
        if conn is None:
            return jsonify(error="MineBank is temporarily unavailable."), 503
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT a.id,a.account_number,a.account_type,t.display_name,
                                  a.balance,a.status
                           FROM bank_accounts a
                           JOIN account_tiers_v2 t ON t.id=a.tier_id
                           JOIN bank_clients bc ON bc.id=a.client_id
                           WHERE a.client_id=%s
                             AND a.status='ACTIVE'
                             AND bc.status='ACTIVE'
                           ORDER BY a.created_at,a.id""",
                        (client_id,),
                    )
                    accounts = [
                        {
                            "id": row[0],
                            "account_number": row[1],
                            "account_type": row[2],
                            "tier": row[3],
                            "balance": int(row[4]),
                            "status": row[5],
                            "currency": "emerald",
                        }
                        for row in cur.fetchall()
                    ]
            return jsonify(accounts=accounts)
        finally:
            release_db_connection(conn)
    except Exception as exc:
        current_app.logger.error("Minecraft account listing failed: %s", type(exc).__name__)
        return jsonify(error="MineBank account data is temporarily unavailable."), 503
    finally:
        _clear_temporary_login_session()


@minecraft_bp.post("/cards")
def issue_card():
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="A JSON request body is required."), 400
    client_id, failure = _authenticate_player(data)
    if failure:
        return failure

    try:
        pin = str(data.get("wallet_pin", ""))
        if len(pin) != 6 or not pin.isdigit():
            return jsonify(error="Wallet PIN must contain exactly six digits."), 400
        pin_ok, pin_message = verify_wallet_pin(client_id, pin)
        if not pin_ok:
            return jsonify(error=pin_message or "Wallet PIN verification failed."), 401

        try:
            account_id = int(data.get("account_id"))
        except (TypeError, ValueError):
            return jsonify(error="A valid account_id is required."), 400

        label = str(data.get("label") or "Minecraft card").strip()[:80] or "Minecraft card"
        token = "mbmc_" + secrets.token_urlsafe(32)
        prefix = token[:16]
        digest = hash_card_token(token)

        conn = get_db_connection()
        if conn is None:
            return jsonify(error="MineBank is temporarily unavailable."), 503
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize issuance per client so simultaneous requests
                    # cannot exceed the active-card cap.
                    cur.execute("SELECT id,status FROM bank_clients WHERE id=%s FOR UPDATE", (client_id,))
                    client = cur.fetchone()
                    if not client or client[1] != "ACTIVE":
                        return jsonify(error="This MineBank profile cannot issue cards."), 403

                    cur.execute(
                        """SELECT id,account_number,account_type,status
                           FROM bank_accounts
                           WHERE id=%s AND client_id=%s AND status='ACTIVE'
                           FOR UPDATE""",
                        (account_id, client_id),
                    )
                    account = cur.fetchone()
                    if not account:
                        return jsonify(error="Account not found or not eligible for card issuance."), 404

                    cur.execute(
                        """SELECT COUNT(*) FROM minebank_minecraft_cards
                           WHERE client_id=%s AND status='ACTIVE'""",
                        (client_id,),
                    )
                    active_count = int(cur.fetchone()[0])
                    if active_count >= MAX_ACTIVE_CARDS_PER_CLIENT:
                        return jsonify(error="The maximum of five active Minecraft cards has been reached."), 409

                    # Include revoked cards in the issuance count so repeatedly
                    # revoking and reissuing cannot bypass this daily throttle.
                    cur.execute(
                        """SELECT COUNT(*) FROM minebank_minecraft_cards
                           WHERE client_id=%s AND issued_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'""",
                        (client_id,),
                    )
                    recent_issuances = int(cur.fetchone()[0])
                    if recent_issuances >= MAX_CARD_ISSUANCES_PER_24H:
                        return jsonify(error="Card issuance limit reached. Try again later."), 429

                    cur.execute(
                        """INSERT INTO minebank_minecraft_cards
                               (client_id,account_id,card_prefix,card_token_hash,label)
                           VALUES(%s,%s,%s,%s,%s)
                           RETURNING id,issued_at""",
                        (client_id, account_id, prefix, digest, label),
                    )
                    card_id, issued_at = cur.fetchone()
                    cur.execute(
                        """INSERT INTO audit_events
                               (actor_client_id,action,target_type,target_id,account_id,ip_address,context)
                           VALUES(%s,'MINECRAFT_CARD_ISSUED','MINECRAFT_CARD',%s,%s,%s::inet,
                                  jsonb_build_object('source','minecraft'))""",
                        (client_id, str(card_id), account_id, request.remote_addr),
                    )
            return jsonify(
                card={
                    "id": card_id,
                    "label": label,
                    "account_number": account[1],
                    "issued_at": issued_at.isoformat(),
                },
                credential=token,
                warning="This credential is shown once. Store it only on the card floppy; never store the account password or Wallet PIN.",
            ), 201
        finally:
            release_db_connection(conn)
    except Exception as exc:
        # A missing migration, database outage, or unexpected database error
        # must not expose SQL details or turn into a false success response.
        current_app.logger.error("Minecraft card issuance failed: %s", type(exc).__name__)
        return jsonify(error="Minecraft card issuance is temporarily unavailable."), 503
    finally:
        _clear_temporary_login_session()


@minecraft_bp.get("/account")
def read_card_account():
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure
    token = _authorized_card_token()
    if not token:
        return jsonify(error="A valid card credential is required."), 401

    conn = get_db_connection()
    if conn is None:
        return jsonify(error="MineBank is temporarily unavailable."), 503
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.id,c.card_token_hash,c.client_id,c.account_id,
                              a.account_number,a.account_type,a.balance,a.status,
                              t.display_name,bc.status
                       FROM minebank_minecraft_cards c
                       JOIN bank_accounts a ON a.id=c.account_id AND a.client_id=c.client_id
                       JOIN account_tiers_v2 t ON t.id=a.tier_id
                       JOIN bank_clients bc ON bc.id=c.client_id
                       WHERE c.card_prefix=%s AND c.status='ACTIVE'
                         AND c.revoked_at IS NULL AND bc.role='CLIENT'
                       LIMIT 5""",
                    (token[:16],),
                )
                rows = cur.fetchall()
                token_hash = hash_card_token(token)
                row = next(
                    (candidate for candidate in rows
                     if hmac.compare_digest(str(candidate[1]), token_hash)),
                    None,
                )
                if not row:
                    return jsonify(error="Card credential is invalid or revoked."), 401
                if row[7] != "ACTIVE" or row[9] != "ACTIVE":
                    return jsonify(error="This card or its MineBank profile is not active."), 403

                cur.execute(
                    "UPDATE minebank_minecraft_cards SET last_used_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (row[0],),
                )
                cur.execute(
                    """INSERT INTO audit_events
                           (actor_client_id,action,target_type,target_id,account_id,ip_address,context)
                       VALUES(%s,'MINECRAFT_CARD_READ','MINECRAFT_CARD',%s,%s,%s::inet,
                              jsonb_build_object('source','minecraft'))""",
                    (row[2], str(row[0]), row[3], request.remote_addr),
                )
            return jsonify(
                account={
                    "account_number": row[4],
                    "account_type": row[5],
                    "tier": row[8],
                    "balance": int(row[6]),
                    "status": row[7],
                    "currency": "emerald",
                }
            )
    except Exception as exc:
        current_app.logger.error("Minecraft card account read failed: %s", type(exc).__name__)
        return jsonify(error="MineBank account data is temporarily unavailable."), 503
    finally:
        release_db_connection(conn)


@minecraft_bp.post("/cards/revoke")
def revoke_card():
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="A JSON request body is required."), 400
    client_id, failure = _authenticate_player(data)
    if failure:
        return failure

    try:
        pin = str(data.get("wallet_pin", ""))
        if not pin:
            return jsonify(error="Wallet PIN is required."), 400
        pin_ok, pin_message = verify_wallet_pin(client_id, pin)
        if not pin_ok:
            return jsonify(error=pin_message or "Wallet PIN verification failed."), 401
        try:
            card_id = int(data.get("card_id"))
        except (TypeError, ValueError):
            return jsonify(error="A valid card_id is required."), 400

        conn = get_db_connection()
        if conn is None:
            return jsonify(error="MineBank is temporarily unavailable."), 503
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE minebank_minecraft_cards
                           SET status='REVOKED',revoked_at=CURRENT_TIMESTAMP
                           WHERE id=%s AND client_id=%s AND status='ACTIVE'
                           RETURNING id""",
                        (card_id, client_id),
                    )
                    revoked = cur.fetchone()
                    if revoked:
                        cur.execute(
                            """INSERT INTO audit_events
                                   (actor_client_id,action,target_type,target_id,ip_address,context)
                               VALUES(%s,'MINECRAFT_CARD_REVOKED','MINECRAFT_CARD',%s,%s::inet,
                                      jsonb_build_object('source','minecraft'))""",
                            (client_id, str(card_id), request.remote_addr),
                        )
            if not revoked:
                return jsonify(error="Active card not found for this MineBank customer."), 404
            return jsonify(card_id=card_id, status="REVOKED")
        finally:
            release_db_connection(conn)
    except Exception as exc:
        current_app.logger.error("Minecraft card revocation failed: %s", type(exc).__name__)
        return jsonify(error="Minecraft card revocation is temporarily unavailable."), 503
    finally:
        _clear_temporary_login_session()


# Money movement is deliberately separate from the read-only card endpoints.
# Amounts for purchases are read from a MineBank-issued checkout, never from the
# card holder's payment request. ATM withdrawals are whole Emerald item units.
MAX_MINECRAFT_WITHDRAWAL = 4096
MAX_MINECRAFT_CHECKOUT = 1000000


def _request_idempotency_key():
    key = str(request.headers.get("Idempotency-Key", "")).strip()
    if not key or len(key) > 128 or not key.isascii() or not all(
        ch.isalnum() or ch in "-_." for ch in key
    ):
        return None
    return key


def _merchant_token():
    header = request.headers.get("Authorization", "")
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    if not token.startswith("mbmerchant_") or len(token) < 40 or len(token) > 180:
        return None
    return token


def _card_row_for_update(cur, token):
    cur.execute(
        """SELECT c.id,c.client_id,c.account_id,c.card_token_hash,c.status,
                  a.account_number,a.account_type,a.tier_id,a.balance,a.status,
                  a.monthly_outgoing_used,a.monthly_outgoing_period,
                  a.last_outgoing_at,bc.status
           FROM minebank_minecraft_cards c
           JOIN bank_accounts a ON a.id=c.account_id AND a.client_id=c.client_id
           JOIN bank_clients bc ON bc.id=c.client_id
           WHERE c.card_prefix=%s AND c.status='ACTIVE' AND c.revoked_at IS NULL
             AND bc.role='CLIENT'
           ORDER BY c.id LIMIT 10 FOR UPDATE OF c,a,bc""",
        (token[:16],),
    )
    token_hash = hash_card_token(token)
    for row in cur.fetchall():
        if hmac.compare_digest(str(row[3]), token_hash):
            if row[4] != "ACTIVE" or row[9] != "ACTIVE" or row[13] != "ACTIVE":
                raise ValueError("The card, account, or MineBank profile is not active.")
            return row
    raise PermissionError("Card credential is invalid or revoked.")


def _validate_outgoing_limits(cur, account, amount):
    """Validate configured tier limits while the account row is locked."""
    cur.execute(
        """SELECT monthly_outgoing_limit,daily_outgoing_limit,single_transfer_limit,
                  max_balance
           FROM account_tiers_v2 WHERE id=%s AND active=TRUE""",
        (account[7],),
    )
    limits = cur.fetchone()
    if not limits:
        raise ValueError("The account tier is unavailable.")
    monthly_limit, daily_limit, single_limit, _ = limits
    period = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m")
    if account[11] != period:
        cur.execute(
            "UPDATE bank_accounts SET monthly_outgoing_used=0,monthly_outgoing_period=%s WHERE id=%s",
            (period, account[0]),
        )
        monthly_used = 0
    else:
        monthly_used = int(account[10] or 0)
    if single_limit is not None and amount > int(single_limit):
        raise ValueError("The amount exceeds the account's single-payment limit.")
    if monthly_limit is not None and monthly_used + amount > int(monthly_limit):
        raise ValueError("The amount exceeds the remaining monthly outgoing limit.")
    cur.execute(
        """SELECT COALESCE(SUM(amount+fee),0) FROM ledger_transactions
           WHERE sender_account_id=%s AND status IN
             ('COMPLETED','PENDING_APPROVAL','PENDING_BUSINESS_APPROVAL')
             AND created_at>=CURRENT_TIMESTAMP-INTERVAL '1 day'""",
        (account[0],),
    )
    daily_used = int(cur.fetchone()[0] or 0)
    if daily_limit is not None and daily_used + amount > int(daily_limit):
        raise ValueError("The amount exceeds the remaining daily outgoing limit.")
    return monthly_used, period, limits


def _save_operation(cur, *, client_id, card_id, account_id, key, op_type,
                    request_hash, status, amount, transaction_id=None, response=None):
    cur.execute(
        """INSERT INTO minebank_minecraft_operations
           (client_id,card_id,account_id,idempotency_key,operation_type,request_hash,
            status,amount,transaction_id,response,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,CURRENT_TIMESTAMP)
           RETURNING id""",
        (client_id, card_id, account_id, key, op_type, request_hash, status,
         amount, transaction_id, __import__("json").dumps(response or {})),
    )
    return cur.fetchone()[0]


def _replay_operation(cur, client_id, key, op_type, request_hash):
    # Advisory lock serializes first-time requests too (SELECT FOR UPDATE cannot
    # lock a row that has not been inserted yet).
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"minebank-minecraft:{client_id}:{key}",))
    cur.execute(
        """SELECT operation_type,request_hash,status,response,transaction_id,amount
           FROM minebank_minecraft_operations
           WHERE client_id=%s AND idempotency_key=%s FOR UPDATE""",
        (client_id, key),
    )
    old = cur.fetchone()
    if not old:
        return None
    if old[0] != op_type or not hmac.compare_digest(str(old[1]), request_hash):
        raise ValueError("This Idempotency-Key was already used for a different request.")
    response = old[3] if isinstance(old[3], dict) else {}
    response = dict(response or {})
    response["idempotent_replay"] = True
    return jsonify(response), 200


@minecraft_bp.post("/merchant/checkouts")
def create_merchant_checkout():
    """Create a short-lived, merchant-authorized checkout with a fixed server amount."""
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure
    token = _merchant_token()
    data = request.get_json(silent=True)
    key = _request_idempotency_key()
    if not token or not isinstance(data, dict) or not key:
        return jsonify(error="Merchant authorization, JSON body, and Idempotency-Key are required."), 400
    raw_amount = data.get("amount")
    if isinstance(raw_amount, bool) or not (
        isinstance(raw_amount, int)
        or (isinstance(raw_amount, str) and raw_amount.isascii() and raw_amount.isdigit())
    ):
        return jsonify(error="Amount must be a whole Emerald integer."), 400
    amount = int(raw_amount)
    if amount < 1 or amount > MAX_MINECRAFT_CHECKOUT:
        return jsonify(error="Checkout amount is outside the permitted range."), 400
    digest = hash_card_token(token)
    description = str(data.get("description") or "Minecraft shop purchase").strip()[:200]
    reference = str(data.get("reference") or "").strip()[:100] or None
    request_hash = hashlib.sha256(
        (str(amount) + ":" + description + ":" + (reference or "")).encode("utf-8")
    ).hexdigest()
    conn = get_db_connection()
    if conn is None:
        return jsonify(error="MineBank is temporarily unavailable."), 503
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT m.id,m.client_id,m.account_id,a.status,bc.status,
                              t.max_balance,a.balance,m.display_name
                       FROM minebank_minecraft_merchants m
                       JOIN bank_accounts a ON a.id=m.account_id AND a.client_id=m.client_id
                       JOIN bank_clients bc ON bc.id=m.client_id
                       JOIN account_tiers_v2 t ON t.id=a.tier_id
                       WHERE m.token_hash=%s AND m.active=TRUE AND bc.role='CLIENT'
                       FOR UPDATE OF m,a,bc""",
                    (digest,),
                )
                merchant = cur.fetchone()
                if not merchant:
                    return jsonify(error="Merchant credential is invalid or revoked."), 401
                if merchant[3] != "ACTIVE" or merchant[4] != "ACTIVE":
                    return jsonify(error="Merchant account is not active."), 403
                cur.execute(
                    """SELECT id,request_hash,amount,description,merchant_reference,status,expires_at
                       FROM minebank_minecraft_checkouts
                       WHERE merchant_id=%s AND merchant_idempotency_key=%s FOR UPDATE""",
                    (merchant[0], key),
                )
                old = cur.fetchone()
                if old:
                    if not hmac.compare_digest(str(old[1]), request_hash):
                        return jsonify(error="This Idempotency-Key was used for a different checkout."), 409
                    return jsonify(
                        checkout_id=str(old[0]), amount=int(old[2]), currency="emerald",
                        description=old[3], status=old[5], expires_at=old[6].isoformat(),
                        payment_endpoint="/api/v1/minecraft/payments", idempotent_replay=True,
                    ), 200
                cur.execute(
                    """INSERT INTO minebank_minecraft_checkouts
                       (merchant_id,merchant_idempotency_key,request_hash,amount,description,
                        merchant_reference,expires_at)
                       VALUES(%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP+INTERVAL '5 minutes')
                       RETURNING id,expires_at""",
                    (merchant[0], key, request_hash, amount, description, reference),
                )
                checkout_id, expires_at = cur.fetchone()
        return jsonify(
            checkout_id=str(checkout_id), amount=amount, currency="emerald",
            merchant=merchant[7], description=description, expires_at=expires_at.isoformat(),
            payment_endpoint="/api/v1/minecraft/payments",
        ), 201
    except Exception as exc:
        current_app.logger.error("Minecraft checkout creation failed: %s", type(exc).__name__)
        return jsonify(error="Checkout creation is temporarily unavailable."), 503
    finally:
        release_db_connection(conn)


@minecraft_bp.post("/payments")
def pay_merchant_checkout():
    """Pay the fixed amount in an open checkout, atomically and idempotently."""
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure
    token = _authorized_card_token()
    data = request.get_json(silent=True)
    key = _request_idempotency_key()
    if not token or not isinstance(data, dict) or not key:
        return jsonify(error="Card credential, JSON body, and Idempotency-Key are required."), 400
    checkout_id = str(data.get("checkout_id", "")).strip()
    if not checkout_id.isascii() or not checkout_id.isdigit() or len(checkout_id) > 20:
        return jsonify(error="A valid checkout_id is required."), 400
    request_hash = hashlib.sha256(("PAYMENT:" + checkout_id).encode("utf-8")).hexdigest()
    conn = get_db_connection()
    if conn is None:
        return jsonify(error="MineBank is temporarily unavailable."), 503
    try:
        with conn:
            with conn.cursor() as cur:
                # Resolve card identity before taking its operation idempotency lock.
                cur.execute(
                    """SELECT c.client_id FROM minebank_minecraft_cards c
                       WHERE c.card_prefix=%s AND c.status='ACTIVE' AND c.revoked_at IS NULL
                       ORDER BY c.id LIMIT 10""",
                    (token[:16],),
                )
                candidates = cur.fetchall()
                client_id = None
                for candidate in candidates:
                    cur.execute("SELECT card_token_hash FROM minebank_minecraft_cards WHERE card_prefix=%s AND client_id=%s",
                                (token[:16], candidate[0]))
                    hashed = cur.fetchone()
                    if hashed and hmac.compare_digest(str(hashed[0]), hash_card_token(token)):
                        client_id = candidate[0]
                        break
                if client_id is None:
                    raise PermissionError("Card credential is invalid or revoked.")
                replay = _replay_operation(cur, client_id, key, "PAYMENT", request_hash)
                if replay:
                    return replay
                card = _card_row_for_update(cur, token)
                cur.execute(
                    """SELECT x.id,x.merchant_id,x.amount,x.description,x.status,x.expires_at,
                              m.client_id,m.account_id,m.active,a.status,bc.status,
                              t.max_balance,a.balance
                       FROM minebank_minecraft_checkouts x
                       JOIN minebank_minecraft_merchants m ON m.id=x.merchant_id
                       JOIN bank_accounts a ON a.id=m.account_id AND a.client_id=m.client_id
                       JOIN bank_clients bc ON bc.id=m.client_id
                       JOIN account_tiers_v2 t ON t.id=a.tier_id
                       WHERE x.id=%s FOR UPDATE OF x,m,a,bc""",
                    (int(checkout_id),),
                )
                checkout = cur.fetchone()
                if not checkout:
                    raise ValueError("Checkout not found.")
                if checkout[4] != "OPEN" or checkout[5] <= __import__("datetime").datetime.now(checkout[5].tzinfo):
                    raise ValueError("Checkout has expired or has already been paid.")
                if not checkout[8] or checkout[9] != "ACTIVE" or checkout[10] != "ACTIVE":
                    raise ValueError("Merchant account is not active.")
                if int(checkout[7]) == int(card[2]):
                    raise ValueError("A MineBank account cannot pay itself.")
                amount = int(checkout[2])
                if amount < 1 or amount > MAX_MINECRAFT_CHECKOUT:
                    raise ValueError("Checkout amount is outside the permitted range.")
                if int(card[8]) < amount:
                    raise ValueError("Insufficient MineBank Balance.")
                cur.execute("SELECT COALESCE(system_suspended,FALSE) FROM settings ORDER BY id LIMIT 1")
                system_row = cur.fetchone()
                if system_row and system_row[0]:
                    raise ValueError("MineBank banking operations are temporarily suspended.")
                cur.execute("SELECT 1 FROM minebank_banking_locks WHERE client_id=%s AND unlocked_at IS NULL",
                            (card[1],))
                if cur.fetchone():
                    raise ValueError("Banking operations are temporarily locked for this client.")
                monthly_used, period, limits = _validate_outgoing_limits(cur, card, amount)
                max_balance = checkout[11]
                if max_balance is not None and int(checkout[12]) + amount > int(max_balance):
                    raise ValueError("Merchant account balance limit would be exceeded.")
                txid = __import__("bank_lib.minebank_core", fromlist=["next_transaction_id"]).next_transaction_id(cur)
                cur.execute(
                    """UPDATE bank_accounts SET balance=balance-%s,monthly_outgoing_used=%s,
                       monthly_outgoing_period=%s,last_outgoing_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s""",
                    (amount, monthly_used + amount, period, card[2]),
                )
                cur.execute("UPDATE bank_accounts SET balance=balance+%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                            (amount, checkout[7]))
                from bank_lib.minebank_core import create_ledger_transaction, audit_event
                create_ledger_transaction(
                    cur, transaction_id=txid, transaction_type="MINECRAFT_PAYMENT",
                    amount=amount, currency="Emerald", sender_account_id=card[2],
                    recipient_account_id=checkout[7], status="COMPLETED",
                    description=("Minecraft checkout: " + str(checkout[3]))[:500],
                    reference_id=str(checkout[0]), transfer_kind="MINECRAFT_PAYMENT",
                )
                cur.execute("UPDATE minebank_minecraft_checkouts SET status='PAID',transaction_id=%s,paid_at=CURRENT_TIMESTAMP WHERE id=%s AND status='OPEN'",
                            (txid, checkout[0]))
                if cur.rowcount != 1:
                    raise ValueError("Checkout was already settled.")
                response_body = {
                    "status": "COMPLETED", "transaction_id": txid,
                    "checkout_id": str(checkout[0]), "amount": amount,
                    "currency": "emerald", "merchant": checkout[3],
                }
                _save_operation(cur, client_id=card[1], card_id=card[0], account_id=card[2],
                                key=key, op_type="PAYMENT", request_hash=request_hash,
                                status="COMPLETED", amount=amount, transaction_id=txid,
                                response=response_body)
                audit_event(cur, actor_client_id=card[1], action="MINECRAFT_PAYMENT",
                            target_type="TRANSACTION", target_id=txid, account_id=card[2],
                            transaction_id=txid, ip_address=request.remote_addr,
                            context={"checkout_id": str(checkout[0]), "merchant_account_id": checkout[7]})
        return jsonify(response_body), 200
    except PermissionError as exc:
        return jsonify(error=str(exc)), 401
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    except Exception as exc:
        current_app.logger.error("Minecraft payment failed: %s", type(exc).__name__)
        return jsonify(error="Payment could not be completed. Check transaction history before retrying."), 503
    finally:
        release_db_connection(conn)


@minecraft_bp.post("/withdrawals")
def withdraw_emerald_items():
    """Atomically debit MineBank balance and issue a single-use ATM dispense ticket."""
    unavailable = _feature_unavailable()
    if unavailable:
        return unavailable
    insecure = _transport_unavailable()
    if insecure:
        return insecure
    token = _authorized_card_token()
    data = request.get_json(silent=True)
    key = _request_idempotency_key()
    if not token or not isinstance(data, dict) or not key:
        return jsonify(error="Card credential, JSON body, and Idempotency-Key are required."), 400
    raw_amount = data.get("amount")
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, int) or raw_amount < 1 or raw_amount > MAX_MINECRAFT_WITHDRAWAL:
        return jsonify(error=f"Withdrawal must be a whole Emerald amount from 1 to {MAX_MINECRAFT_WITHDRAWAL}."), 400
    amount = raw_amount
    request_hash = hashlib.sha256(("WITHDRAWAL:" + str(amount)).encode("utf-8")).hexdigest()
    conn = get_db_connection()
    if conn is None:
        return jsonify(error="MineBank is temporarily unavailable."), 503
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT c.client_id FROM minebank_minecraft_cards c
                       WHERE c.card_prefix=%s AND c.status='ACTIVE' AND c.revoked_at IS NULL
                       ORDER BY c.id LIMIT 10""", (token[:16],)
                )
                client_id = None
                for candidate in cur.fetchall():
                    cur.execute("SELECT card_token_hash FROM minebank_minecraft_cards WHERE card_prefix=%s AND client_id=%s",
                                (token[:16], candidate[0]))
                    hashed = cur.fetchone()
                    if hashed and hmac.compare_digest(str(hashed[0]), hash_card_token(token)):
                        client_id = candidate[0]
                        break
                if client_id is None:
                    raise PermissionError("Card credential is invalid or revoked.")
                replay = _replay_operation(cur, client_id, key, "WITHDRAWAL", request_hash)
                if replay:
                    return replay
                card = _card_row_for_update(cur, token)
                cur.execute("SELECT COALESCE(system_suspended,FALSE) FROM settings ORDER BY id LIMIT 1")
                system_row = cur.fetchone()
                if system_row and system_row[0]:
                    raise ValueError("MineBank banking operations are temporarily suspended.")
                cur.execute("SELECT 1 FROM minebank_banking_locks WHERE client_id=%s AND unlocked_at IS NULL",
                            (card[1],))
                if cur.fetchone():
                    raise ValueError("Banking operations are temporarily locked for this client.")
                monthly_used, period, _ = _validate_outgoing_limits(cur, card, amount)
                if int(card[8]) < amount:
                    raise ValueError("Insufficient MineBank Balance. CashLine cannot be used at an ATM.")
                txid = __import__("bank_lib.minebank_core", fromlist=["next_transaction_id"]).next_transaction_id(cur)
                cur.execute(
                    """UPDATE bank_accounts SET balance=balance-%s,monthly_outgoing_used=%s,
                       monthly_outgoing_period=%s,last_outgoing_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s""",
                    (amount, monthly_used + amount, period, card[2]),
                )
                from bank_lib.minebank_core import create_ledger_transaction, audit_event
                create_ledger_transaction(
                    cur, transaction_id=txid, transaction_type="WITHDRAWAL",
                    amount=amount, currency="Emerald", sender_account_id=card[2],
                    status="COMPLETED", description="MineBank Minecraft ATM withdrawal",
                    reference_id=key, transfer_kind="MINECRAFT_ATM",
                )
                response_body = {
                    "status": "COMPLETED", "transaction_id": txid,
                    "withdrawal_id": key, "amount": amount,
                    "item": "minecraft:emerald", "items": amount,
                    "currency": "emerald",
                    "warning": "This withdrawal has been debited. Dispense the listed items once only.",
                }
                _save_operation(cur, client_id=card[1], card_id=card[0], account_id=card[2],
                                key=key, op_type="WITHDRAWAL", request_hash=request_hash,
                                status="COMPLETED", amount=amount, transaction_id=txid,
                                response=response_body)
                audit_event(cur, actor_client_id=card[1], action="MINECRAFT_ATM_WITHDRAWAL",
                            target_type="TRANSACTION", target_id=txid, account_id=card[2],
                            transaction_id=txid, ip_address=request.remote_addr,
                            context={"withdrawal_id": key, "items": amount})
        return jsonify(response_body), 200
    except PermissionError as exc:
        return jsonify(error=str(exc)), 401
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    except Exception as exc:
        current_app.logger.error("Minecraft withdrawal failed: %s", type(exc).__name__)
        return jsonify(error="Withdrawal could not be completed. Check transaction history before retrying."), 503
    finally:
        release_db_connection(conn)
