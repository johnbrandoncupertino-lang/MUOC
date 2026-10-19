import os
from datetime import datetime, timezone

from flask import Flask, session

from bank_lib.minebank_minecraft import (
    hash_card_token,
    minecraft_bp,
    minecraft_enabled,
    _merchant_payment_status_code,
    _merchant_payment_error_code,
    _validate_outgoing_limits,
)
from bank_lib import minebank_minecraft as minecraft_api


def test_minecraft_feature_flag_fails_closed(monkeypatch):
    monkeypatch.delenv("MINEBANK_MINECRAFT_ENABLED", raising=False)
    assert minecraft_enabled() is False
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "yes")
    assert minecraft_enabled() is False
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    assert minecraft_enabled() is True


def test_card_token_hash_is_stable_and_not_the_token():
    token = "mbmc_example-secret-card-token"
    digest = hash_card_token(token)
    assert len(digest) == 64
    assert digest != token
    assert digest == hash_card_token(token)


def test_minecraft_routes_are_hidden_when_feature_disabled(monkeypatch):
    monkeypatch.delenv("MINEBANK_MINECRAFT_ENABLED", raising=False)
    app = Flask(__name__)
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.get("/api/v1/minecraft/account")
    assert response.status_code == 404

    response = client.post("/api/v1/minecraft/cards", json={})
    assert response.status_code == 404


def test_minecraft_card_routes_reject_operator_identity(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")

    def fake_login(email, password, captcha_answer=None):
        session["minebank_client_id"] = 999
        session["minebank_role"] = "OPERATOR"
        return True, None

    monkeypatch.setattr(minecraft_api, "login_client", fake_login)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/accounts",
        json={"email": "operator@minebank.test", "password": "password",
              "wallet_pin": "123456"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 403


def test_minecraft_api_rejects_plain_http_when_enabled(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    app = Flask(__name__)
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.get("/api/v1/minecraft/account")
    assert response.status_code == 400
    assert response.get_json()["error"] == "Minecraft integration requires HTTPS."



def test_minecraft_operation_status_route_is_hidden_when_feature_disabled(monkeypatch):
    monkeypatch.delenv("MINEBANK_MINECRAFT_ENABLED", raising=False)
    app = Flask(__name__)
    app.register_blueprint(minecraft_bp)
    response = app.test_client().get("/api/v1/minecraft/operations/atm-test-key")
    assert response.status_code == 404


def test_minecraft_operation_status_requires_https(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    app = Flask(__name__)
    app.register_blueprint(minecraft_bp)
    response = app.test_client().get(
        "/api/v1/minecraft/operations/atm-test-key",
        headers={"Authorization": "Bearer mbmc_" + "x" * 48},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Minecraft integration requires HTTPS."


def test_minecraft_operation_status_rejects_malformed_key_before_database_access(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setattr(minecraft_api, "_client_id_for_card_token",
                        lambda cursor, token: (_ for _ in ()).throw(AssertionError("DB lookup must not run")))
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    response = app.test_client().get(
        "/api/v1/minecraft/operations/bad%20key",
        headers={
            "Authorization": "Bearer mbmc_" + "x" * 48,
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 400
    assert "operation key" in response.get_json()["error"].lower()


def test_minecraft_operation_status_returns_receipt_to_matching_customer(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=()):
            self.query = query
            self.params = params

        def fetchone(self):
            assert "FROM minebank_minecraft_operations" in self.query
            assert self.params == (72, "atm-status-key")
            return ("WITHDRAWAL", "COMPLETED",
                    {"transaction_id": "MBTX-RECOVERY"}, "MBTX-RECOVERY", 64)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(minecraft_api, "_client_id_for_card_token",
                        lambda cursor, token: 72)
    monkeypatch.setattr(minecraft_api, "get_db_connection", lambda: FakeConnection())
    monkeypatch.setattr(minecraft_api, "release_db_connection", lambda connection: None)

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    response = app.test_client().get(
        "/api/v1/minecraft/operations/atm-status-key",
        headers={
            "Authorization": "Bearer mbmc_" + "x" * 48,
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "found": True,
        "operation_type": "WITHDRAWAL",
        "status": "COMPLETED",
        "response": {"transaction_id": "MBTX-RECOVERY"},
        "transaction_id": "MBTX-RECOVERY",
        "amount": 64,
    }


def test_minecraft_withdrawal_requires_idempotency_key(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", "atm-test-token-" + "a" * 40)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48, "X-Forwarded-Proto": "https", "X-MineBank-ATM-Token": "atm-test-token-" + "a" * 40},
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.get_json()["error"]


def test_minecraft_withdrawal_rejects_non_integer_amounts(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", "atm-test-token-" + "a" * 40)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10.5},
        headers={
            "Authorization": "Bearer mbmc_" + "x" * 48,
            "Idempotency-Key": "atm-test-1",
            "X-Forwarded-Proto": "https", "X-MineBank-ATM-Token": "atm-test-token-" + "a" * 40,
        },
    )
    assert response.status_code == 400


def test_minecraft_merchant_checkout_rejects_fractional_amounts(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/merchant/checkouts",
        json={"amount": 10.5},
        headers={
            "Authorization": "Bearer mbmerchant_" + "x" * 48,
            "Idempotency-Key": "shop-test-1",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 400



def test_minecraft_money_movement_requires_separate_feature_flag(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.delenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", raising=False)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10},
        headers={
            "Authorization": "Bearer mbmc_" + "x" * 48,
            "Idempotency-Key": "atm-test-2",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 404
    assert response.get_json()["error"] == "Minecraft transactions are disabled."


def test_clubcash_rejects_dynamic_cashline_advance_without_database_access(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setattr(minecraft_api, "_atm_authorized", lambda: True)
    monkeypatch.setattr(minecraft_api, "_authorized_card_token", lambda: "mbmc_" + "x" * 48)
    monkeypatch.setattr(minecraft_api, "get_db_connection",
                        lambda: (_ for _ in ()).throw(AssertionError("DB must not be touched")))
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)

    response = app.test_client().post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10, "funding_source": "DYNAMIC_CASHLINE", "wallet_pin": "123456"},
        headers={
            "Authorization": "Bearer " + "mbmc_" + "x" * 48,
            "Idempotency-Key": "atm-dynamic-advance",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 400
    assert "Standard CashLine advances only" in response.get_json()["error"]


def test_clubcash_cashline_advance_requires_wallet_pin(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setattr(minecraft_api, "_atm_authorized", lambda: True)
    monkeypatch.setattr(minecraft_api, "_authorized_card_token", lambda: "mbmc_" + "x" * 48)
    monkeypatch.setattr(minecraft_api, "_verify_card_pin", lambda token, pin: (False, "Enter your six-digit Wallet PIN."))
    import bank_lib.credicheck as credicheck
    import bank_lib.database as database
    monkeypatch.setattr(credicheck, "ensure_credicheck_schema", lambda: True)
    monkeypatch.setattr(database, "ensure_loan_schema", lambda: True)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)

    response = app.test_client().post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10, "funding_source": "CASHLINE"},
        headers={
            "Authorization": "Bearer " + "mbmc_" + "x" * 48,
            "Idempotency-Key": "atm-cashline-pin-required",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 401
    assert "Wallet PIN" in response.get_json()["error"]


def test_minecraft_withdrawal_requires_wallet_pin(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", "atm-test-token-" + "a" * 40)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48,
                 "Idempotency-Key": "atm-pin-required", "X-Forwarded-Proto": "https", "X-MineBank-ATM-Token": "atm-test-token-" + "a" * 40},
    )
    assert response.status_code == 401
    assert "Wallet PIN" in response.get_json()["error"]


def test_minecraft_deposit_requires_wallet_pin(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", "atm-test-token-" + "a" * 40)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/deposits",
        json={"amount": 10},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48,
                 "Idempotency-Key": "atm-deposit-pin-required", "X-Forwarded-Proto": "https", "X-MineBank-ATM-Token": "atm-test-token-" + "a" * 40},
    )
    assert response.status_code == 401
    assert "Wallet PIN" in response.get_json()["error"]


def test_minecraft_transfer_limit_is_enforced_before_database_access(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/transfers",
        json={"amount": 1001, "recipient_account_number": "MB123", "wallet_pin": "123456"},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48,
                 "Idempotency-Key": "transfer-over-limit", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 400
    assert "1 to 1000" in response.get_json()["error"]


def test_minecraft_payment_requires_wallet_pin(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/payments",
        json={"checkout_id": "123"},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48,
                 "Idempotency-Key": "payment-pin-required", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 401
    assert "Wallet PIN" in response.get_json()["error"]


def test_minecraft_self_revoke_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/cards/revoke-self",
        json={"wallet_pin": "123456"},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48, "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 400
    assert "confirmation" in response.get_json()["error"].lower()


def test_minecraft_deposit_requires_trusted_atm_credential(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.delenv("MINEBANK_MINECRAFT_ATM_TOKEN", raising=False)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()
    response = client.post(
        "/api/v1/minecraft/deposits",
        json={"amount": 10, "wallet_pin": "123456"},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48,
                 "Idempotency-Key": "atm-machine-required", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 401
    assert "trusted ATM credential" in response.get_json()["error"]

def test_merchant_payment_rejections_are_not_misreported_as_conflicts():
    assert _merchant_payment_status_code("Standard CashLine is not active for this account.") == 422
    assert _merchant_payment_status_code("Insufficient Standard CashLine availability. Available: 0 Emerald.") == 422
    assert _merchant_payment_error_code("Standard CashLine is not active for this account.") == "CASHLINE_NOT_AVAILABLE"
    assert _merchant_payment_error_code("CrediCheck has not authorised this credit product. Check your Inbox for details.") == "CREDICHECK_REJECTED"


def test_merchant_payment_only_uses_conflict_for_real_state_conflicts():
    assert _merchant_payment_status_code("Checkout has expired or has already been paid.") == 409
    assert _merchant_payment_status_code("This Idempotency-Key was already used for a different request.") == 409
    assert _merchant_payment_status_code("Checkout not found.") == 404
    assert _merchant_payment_error_code("This Idempotency-Key was already used for a different request.") == "IDEMPOTENCY_CONFLICT"

def test_minecraft_payment_limits_use_account_id_not_card_id():
    class Cursor:
        def __init__(self):
            self.query = ""
            self.params = ()
            self.calls = []

        def execute(self, query, params=()):
            self.query = query
            self.params = params
            self.calls.append((query, params))

        def fetchone(self):
            if "SELECT monthly_outgoing_limit" in self.query:
                return (1000, 500, 100, None)
            if "SELECT COALESCE(SUM(amount+fee)" in self.query:
                return (0,)
            raise AssertionError("Unexpected query: " + self.query)

    cursor = Cursor()
    current_period = datetime.now(timezone.utc).strftime("%Y-%m")
    # The card row ID at index 0 intentionally differs from its bank account ID at index 2.
    card_row = (9001, 72, 1234, "hash", "ACTIVE", "MB1234", "PERSONAL", 8,
                250, "ACTIVE", 25, current_period, None, "ACTIVE")
    monthly_used, period, limits = _validate_outgoing_limits(cursor, card_row, 10)

    assert monthly_used == 25
    assert period == current_period
    assert limits == (1000, 500, 100, None)
    daily_call = next(call for call in cursor.calls if "SELECT COALESCE(SUM(amount+fee)" in call[0])
    assert daily_call[1] == (1234,)

    reset_cursor = Cursor()
    old_period_row = card_row[:11] + ("1900-01",) + card_row[12:]
    _validate_outgoing_limits(reset_cursor, old_period_row, 10)
    reset_call = next(call for call in reset_cursor.calls if "UPDATE bank_accounts SET monthly_outgoing_used=0" in call[0])
    assert reset_call[1][1] == 1234

