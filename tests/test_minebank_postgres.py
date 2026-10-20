import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash
from flask import session

os.environ.setdefault("SECRET_KEY", "ci-test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@localhost:5432/muoc_test")

from bank_lib.database import ensure_minebank_schema, execute_query, execute_query_dict, get_db_connection, release_db_connection
from bank_lib.minebank_auth import create_account, set_wallet_pin, verify_wallet_pin
from bank_lib import minebank_minecraft as minecraft_api
from bank_lib.minebank_minecraft import hash_card_token
from bank_lib.minebank_core import (
    activate_credit,
    accrue_daily_credit_interest,
    preview_transfer,
    transfer,
)


@pytest.fixture(scope="session", autouse=True)
def database():
    assert ensure_minebank_schema() is True
    # Apply the additive Minecraft card migration only inside the disposable CI
    # database; production migrations remain an explicit operator action.
    migration_conn = get_db_connection()
    assert migration_conn is not None
    try:
        with migration_conn:
            with migration_conn.cursor() as migration_cur:
                for migration_name in ("minebank_minecraft.sql", "minepoints.sql", "mineclubs.sql"):
                    migration_sql = (ROOT / "extras" / migration_name).read_text(encoding="utf-8")
                    migration_cur.execute(migration_sql)
    finally:
        release_db_connection(migration_conn)
    execute_query("TRUNCATE TABLE minepoints_redemptions, minepoints_ledger RESTART IDENTITY CASCADE", fetch=False, commit=True)
    execute_query("DELETE FROM minepoints_rewards", fetch=False, commit=True)
    execute_query(
        """
        TRUNCATE TABLE
            ledger_transactions,
            credit_statements,
            credit_facilities,
            minebank_payment_requests,
            minebank_scheduled_transfers,
            minebank_business_payment_approvals,
            bank_requests_v2,
            bank_notifications,
            audit_events,
            minebank_risk_events,
            minebank_banking_locks,
            minebank_sessions,
            bank_accounts,
            bank_clients
        RESTART IDENTITY CASCADE
        """,
        fetch=False,
        commit=True,
    )
    execute_query("DELETE FROM settings", fetch=False, commit=True)
    execute_query(
        """
        INSERT INTO bank_clients(email,password_hash,role,status,wallet_pin_hash)
        VALUES
            ('alice@minebank.test', %s, 'CLIENT', 'ACTIVE', %s),
            ('bob@minebank.test', %s, 'CLIENT', 'ACTIVE', %s)
        """,
        (
            generate_password_hash("alice-password"),
            generate_password_hash("123456"),
            generate_password_hash("bob-password"),
            generate_password_hash("654321"),
        ),
        fetch=False,
        commit=True,
    )
    yield


@pytest.fixture
def accounts():
    execute_query("TRUNCATE TABLE ledger_transactions, credit_statements, credit_facilities, bank_accounts RESTART IDENTITY CASCADE", fetch=False, commit=True)
    alice = execute_query_dict(
        "SELECT id FROM bank_clients WHERE email='alice@minebank.test'"
    )[0]["id"]
    bob = execute_query_dict(
        "SELECT id FROM bank_clients WHERE email='bob@minebank.test'"
    )[0]["id"]
    alice_account_id, alice_number = create_account(alice, "PERSONAL", "PERSONAL")
    bob_account_id, bob_number = create_account(bob, "PERSONAL", "PERSONAL")
    execute_query(
        "UPDATE bank_accounts SET balance=2000 WHERE id=%s",
        (alice_account_id,),
        fetch=False,
        commit=True,
    )
    execute_query(
        "UPDATE bank_accounts SET balance=100 WHERE id=%s",
        (bob_account_id,),
        fetch=False,
        commit=True,
    )
    return alice, bob, alice_account_id, alice_number, bob_account_id, bob_number


def test_minepoints_ledger_is_idempotent_and_separate_from_bank_balance(accounts):
    from bank_lib.minebank_points import record_points_entry, get_points_balance

    alice, _, alice_account_id, _, _, _ = accounts
    before = execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s", (alice_account_id,))[0]["balance"]

    award = record_points_entry(
        alice, 100, "AWARD", "promo:welcome:alice", "Approved test promotion",
        source_type="PROMOTION", source_id="welcome-test", related_account_id=alice_account_id,
        rule_version="test-v1",
    )
    assert award == {"entry_id": award["entry_id"], "replayed": False, "points_balance": 100}
    replay = record_points_entry(
        alice, 100, "AWARD", "promo:welcome:alice", "Approved test promotion",
        source_type="PROMOTION", source_id="welcome-test", related_account_id=alice_account_id,
        rule_version="test-v1",
    )
    assert replay["entry_id"] == award["entry_id"]
    assert replay["replayed"] is True
    assert get_points_balance(alice) == 100

    debit = record_points_entry(
        alice, -40, "REDEMPTION", "redeem:test:alice", "Test redemption",
        source_type="TEST_REWARD", source_id="test-reward",
    )
    assert debit["points_balance"] == 60
    assert get_points_balance(alice) == 60
    after = execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s", (alice_account_id,))[0]["balance"]
    assert before == after

    from bank_lib.minebank_points import InsufficientPointsError, IdempotencyConflictError
    with pytest.raises(InsufficientPointsError):
        record_points_entry(alice, -61, "REDEMPTION", "redeem:too-many", "Must not overdraw")
    with pytest.raises(IdempotencyConflictError):
        record_points_entry(
            alice, 101, "AWARD", "promo:welcome:alice", "Different payload",
            source_type="PROMOTION", source_id="welcome-test", related_account_id=alice_account_id,
            rule_version="test-v1",
        )
    assert get_points_balance(alice) == 60


def test_minepoints_reversal_requires_link_and_awards_have_correct_sign():
    from bank_lib.minebank_points import _validate_entry

    with pytest.raises(ValueError, match="reference"):
        _validate_entry(10, "REVERSAL", "reverse:1", "Reverse award", None)
    with pytest.raises(ValueError, match="add points"):
        _validate_entry(-10, "AWARD", "award:1", "Award points", None)
    with pytest.raises(ValueError, match="deduct points"):
        _validate_entry(10, "REDEMPTION", "redeem:1", "Redeem points", None)


def test_schema_and_canonical_tiers_exist():
    rows = execute_query_dict(
        """
        SELECT code,monthly_fee,opening_fee,max_balance,monthly_outgoing_limit,
               daily_outgoing_limit,default_credit_limit
        FROM account_tiers_v2
        ORDER BY code
        """
    )
    tiers = {row["code"]: row for row in rows}
    assert tiers["PERSONAL"]["monthly_fee"] == 0
    assert tiers["PERSONAL"]["opening_fee"] == 0
    assert tiers["PERSONAL"]["max_balance"] == 5000
    assert tiers["PERSONAL"]["monthly_outgoing_limit"] == 10000
    assert tiers["PERSONAL"]["daily_outgoing_limit"] == 1000
    assert tiers["PERSONAL"]["default_credit_limit"] == 1800
    assert tiers["PERSONAL_PRO"]["opening_fee"] == 40
    assert tiers["PERSONAL_PRIVATE"]["monthly_fee"] == 50
    assert tiers["BUSINESS"]["max_balance"] == 50000
    assert tiers["BUSINESS_PRO"]["default_credit_limit"] == 100000
    assert tiers["CORPORATE"]["default_credit_limit"] == 5000000


def test_wallet_pin_locks_for_five_minutes_after_three_failures():
    client_id = execute_query_dict(
        "SELECT id FROM bank_clients WHERE email='alice@minebank.test'"
    )[0]["id"]
    assert verify_wallet_pin(client_id, "000000")[0] is False
    assert verify_wallet_pin(client_id, "000000")[0] is False
    ok, message = verify_wallet_pin(client_id, "000000")
    assert ok is False
    assert "5 minutes" in message

    row = execute_query_dict(
        "SELECT wallet_pin_failed_attempts,wallet_pin_locked_until "
        "FROM bank_clients WHERE id=%s",
        (client_id,),
    )[0]
    assert row["wallet_pin_failed_attempts"] == 0
    assert row["wallet_pin_locked_until"] > datetime.now(timezone.utc) + timedelta(minutes=4)


def test_transfer_tariff_is_free_through_500_and_five_over_500(accounts):
    alice, bob, alice_id, _, _, bob_number = accounts
    preview_free = preview_transfer(alice, alice_id, bob_number, 500)
    assert preview_free["fee"] == 0

    execute_query(
        "UPDATE bank_accounts SET last_outgoing_at=NULL WHERE id=%s",
        (alice_id,),
        fetch=False,
        commit=True,
    )
    preview_fee = preview_transfer(alice, alice_id, bob_number, 501)
    assert preview_fee["fee"] == 5


def test_transfer_over_5000_is_pending_approval(accounts):
    alice, bob, alice_id, _, _, bob_number = accounts
    execute_query(
        "UPDATE bank_accounts SET balance=20000,last_outgoing_at=NULL WHERE id=%s",
        (alice_id,),
        fetch=False,
        commit=True,
    )
    # Use a recipient tier that can accept an amount above the Personal
    # account's 5,000 Emerald balance cap; this test targets approval logic.
    recipient_id, recipient_number = create_account(bob, "BUSINESS", "BUSINESS")
    execute_query(
        "UPDATE bank_accounts SET balance=100 WHERE id=%s",
        (recipient_id,),
        fetch=False,
        commit=True,
    )
    # Personal has a 1,000 Emerald daily limit, so use a Business Pro sender.
    private_id, private_number = create_account(alice, "BUSINESS", "BUSINESS_PRO")
    execute_query(
        "UPDATE bank_accounts SET balance=20000,last_outgoing_at=NULL WHERE id=%s",
        (private_id,),
        fetch=False,
        commit=True,
    )
    result = transfer(
        sender_account_id=private_id,
        recipient_account_number=recipient_number,
        amount=5001,
        actor_client_id=alice,
        idempotency_key="ci-approval-5001",
    )
    assert result["status"] == "PENDING_APPROVAL"
    tx = execute_query_dict(
        "SELECT status,amount,fee FROM ledger_transactions WHERE transaction_id=%s",
        (result["transaction_id"],),
    )[0]
    assert tx["status"] == "PENDING_APPROVAL"
    assert tx["amount"] == 5001
    assert tx["fee"] == 5


def test_cashline_limits_and_daily_interest(accounts):
    alice, _, alice_id, _, _, bob_number = accounts
    execute_query("UPDATE bank_accounts SET balance=0 WHERE id=%s", (alice_id,), fetch=False, commit=True)
    activation = activate_credit(alice_id, 500)
    assert activation["credit_limit"] == 500
    assert activation["interest_annual_bps"] == 1830
    assert activation["monthly_activation_fee"] == 10

    # CashLine is drawn by selecting it as the transfer funding source;
    # the ordinary account balance must not become negative.
    payment = transfer(
        sender_account_id=alice_id,
        recipient_account_number=bob_number,
        amount=300,
        actor_client_id=alice,
        funding_source="CASHLINE",
        idempotency_key="ci-cashline-300",
    )
    assert payment["status"] == "COMPLETED"
    assert payment["amount"] == 300

    execute_query(
        "UPDATE credit_facilities SET last_interest_at=%s WHERE account_id=%s",
        (datetime.now(timezone.utc) - timedelta(days=1), alice_id),
        fetch=False,
        commit=True,
    )
    result = accrue_daily_credit_interest(alice_id)
    assert result and result[0]["interest"] >= 1

    balance = execute_query_dict(
        "SELECT balance FROM bank_accounts WHERE id=%s", (alice_id,)
    )[0]["balance"]
    assert balance == 0

    with pytest.raises(ValueError, match="between 300 and 1800"):
        activate_credit(alice_id, 2000)


def test_web_smoke_routes_import_and_render():
    # Catches deployment-time import errors and dead public/login routes without
    # changing the portal's visual templates or requiring an authenticated user.
    from app import app

    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = app.test_client()
    for path in ("/", "/about", "/login", "/portal/login", "/portal/register"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code < 500, (path, response.status_code, response.get_data(as_text=True)[:500])


def test_minecraft_card_issuance_read_and_revocation(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    alice, bob, alice_account_id, _, bob_account_id, _ = accounts

    # Exercise the real route/database operations while isolating credential
    # verification from the portal's CAPTCHA and password hashing implementation.
    def fake_login(email, password, captcha_answer=None):
        if email == "alice@minebank.test" and password == "alice-password":
            session["minebank_client_id"] = alice
            session["minebank_role"] = "CLIENT"
            return True, None
        return False, "Invalid credentials."

    monkeypatch.setattr(minecraft_api, "login_client", fake_login)
    monkeypatch.setattr(minecraft_api, "verify_wallet_pin", lambda client_id, pin: (
        (client_id == alice and pin == "123456"), None if pin == "123456" else "Invalid wallet PIN."
    ))

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    secure_headers = {"X-Forwarded-Proto": "https"}

    listed = client.post(
        "/api/v1/minecraft/accounts",
        json={"email": "alice@minebank.test", "password": "alice-password",
              "wallet_pin": "123456"},
        headers=secure_headers,
    )
    assert listed.status_code == 200, listed.get_json()
    listed_accounts = listed.get_json()["accounts"]
    assert [account["id"] for account in listed_accounts] == [alice_account_id]
    assert listed_accounts[0]["balance"] == 2000

    # A customer's credentials must not authorize another customer's account.
    wrong_owner = client.post(
        "/api/v1/minecraft/cards",
        json={"email": "alice@minebank.test", "password": "alice-password",
              "wallet_pin": "123456", "account_id": bob_account_id},
        headers=secure_headers,
    )
    assert wrong_owner.status_code == 404

    issued = client.post(
        "/api/v1/minecraft/cards",
        json={"email": "alice@minebank.test", "password": "alice-password",
              "wallet_pin": "123456", "account_id": alice_account_id,
              "label": "CI test card"},
        headers=secure_headers,
    )
    assert issued.status_code == 201, issued.get_json()
    payload = issued.get_json()
    card_id = payload["card"]["id"]
    token = payload["credential"]
    assert token.startswith("mbmc_")

    stored = execute_query_dict(
        "SELECT card_token_hash,card_prefix,status FROM minebank_minecraft_cards WHERE id=%s",
        (card_id,),
    )[0]
    assert stored["card_token_hash"] == hash_card_token(token)
    assert stored["card_token_hash"] != token
    assert stored["card_prefix"] == token[:16]
    assert stored["status"] == "ACTIVE"

    account_response = client.get(
        "/api/v1/minecraft/account",
        headers={**secure_headers, "Authorization": f"Bearer {token}"},
    )
    assert account_response.status_code == 200, account_response.get_json()
    assert account_response.get_json()["account"]["balance"] == 2000
    assert account_response.get_json()["account"]["currency"] == "emerald"

    # Frozen accounts must not be exposed through a valid card.
    execute_query(
        "UPDATE bank_accounts SET status='FROZEN' WHERE id=%s",
        (alice_account_id,), fetch=False, commit=True,
    )
    frozen = client.get(
        "/api/v1/minecraft/account",
        headers={**secure_headers, "Authorization": f"Bearer {token}"},
    )
    assert frozen.status_code == 403
    execute_query(
        "UPDATE bank_accounts SET status='ACTIVE' WHERE id=%s",
        (alice_account_id,), fetch=False, commit=True,
    )

    revoked = client.post(
        "/api/v1/minecraft/cards/revoke",
        json={"email": "alice@minebank.test", "password": "alice-password",
              "wallet_pin": "123456", "card_id": card_id},
        headers=secure_headers,
    )
    assert revoked.status_code == 200, revoked.get_json()
    assert revoked.get_json()["status"] == "REVOKED"

    denied = client.get(
        "/api/v1/minecraft/account",
        headers={**secure_headers, "Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 401



def _insert_test_minecraft_card(client_id, account_id, token):
    execute_query(
        """INSERT INTO minebank_minecraft_cards
           (client_id,account_id,card_prefix,card_token_hash,label)
           VALUES(%s,%s,%s,%s,'transaction test card')""",
        (client_id, account_id, token[:16], hash_card_token(token)),
        fetch=False, commit=True,
    )
    return execute_query_dict(
        "SELECT id FROM minebank_minecraft_cards WHERE card_prefix=%s AND client_id=%s",
        (token[:16], client_id),
    )[0]["id"]


def _insert_test_minecraft_atm(client_id, card_id, token):
    execute_query(
        """INSERT INTO minebank_minecraft_atms
           (client_id,enrolled_by_card_id,token_prefix,token_hash,label)
           VALUES(%s,%s,%s,%s,'test ClubCash ATM')""",
        (client_id, card_id, token[:16], hash_card_token(token)),
        fetch=False, commit=True,
    )



def _insert_test_mineclub(terminal_token, code="padua-central"):
    rows = execute_query(
        """INSERT INTO minebank_club_locations(code,display_name,status)
           VALUES(%s,%s,'ACTIVE') RETURNING id""",
        (code, "MineClubs Padua Central"),
        fetch=True, commit=True,
    )
    club_id = rows[0][0]
    execute_query(
        """INSERT INTO minebank_club_terminals(club_id,label,token_hash,status)
           VALUES(%s,%s,%s,'ACTIVE')""",
        (club_id, "CI entrance", hash_card_token(terminal_token)),
        fetch=False, commit=True,
    )
    return club_id


def test_minecraft_merchant_payment_is_atomic_and_idempotent(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, bob, alice_account_id, _, bob_account_id, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    card_token = "mbmc_" + "p" * 48
    merchant_token = "mbmerchant_" + "m" * 48
    _insert_test_minecraft_card(alice, alice_account_id, card_token)
    merchant_account_id, _ = create_account(bob, "BUSINESS", "BUSINESS")
    execute_query("UPDATE bank_accounts SET balance=100 WHERE id=%s", (merchant_account_id,), fetch=False, commit=True)
    execute_query(
        """INSERT INTO minebank_minecraft_merchants
           (client_id,account_id,token_hash,display_name,active)
           VALUES(%s,%s,%s,'CI Shop',TRUE)""",
        (bob, merchant_account_id, hash_card_token(merchant_token)),
        fetch=False, commit=True,
    )

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    secure = {"X-Forwarded-Proto": "https"}

    checkout = client.post(
        "/api/v1/minecraft/merchant/checkouts",
        json={"amount": 100, "description": "CI shop item"},
        headers={
            **secure,
            "Authorization": f"Bearer {merchant_token}",
            "Idempotency-Key": "ci-checkout-1",
        },
    )
    assert checkout.status_code == 201, checkout.get_json()
    checkout_id = checkout.get_json()["checkout_id"]

    headers = {
        **secure,
        "Authorization": f"Bearer {card_token}",
        "Idempotency-Key": "ci-payment-1",
    }
    payment = client.post(
        "/api/v1/minecraft/payments",
        json={"checkout_id": checkout_id, "wallet_pin": "123456"},
        headers=headers,
    )
    assert payment.status_code == 200, payment.get_json()
    assert payment.get_json()["amount"] == 100
    assert payment.get_json()["status"] == "COMPLETED"

    replay = client.post(
        "/api/v1/minecraft/payments",
        json={"checkout_id": checkout_id, "wallet_pin": "123456"},
        headers=headers,
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True

    balances = execute_query_dict(
        "SELECT id,balance FROM bank_accounts WHERE id IN (%s,%s) ORDER BY id",
        (alice_account_id, merchant_account_id),
    )
    by_id = {row["id"]: row["balance"] for row in balances}
    assert by_id[alice_account_id] == 1900
    assert by_id[merchant_account_id] == 200
    rows = execute_query_dict(
        "SELECT COUNT(*) AS n FROM ledger_transactions WHERE transaction_type='MINECRAFT_PAYMENT'"
    )
    assert rows[0]["n"] == 1


def test_minecraft_atm_withdrawal_is_atomic_and_idempotent(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", "mbatm_" + "a" * 48)
    alice, _, alice_account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    card_token = "mbmc_" + "w" * 48
    card_id = _insert_test_minecraft_card(alice, alice_account_id, card_token)
    _insert_test_minecraft_atm(alice, card_id, "mbatm_" + "a" * 48)

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {
        "X-Forwarded-Proto": "https",
        "X-MineBank-ATM-Token": "mbatm_" + "a" * 48,
        "Authorization": f"Bearer {card_token}",
        "Idempotency-Key": "ci-atm-1",
    }

    withdrawal = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 64, "wallet_pin": "123456"},
        headers=headers,
    )
    assert withdrawal.status_code == 200, withdrawal.get_json()
    assert withdrawal.get_json()["items"] == 64

    replay = client.post(
        "/api/v1/minecraft/withdrawals",
        json={"amount": 64, "wallet_pin": "123456"},
        headers=headers,
    )
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True

    balance = execute_query_dict(
        "SELECT balance FROM bank_accounts WHERE id=%s", (alice_account_id,)
    )[0]["balance"]
    assert balance == 1936
    rows = execute_query_dict(
        "SELECT COUNT(*) AS n FROM ledger_transactions WHERE transaction_type='WITHDRAWAL'"
    )
    assert rows[0]["n"] == 1


def test_clubcash_cashline_advance_is_atomic_and_idempotent(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, _, account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    activate_credit(account_id, 500)

    card_token = "mbmc_" + "c" * 48
    machine_token = "mbatm_" + "b" * 48
    card_id = _insert_test_minecraft_card(alice, account_id, card_token)
    _insert_test_minecraft_atm(alice, card_id, machine_token)
    before_balance = execute_query_dict(
        "SELECT balance FROM bank_accounts WHERE id=%s", (account_id,)
    )[0]["balance"]

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {
        "X-Forwarded-Proto": "https",
        "X-MineBank-ATM-Token": machine_token,
        "Authorization": f"Bearer {card_token}",
        "Idempotency-Key": "ci-clubcash-cashline-advance",
    }
    payload = {"amount": 64, "wallet_pin": "123456", "funding_source": "CASHLINE"}

    advance = client.post("/api/v1/minecraft/withdrawals", json=payload, headers=headers)
    assert advance.status_code == 200, advance.get_json()
    assert advance.get_json()["funding_source"] == "CASHLINE"
    assert advance.get_json()["amount"] == 64

    replay = client.post("/api/v1/minecraft/withdrawals", json=payload, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True

    after_balance = execute_query_dict(
        "SELECT balance FROM bank_accounts WHERE id=%s", (account_id,)
    )[0]["balance"]
    assert after_balance == before_balance
    rows = execute_query_dict(
        """SELECT COUNT(*) AS n FROM ledger_transactions
           WHERE transaction_type='CREDIT_DRAW' AND recipient_account_id=%s
             AND transfer_kind='CASHLINE' AND reference_id=%s""",
        (account_id, "ci-clubcash-cashline-advance"),
    )
    assert rows[0]["n"] == 1


def test_minecraft_atm_deposit_is_atomic_and_idempotent(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    machine_token = "mbatm_" + "a" * 48
    monkeypatch.setenv("MINEBANK_MINECRAFT_ATM_TOKEN", machine_token)
    alice, _, account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    card_token = "mbmc_" + "d" * 48
    card_id = _insert_test_minecraft_card(alice, account_id, card_token)
    _insert_test_minecraft_atm(alice, card_id, machine_token)

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {
        "X-Forwarded-Proto": "https",
        "X-MineBank-ATM-Token": machine_token,
        "Authorization": f"Bearer {card_token}",
        "Idempotency-Key": "ci-atm-deposit-1",
    }
    deposit = client.post("/api/v1/minecraft/deposits",
                          json={"amount": 32, "wallet_pin": "123456"}, headers=headers)
    assert deposit.status_code == 200, deposit.get_json()
    assert deposit.get_json()["amount"] == 32
    replay = client.post("/api/v1/minecraft/deposits",
                         json={"amount": 32, "wallet_pin": "123456"}, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True
    balance = execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s", (account_id,))[0]["balance"]
    assert balance == 2032
    rows = execute_query_dict("SELECT COUNT(*) AS n FROM ledger_transactions WHERE transaction_type='DEPOSIT'")
    assert rows[0]["n"] == 1



def test_mineclubs_admission_charges_personal_once_and_replays(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, _, account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    card_token = "mbmc_" + "p" * 48
    club_token = "mbclub_" + "q" * 48
    _insert_test_minecraft_card(alice, account_id, card_token)
    _insert_test_mineclub(club_token)
    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {"X-Forwarded-Proto":"https","X-MineBank-Club-Token":club_token,
               "Authorization":f"Bearer {card_token}","Idempotency-Key":"ci-club-admission-personal"}
    response = client.post("/api/v1/minecraft/clubs/admissions",
                           json={"wallet_pin":"123456"}, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["fee"] == 50
    replay = client.post("/api/v1/minecraft/clubs/admissions",
                         json={"wallet_pin":"123456"}, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True
    assert execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s",(account_id,))[0]["balance"] == 1950
    assert execute_query_dict("SELECT COUNT(*) AS n FROM minebank_club_admissions")[0]["n"] == 1
    assert execute_query_dict("SELECT COUNT(*) AS n FROM ledger_transactions WHERE transfer_kind='MINECLUB_ADMISSION'")[0]["n"] == 1


def test_mineclubs_private_admission_is_free_but_audited(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, _, account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    tier = execute_query_dict("SELECT id FROM account_tiers_v2 WHERE code='PERSONAL_PRIVATE' AND active=TRUE")[0]["id"]
    execute_query("UPDATE bank_accounts SET tier_id=%s WHERE id=%s",(tier,account_id),fetch=False,commit=True)
    card_token = "mbmc_" + "r" * 48
    club_token = "mbclub_" + "s" * 48
    _insert_test_minecraft_card(alice, account_id, card_token)
    _insert_test_mineclub(club_token, code="private-free")
    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {"X-Forwarded-Proto":"https","X-MineBank-Club-Token":club_token,
               "Authorization":f"Bearer {card_token}","Idempotency-Key":"ci-club-admission-private"}
    response = client.post("/api/v1/minecraft/clubs/admissions",json={"wallet_pin":"123456"},headers=headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["fee"] == 0 and response.get_json()["unlimited_admission"] is True
    assert execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s",(account_id,))[0]["balance"] == 2000
    assert execute_query_dict("SELECT COUNT(*) AS n FROM minebank_club_admissions WHERE fee_amount=0")[0]["n"] == 1
    assert execute_query_dict("SELECT COUNT(*) AS n FROM ledger_transactions WHERE transfer_kind='MINECLUB_ADMISSION'")[0]["n"] == 0


def test_mineclubs_insufficient_balance_does_not_admit(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, _, account_id, _, _, _ = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    execute_query("UPDATE bank_accounts SET balance=10 WHERE id=%s",(account_id,),fetch=False,commit=True)
    card_token = "mbmc_" + "u" * 48
    club_token = "mbclub_" + "v" * 48
    _insert_test_minecraft_card(alice, account_id, card_token)
    _insert_test_mineclub(club_token, code="insufficient")
    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    response = client.post("/api/v1/minecraft/clubs/admissions",json={"wallet_pin":"123456"},
        headers={"X-Forwarded-Proto":"https","X-MineBank-Club-Token":club_token,
                 "Authorization":f"Bearer {card_token}","Idempotency-Key":"ci-club-insufficient"})
    assert response.status_code == 409, response.get_json()
    assert execute_query_dict("SELECT balance FROM bank_accounts WHERE id=%s",(account_id,))[0]["balance"] == 10
    assert execute_query_dict("SELECT COUNT(*) AS n FROM minebank_club_admissions")[0]["n"] == 0


def test_minecraft_personal_transfer_is_atomic_and_idempotent(accounts, monkeypatch):
    monkeypatch.setenv("MINEBANK_MINECRAFT_ENABLED", "true")
    monkeypatch.setenv("MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED", "true")
    alice, _, alice_account_id, _, bob_account_id, bob_number = accounts
    execute_query("UPDATE bank_clients SET wallet_pin_failed_attempts=0,wallet_pin_locked_until=NULL WHERE id=%s",
                  (alice,), fetch=False, commit=True)
    card_token = "mbmc_" + "t" * 48
    _insert_test_minecraft_card(alice, alice_account_id, card_token)

    from flask import Flask
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="ci-test-secret")
    app.register_blueprint(minecraft_api.minecraft_bp)
    client = app.test_client()
    headers = {
        "X-Forwarded-Proto": "https",
        "Authorization": f"Bearer {card_token}",
        "Idempotency-Key": "ci-transfer-1",
    }
    payload = {"recipient_account_number": bob_number, "amount": 600, "wallet_pin": "123456"}
    transfer_response = client.post("/api/v1/minecraft/transfers", json=payload, headers=headers)
    assert transfer_response.status_code == 200, transfer_response.get_json()
    assert transfer_response.get_json()["fee"] == 5
    replay = client.post("/api/v1/minecraft/transfers", json=payload, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["idempotent_replay"] is True
    balances = execute_query_dict(
        "SELECT id,balance FROM bank_accounts WHERE id IN (%s,%s) ORDER BY id",
        (alice_account_id, bob_account_id),
    )
    by_id = {row["id"]: row["balance"] for row in balances}
    assert by_id[alice_account_id] == 1395
    assert by_id[bob_account_id] == 700
    rows = execute_query_dict("SELECT COUNT(*) AS n FROM ledger_transactions WHERE transfer_kind='MINECRAFT_TRANSFER'")
    assert rows[0]["n"] == 1
