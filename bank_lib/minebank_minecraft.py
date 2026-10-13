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


@minecraft_bp.get("/diagnostics")
def minecraft_diagnostics():
    """Temporary, non-sensitive diagnostic for deployment/proxy troubleshooting."""
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    payload = {
        "diagnostics": "minebank-minecraft-v1",
        "integration_enabled": minecraft_enabled(),
        "request_is_secure": bool(request.is_secure),
        "forwarded_proto": forwarded_proto[:80],
        "content_type": request.content_type,
        "https_check_passes": bool(
            request.is_secure
            or forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        ),
    }
    current_app.logger.warning(
        "Minecraft diagnostic endpoint checked: enabled=%s secure=%s forwarded_proto=%r.",
        payload["integration_enabled"],
        payload["request_is_secure"],
        payload["forwarded_proto"],
    )
    return jsonify(payload), 200


def _authenticate_player(data):
    """Reuse the portal's login controls, then remove the temporary web session."""
    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))
    captcha_answer = data.get("captcha_answer")
    if not email or not password:
        return None, (jsonify(error="Invalid credentials."), 401)
    ok, message = login_client(email, password, captcha_answer)
    if not ok:
        # A database outage is not an authentication failure. Keep the existing
        # login-protection messages, but return a retryable server status when
        # the canonical login helper cannot obtain a database connection.
        if message and "temporarily unavailable" in message.lower():
            current_app.logger.error(
                "Minecraft customer authentication unavailable: login helper reported a service outage."
            )
            return None, (jsonify(error=message), 503)
        # Preserve login-protection messages (including challenge and temporary
        # block information); never log the supplied credentials.
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
        current_app.logger.warning(
            "Minecraft accounts request rejected: HTTPS check failed "
            "(request.is_secure=%s, forwarded_proto=%r).",
            request.is_secure,
            request.headers.get("X-Forwarded-Proto", ""),
        )
        return insecure

    request_id = request.headers.get("x-vercel-id", "")[:100] or "unavailable"
    current_app.logger.info("Minecraft accounts stage=request_received vercel_id=%s", request_id)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        current_app.logger.warning(
            "Minecraft accounts request rejected: invalid/missing JSON "
            "(content_type=%r, content_length=%r).",
            request.content_type,
            request.content_length,
        )
        return jsonify(error="A JSON request body is required."), 400
    current_app.logger.info("Minecraft accounts stage=customer_authentication_started vercel_id=%s", request_id)
    client_id, failure = _authenticate_player(data)
    if failure:
        current_app.logger.warning(
            "Minecraft accounts stage=customer_authentication_rejected vercel_id=%s", request_id
        )
        return failure
    current_app.logger.info("Minecraft accounts stage=customer_authenticated vercel_id=%s", request_id)

    try:
        pin = str(data.get("wallet_pin", ""))
        if len(pin) != 6 or not pin.isdigit():
            current_app.logger.warning(
                "Minecraft accounts request rejected: Wallet PIN format invalid "
                "(length=%d, digits_only=%s).",
                len(pin),
                pin.isdigit(),
            )
            return jsonify(error="Wallet PIN must contain exactly six digits."), 400
        current_app.logger.info("Minecraft accounts stage=wallet_pin_verification_started vercel_id=%s", request_id)
        pin_ok, pin_message = verify_wallet_pin(client_id, pin)
        if not pin_ok:
            current_app.logger.warning("Minecraft accounts stage=wallet_pin_verification_rejected vercel_id=%s", request_id)
            return jsonify(error=pin_message or "Wallet PIN verification failed."), 401
        current_app.logger.info("Minecraft accounts stage=wallet_pin_verified vercel_id=%s", request_id)

        current_app.logger.info("Minecraft accounts stage=account_database_connection_started vercel_id=%s", request_id)
        conn = get_db_connection()
        if conn is None:
            return jsonify(error="MineBank is temporarily unavailable."), 503
        try:
            with conn:
                with conn.cursor() as cur:
                    current_app.logger.info("Minecraft accounts stage=account_query_started vercel_id=%s", request_id)
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
                    rows = cur.fetchall()
                    current_app.logger.info("Minecraft accounts stage=account_query_completed vercel_id=%s account_count=%d", request_id, len(rows))
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
                        for row in rows
                    ]
            current_app.logger.info("Minecraft accounts stage=response_ready vercel_id=%s account_count=%d", request_id, len(accounts))
            return jsonify(accounts=accounts)
        finally:
            release_db_connection(conn)
    except Exception as exc:
        current_app.logger.error("Minecraft account listing failed: %s", type(exc).__name__)
        return jsonify(error="MineBank account data is temporarily unavailable."), 503
    finally:
        current_app.logger.info("Minecraft accounts stage=session_cleanup_started vercel_id=%s", request_id)
        _clear_temporary_login_session()
        current_app.logger.info("Minecraft accounts stage=request_cleanup_completed vercel_id=%s", request_id)


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
                         AND (c.revoked_at IS NULL)
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
                if row[7] != "ACTIVE" or row[9] not in ("ACTIVE", "LIMITED"):
                    return jsonify(error="This card's linked account is not available."), 403

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
