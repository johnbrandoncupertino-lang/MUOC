from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_application_imports_and_core_routes_exist():
    import app

    routes = {rule.rule for rule in app.app.url_map.iter_rules()}
    expected = {
        "/",
        "/login",
        "/portal/login",
        "/portal/register",
        "/portal",
        "/portal/transfer",
        "/portal/transactions",
        "/portal/statements",
        "/api/get/health",
        "/api/v1/accounts",
    }
    assert expected.issubset(routes)


def test_minebank_schema_files_exist():
    assert (ROOT / "extras" / "minebank_v2.sql").is_file()
    assert (ROOT / "bank_lib" / "minebank_schema.py").is_file()
    assert (ROOT / "bank_lib" / "database.py").is_file()


def test_minebank_registration_template_has_required_fields():
    template = (ROOT / "templates" / "minebank_register_new.html").read_text(encoding="utf-8")
    assert 'name="email"' in template
    assert 'name="password"' in template
    assert 'name="password_confirmation"' in template
    assert 'name="wallet_pin"' in template


def test_runtime_database_migration_helper_exists():
    source = (ROOT / "bank_lib" / "database.py").read_text(encoding="utf-8")
    assert "def ensure_minebank_schema()" in source
    assert "init_minebank_v2()" in source
    assert "def get_db_connection()" in source


def test_minebank_core_features_exist():
    requests_source = (ROOT / "bank_lib" / "minebank_requests.py").read_text(encoding="utf-8")
    core_source = (ROOT / "bank_lib" / "minebank_core.py").read_text(encoding="utf-8")
    auth_source = (ROOT / "bank_lib" / "minebank_auth.py").read_text(encoding="utf-8")

    assert "def create_request(" in requests_source
    assert "def list_requests(" in requests_source
    assert "def transfer(" in core_source
    assert "def deposit(" in core_source
    assert "def draw_credit(" in core_source
    assert "def verify_wallet_pin(" in auth_source


def test_money_rules_remain_integer_and_approval_threshold_is_5000():
    from bank_lib.minebank_core import PENDING_APPROVAL_THRESHOLD, credit_activation_fee

    assert PENDING_APPROVAL_THRESHOLD == 5000
    assert credit_activation_fee(5000) == 10
    assert credit_activation_fee(5001) == 40


def test_security_basics_are_present():
    source = (ROOT / "bank_lib" / "minebank_auth.py").read_text(encoding="utf-8")
    assert "WALLET_PIN_MAX_ATTEMPTS = 3" in source
    assert "WALLET_PIN_LOCKOUT_MINUTES = 5" in source
