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
