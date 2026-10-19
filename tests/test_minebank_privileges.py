import pytest

from bank_lib.minebank_privileges import (
    canonical_tier_code,
    known_tier_codes,
    resolve_privileges,
)


EXPECTED_TIERS = {
    "PERSONAL": ("PERSONAL", "CORE", "GENERAL", "BASE"),
    "PERSONAL_PRO": ("PERSONAL", "PRO", "ENHANCED", "PRO"),
    "PERSONAL_PRIVATE": ("PERSONAL", "PRIVATE", "PRIVATE", "PRIVATE"),
    "BUSINESS": ("BUSINESS", "CORE", "GENERAL", "BASE"),
    "BUSINESS_PRO": ("BUSINESS", "PRO", "BUSINESS", "PRO"),
    "CORPORATE": ("BUSINESS", "CORPORATE", "CORPORATE", "CORPORATE"),
}


def test_privileges_support_exactly_the_six_existing_tiers():
    assert set(known_tier_codes()) == set(EXPECTED_TIERS)


@pytest.mark.parametrize(
    "tier_code,expected",
    EXPECTED_TIERS.items(),
)
def test_active_account_resolves_expected_tier_policy(tier_code, expected):
    account_type, service_level, club_level, points_class = expected
    result = resolve_privileges(tier_code, account_type, "ENABLED")

    assert result["eligible"] is True
    assert result["tier_code"] == tier_code
    assert result["service_level"] == service_level
    assert result["mineclubs_access_level"] == club_level
    assert result["points_earning_class"] == points_class
    assert result["clubcash_network_eligible"] is True
    assert result["minepoints_programme_eligible"] is True


@pytest.mark.parametrize("status", ["FROZEN", "DISABLED", "CLOSED", "SUSPENDED", "PENDING", "", None])
def test_inactive_or_unknown_account_status_fails_closed(status):
    result = resolve_privileges("PERSONAL_PRIVATE", "PERSONAL", status)

    assert result["eligible"] is False
    assert result["clubcash_network_eligible"] is False
    assert result["mineclubs_access_level"] == "NONE"
    assert result["minepoints_programme_eligible"] is False


@pytest.mark.parametrize(
    "tier_code,account_type",
    [
        ("PERSONAL", "BUSINESS"),
        ("PERSONAL_PRO", "BUSINESS"),
        ("BUSINESS", "PERSONAL"),
        ("CORPORATE", "PERSONAL"),
    ],
)
def test_account_type_mismatch_never_grants_privileges(tier_code, account_type):
    result = resolve_privileges(tier_code, account_type, "ACTIVE")
    assert result["eligible"] is False
    assert result["service_level"] == "NONE"


@pytest.mark.parametrize("tier_code", [None, "", "UNKNOWN", "Personal Private", "PERSONAL_PREMIUM"])
def test_unknown_or_display_name_tier_does_not_grant_privileges(tier_code):
    result = resolve_privileges(tier_code, "PERSONAL", "ENABLED")
    assert result["eligible"] is False
    assert result["tier_code"] is None
    assert result["mineclubs_access_level"] == "NONE"


def test_tier_code_normalization_does_not_infer_from_display_names():
    assert canonical_tier_code(" personal_pro ") == "PERSONAL_PRO"
    assert canonical_tier_code("Personal Pro") is None


def test_foundation_does_not_activate_unapproved_financial_benefits():
    for tier_code, (account_type, _, _, _) in EXPECTED_TIERS.items():
        result = resolve_privileges(tier_code, account_type, "ACTIVE")
        assert result["clubcash_fee_discount_enabled"] is False
        assert result["clubcash_limit_adjustment_enabled"] is False
        assert result["points_bonus_multiplier_enabled"] is False
