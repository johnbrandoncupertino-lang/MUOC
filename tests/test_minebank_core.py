import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


def test_money_is_integer():
    from bank_lib.minebank_core import transfer
    with pytest.raises(ValueError):
        transfer(sender_account_id=1, recipient_account_number="MB-TEST", amount=1.5)


def test_threshold_is_approval():
    from bank_lib.minebank_core import PENDING_APPROVAL_THRESHOLD
    assert PENDING_APPROVAL_THRESHOLD == 5000


def test_credit_activation_fee():
    from bank_lib.minebank_core import credit_activation_fee
    assert credit_activation_fee(5000) == 10
    assert credit_activation_fee(5001) == 40


def test_pending_threshold():
    from bank_lib.minebank_core import PENDING_APPROVAL_THRESHOLD
    assert PENDING_APPROVAL_THRESHOLD == 5000
