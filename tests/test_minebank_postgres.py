import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("SECRET_KEY", "ci-test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test@localhost:5432/muoc_test")

from bank_lib.database import ensure_minebank_schema, execute_query, execute_query_dict
from bank_lib.minebank_auth import create_account, set_wallet_pin, verify_wallet_pin
from bank_lib.minebank_core import (
    activate_credit,
    accrue_daily_credit_interest,
    draw_credit,
    preview_transfer,
    transfer,
)


@pytest.fixture(scope="session", autouse=True)
def database():
    assert ensure_minebank_schema() is True
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
    # Personal has a 1,000 Emerald daily limit, so use Personal Private for
    # the approval-threshold test.
    private_id, private_number = create_account(alice, "PERSONAL", "PERSONAL_PRIVATE")
    execute_query(
        "UPDATE bank_accounts SET balance=20000,last_outgoing_at=NULL WHERE id=%s",
        (private_id,),
        fetch=False,
        commit=True,
    )
    result = transfer(
        sender_account_id=private_id,
        recipient_account_number=bob_number,
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
    assert tx["fee"] == 0


def test_cashline_limits_and_daily_interest(accounts):
    alice, _, alice_id, _, _, _ = accounts
    activation = activate_credit(alice_id, 500)
    assert activation["credit_limit"] == 500
    assert activation["interest_annual_bps"] == 1830
    assert activation["monthly_activation_fee"] == 10

    draw = draw_credit(alice_id, 300, actor_client_id=alice)
    assert draw["available_credit"] == 200

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
    assert balance < 0

    with pytest.raises(ValueError, match="between 300 and 1800"):
        activate_credit(alice_id, 2000)
