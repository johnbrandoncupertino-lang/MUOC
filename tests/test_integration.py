import os
from datetime import UTC, datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("SECRET_KEY", "ci-test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@localhost:5432/muoc_test")

from app import app
from bank_lib.database import execute_query, execute_query_dict, init_db


@pytest.fixture(scope="session", autouse=True)
def database():
    assert init_db() is True
    execute_query("DELETE FROM account_charges", commit=True)
    execute_query("DELETE FROM requests", commit=True)
    execute_query("DELETE FROM logs", commit=True)
    execute_query("DELETE FROM users", commit=True)

    execute_query(
        """
        INSERT INTO settings
            (bank_name, currency_name, admin_password, maximum_currency)
        VALUES (%s, %s, %s, %s)
        """,
        ("MUOC CI Bank", "MUOC", generate_password_hash("admin-pass"), 1000000.0),
        commit=True,
    )
    execute_query(
        """
        INSERT INTO users
            (wallet_name, password, current_currency, account_tier, credit_limit, credit_used)
        VALUES
            ('admin', %s, 1000000, 'Personal', 0, 0),
            ('alice', %s, 100, 'Personal', 1000, 0),
            ('bob', %s, 0, 'Personal', 1000, 0)
        """,
        (
            generate_password_hash("admin-pass"),
            generate_password_hash("alice-pass"),
            generate_password_hash("bob-pass"),
        ),
        commit=True,
    )
    yield
    execute_query("DELETE FROM account_charges", commit=True)
    execute_query("DELETE FROM requests", commit=True)
    execute_query("DELETE FROM logs", commit=True)
    execute_query("DELETE FROM users", commit=True)
    execute_query("DELETE FROM settings", commit=True)


@pytest.fixture
def client():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app.test_client()


def login_as(client, wallet_name):
    with client.session_transaction() as session:
        session["wallet_name"] = wallet_name
        session["admin"] = wallet_name == "admin"
        session.permanent = True


def test_database_initializes_and_routes_exist():
    assert init_db() is True
    assert "/login" in {rule.rule for rule in app.url_map.iter_rules()}
    assert execute_query("SELECT COUNT(*) FROM users")[0][0] == 3


def test_three_failed_logins_lock_wallet_for_five_minutes(client):
    for expected_attempt in (1, 2):
        response = client.post(
            "/login",
            data={"wallet_name": "alice", "password": "wrong-password"},
        )
        assert response.status_code == 200
        assert f"Failed attempt {expected_attempt} of 3" in response.get_data(as_text=True)

    response = client.post(
        "/login",
        data={"wallet_name": "alice", "password": "wrong-password"},
    )
    assert response.status_code == 200
    assert "locked for 5 minutes" in response.get_data(as_text=True)

    user = execute_query_dict(
        "SELECT failed_login_attempts, locked_until FROM users WHERE wallet_name='alice'"
    )[0]
    assert user["failed_login_attempts"] == 0
    assert user["locked_until"] is not None
    assert user["locked_until"] > datetime.now(UTC) + timedelta(minutes=4)


def test_successful_login_resets_lock_state(client):
    execute_query(
        """
        UPDATE users
        SET failed_login_attempts=2, locked_until=NULL
        WHERE wallet_name='alice'
        """,
        commit=True,
    )

    response = client.post(
        "/login",
        data={"wallet_name": "alice", "password": "alice-pass"},
    )
    assert response.status_code in (302, 303)

    user = execute_query_dict(
        "SELECT failed_login_attempts, locked_until, last_login FROM users WHERE wallet_name='alice'"
    )[0]
    assert user["failed_login_attempts"] == 0
    assert user["locked_until"] is None
    assert user["last_login"] is not None


def test_credit_draw_enforces_limit_and_increases_debt(client):
    login_as(client, "alice")

    response = client.post("/api/credit/draw", json={"amount": 250})
    assert response.status_code == 200
    body = response.get_json()
    assert body["available_credit"] == pytest.approx(750)

    user = execute_query_dict(
        "SELECT current_currency, credit_used FROM users WHERE wallet_name='alice'"
    )[0]
    assert user["current_currency"] == pytest.approx(-150)
    assert user["credit_used"] == pytest.approx(250)

    response = client.post("/api/credit/draw", json={"amount": 751})
    assert response.status_code == 400

    user = execute_query_dict(
        "SELECT credit_used FROM users WHERE wallet_name='alice'"
    )[0]
    assert user["credit_used"] == pytest.approx(250)


def test_positive_bank_transfer_reduces_outstanding_credit(client):
    login_as(client, "admin")
    execute_query(
        """
        UPDATE users
        SET current_currency=-150, credit_used=250
        WHERE wallet_name='alice'
        """,
        commit=True,
    )

    response = client.post(
        "/api/transfer/bank",
        json={
            "wallet_name": "alice",
            "amount": 100,
            "category": "Credit repayment",
            "reason": "Integration test deposit",
        },
    )
    assert response.status_code == 200

    user = execute_query_dict(
        "SELECT current_currency, credit_used FROM users WHERE wallet_name='alice'"
    )[0]
    assert user["current_currency"] == pytest.approx(-50)
    assert user["credit_used"] == pytest.approx(150)


def test_credit_request_creates_pending_request(client):
    login_as(client, "alice")

    response = client.post(
        "/api/request/credit",
        json={"requested_limit": 5000, "reason": "Need additional working capital"},
    )
    assert response.status_code == 200

    request_row = execute_query_dict(
        """
        SELECT request_type, wallet_name, status
        FROM requests
        WHERE wallet_name='alice' AND request_type='CreditLine'
        ORDER BY id DESC LIMIT 1
        """
    )[0]
    assert request_row["request_type"] == "CreditLine"
    assert request_row["wallet_name"] == "alice"
    assert request_row["status"] == "Pending"
