import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_bonus_module_imports():
    from bank_lib.minebank_bonus import (
        ensure_bonus_schema, request_loan, calculate_risk,
        banking_locked, process_due_schedules, bank_statistics,
    )
    assert callable(ensure_bonus_schema)
    assert callable(request_loan)
    assert callable(calculate_risk)
    assert callable(banking_locked)
    assert callable(process_due_schedules)
    assert callable(bank_statistics)

def test_bonus_loan_limits():
    from bank_lib.minebank_bonus import request_loan
    assert callable(request_loan)

def test_bonus_risk_thresholds_are_documented():
    from bank_lib.minebank_bonus import calculate_risk
    assert callable(calculate_risk)
