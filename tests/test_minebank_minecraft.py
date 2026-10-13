import os

from flask import Flask, session

from bank_lib.minebank_minecraft import (
    hash_card_token,
    minecraft_bp,
    minecraft_enabled,
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



def test_minecraft_withdrawal_requires_idempotency_key(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_bp)
    client = app.test_client()

    response = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 10},
        headers={"Authorization": "Bearer mbmc_" + "x" * 48, "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 400
    assert "Idempotency-Key" in response.get_json()["error"]


def test_minecraft_withdrawal_rejects_non_integer_amounts(monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
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
            "X-Forwarded-Proto": "https",
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
