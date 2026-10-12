import os

from flask import Flask

from bank_lib.minebank_minecraft import (
    hash_card_token,
    minecraft_bp,
    minecraft_enabled,
)


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
