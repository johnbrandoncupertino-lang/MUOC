"""Central MineBank Privileges policy.

This module is intentionally pure: it does not read the database, mutate accounts,
change financial limits, or grant a reward. Callers must pass authoritative account
fields loaded server-side. Monetary discounts and point multipliers are not activated
here; those require approved product rules and service implementation.
"""

from copy import deepcopy


# Canonical account_tiers_v2 codes. These are the existing six products, not new tiers.
_TIER_POLICIES = {
    "PERSONAL": {
        "account_type": "PERSONAL",
        "service_level": "CORE",
        "mineclubs_access_level": "GENERAL",
        "points_earning_class": "BASE",
    },
    "PERSONAL_PRO": {
        "account_type": "PERSONAL",
        "service_level": "PRO",
        "mineclubs_access_level": "ENHANCED",
        "points_earning_class": "PRO",
    },
    "PERSONAL_PRIVATE": {
        "account_type": "PERSONAL",
        "service_level": "PRIVATE",
        "mineclubs_access_level": "PRIVATE",
        "points_earning_class": "PRIVATE",
    },
    "BUSINESS": {
        "account_type": "BUSINESS",
        "service_level": "CORE",
        "mineclubs_access_level": "GENERAL",
        "points_earning_class": "BASE",
    },
    "BUSINESS_PRO": {
        "account_type": "BUSINESS",
        "service_level": "PRO",
        "mineclubs_access_level": "BUSINESS",
        "points_earning_class": "PRO",
    },
    "CORPORATE": {
        "account_type": "BUSINESS",
        "service_level": "CORPORATE",
        "mineclubs_access_level": "CORPORATE",
        "points_earning_class": "CORPORATE",
    },
}

_ACTIVE_STATUSES = frozenset({"ACTIVE", "ENABLED"})
_INACTIVE_STATUSES = frozenset({"CLOSED", "DISABLED", "FROZEN", "SUSPENDED"})


def canonical_tier_code(tier_code):
    """Normalize a persisted tier code without inferring it from a display label."""
    if not isinstance(tier_code, str):
        return None
    normalized = tier_code.strip().upper()
    return normalized if normalized in _TIER_POLICIES else None


def resolve_privileges(tier_code, account_type, account_status):
    """Return a fail-closed entitlement snapshot for one authoritative account.

    account_type and account_status must come from the server-side account record.
    Unknown tier codes, mismatched account types, and unknown statuses receive no
    access. This metadata does not replace product-specific status, machine capability,
    reward availability, or current financial-limit checks.
    """
    code = canonical_tier_code(tier_code)
    kind = account_type.strip().upper() if isinstance(account_type, str) else None
    status = account_status.strip().upper() if isinstance(account_status, str) else None
    policy = _TIER_POLICIES.get(code) if code else None

    eligible = bool(
        policy
        and kind == policy["account_type"]
        and status in _ACTIVE_STATUSES
        and status not in _INACTIVE_STATUSES
    )

    if not eligible:
        return {
            "eligible": False,
            "tier_code": code,
            "account_type": kind,
            "account_status": status,
            "service_level": "NONE",
            "clubcash_network_eligible": False,
            "mineclubs_access_level": "NONE",
            "minepoints_programme_eligible": False,
            "points_earning_class": "NONE",
            "clubcash_fee_discount_enabled": False,
            "clubcash_limit_adjustment_enabled": False,
            "points_bonus_multiplier_enabled": False,
        }

    return {
        "eligible": True,
        "tier_code": code,
        "account_type": kind,
        "account_status": status,
        **deepcopy(policy),
        "clubcash_network_eligible": True,
        "mineclubs_access_level": policy["mineclubs_access_level"],
        "minepoints_programme_eligible": True,
        # Financial and earning bonuses remain off until separately approved.
        "clubcash_fee_discount_enabled": False,
        "clubcash_limit_adjustment_enabled": False,
        "points_bonus_multiplier_enabled": False,
    }


def known_tier_codes():
    """Return the six supported canonical codes in a stable order."""
    return tuple(_TIER_POLICIES)
