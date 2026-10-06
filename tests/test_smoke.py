import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_application_imports_and_core_routes_exist():
    import app

    routes = {rule.rule for rule in app.app.url_map.iter_rules()}
    assert "/" in routes
    assert "/login" in routes
    assert "/api/setup" in routes
    assert "/api/transfer/bank" in routes


def test_schema_has_no_trailing_comma_before_closing_parenthesis():
    schema = (ROOT / "extras" / "schema.sql").read_text(encoding="utf-8")
    assert ",\n);" not in schema


def test_setup_uses_canonical_initializer():
    setup = (ROOT / "api" / "setup.py").read_text(encoding="utf-8")
    assert "init_db()" in setup
    assert "admin_password" in setup
    assert "VALUES (%s, %s, %s)" in setup


def test_minebank_registration_route_exists():
    import app

    routes = {rule.rule for rule in app.app.url_map.iter_rules()}
    assert "/portal/register" in routes
    assert "/portal/login" in routes


def test_minebank_registration_template_has_required_fields():
    template = (ROOT / "templates" / "minebank_register.html").read_text(encoding="utf-8")
    assert 'name="email"' in template
    assert 'name="password"' in template
    assert 'name="password_confirmation"' in template
    assert "minebank_register" in template


def test_runtime_minebank_schema_migration_helper_exists():
    source = (ROOT / "bank_lib" / "database.py").read_text(encoding="utf-8")
    assert "def ensure_minebank_schema()" in source
    assert "init_minebank_v2()" in source


def test_minebank_full_wallet_routes_exist():
    import app

    routes = {rule.rule for rule in app.app.url_map.iter_rules()}
    expected = {
        "/portal/statements",
        "/portal/statements/print",
        "/portal/statements/csv",
        "/portal/requests",
        "/portal/profile",
        "/portal/notifications",
        "/portal/plans",
        "/portal/transactions/<transaction_id>",
        "/portal/transactions/<transaction_id>/refund",
    }
    assert expected.issubset(routes)


def test_minebank_request_helper_and_credit_draw_exist():
    requests_source = (ROOT / "bank_lib" / "minebank_requests.py").read_text(encoding="utf-8")
    core_source = (ROOT / "bank_lib" / "minebank_core.py").read_text(encoding="utf-8")
    assert "def create_request(" in requests_source
    assert "def list_requests(" in requests_source
    assert "def draw_credit(" in core_source
