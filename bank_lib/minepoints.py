"""MinePoints read models and controlled earning primitives.

The points ledger is separate from the Emerald banking ledger. Live awards must
only be invoked by trusted server-side code after it verifies the source event.
"""
from decimal import Decimal, ROUND_FLOOR
import hashlib

from .database import execute_query, execute_query_dict


def points_balance(client_id):
    rows = execute_query(
        "SELECT COALESCE(SUM(points_delta),0) FROM minepoints_ledger WHERE client_id=%s",
        (client_id,),
    )
    return int(rows[0][0] or 0) if rows else 0


def customer_summary(client_id):
    """Return points, history, active rewards, and account-specific tier rates."""
    balance = points_balance(client_id)
    history = execute_query_dict(
        """SELECT id,entry_type,points_delta,reason,source_type,source_id,
                  related_account_id,created_at
           FROM minepoints_ledger WHERE client_id=%s
           ORDER BY created_at DESC,id DESC LIMIT 50""",
        (client_id,),
    )
    rewards = execute_query_dict(
        """SELECT id,code,title,description,reward_type,points_cost,
                  eligibility_rules,fulfilment_instructions
           FROM minepoints_rewards WHERE status='ACTIVE'
           ORDER BY points_cost,id LIMIT 100"""
    )
    accounts = execute_query_dict(
        """SELECT a.id,a.account_number,a.account_type,a.status,t.code AS tier_code,t.display_name AS tier_name
           FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
           WHERE a.client_id=%s AND a.status<>'CLOSED' ORDER BY a.id""",
        (client_id,),
    )
    rates = execute_query_dict(
        "SELECT tier_code,points_per_100_emeralds,enabled,rule_version FROM minepoints_tier_rates"
    )
    rate_by_tier = {str(row["tier_code"]): row for row in rates}
    events = execute_query_dict(
        """SELECT event_code,label,enabled FROM minepoints_eligible_events
           ORDER BY event_group,event_code"""
    )
    events_by_code = {str(row["event_code"]): row for row in events}
    # Only completed external outgoing transfers currently have a live award hook.
    # Purchase and fee rules may be configured by admins, but are not live earning
    # sources until their product integrations are implemented and tested.
    transfer_event = events_by_code.get("EXTERNAL_TRANSFER_OUT")
    transfer_enabled = bool(transfer_event and transfer_event["enabled"])
    for account in accounts:
        rate = rate_by_tier.get(str(account["tier_code"]))
        account["earning_rule"] = rate
        rate_enabled = bool(rate and rate["enabled"] and rate["points_per_100_emeralds"] is not None
                            and int(rate["points_per_100_emeralds"]) > 0)
        account_enabled = str(account["status"]).upper() == "ENABLED"
        account["points_eligible"] = bool(account_enabled and rate_enabled and transfer_enabled)
        if not account_enabled:
            account["eligibility_status"] = "Account unavailable"
            account["eligibility_detail"] = "This account is not enabled for transactions, so it cannot currently earn points."
        elif not rate_enabled:
            account["eligibility_status"] = "Rate disabled"
            account["eligibility_detail"] = "MineBank has not enabled a points rate for this account tier."
        elif transfer_enabled:
            account["eligibility_status"] = "Eligible"
            account["eligibility_detail"] = "Completed external outgoing transfers paid from Balance may qualify. Purchases and fee-paying services are not yet connected."
        else:
            account["eligibility_status"] = "Not currently earning"
            account["eligibility_detail"] = "The account rate is configured, but the live external-transfer earning rule is disabled."
    program_status = "Eligible" if any(a["points_eligible"] for a in accounts) else "Not currently earning"
    return {"balance": balance, "history": history, "rewards": rewards, "accounts": accounts,
            "program_status": program_status}


def owner_configuration():
    tiers = execute_query_dict(
        "SELECT code,display_name,account_type,active FROM account_tiers_v2 ORDER BY account_type,code"
    )
    rates = execute_query_dict(
        "SELECT tier_code,points_per_100_emeralds,enabled,rule_version,updated_at FROM minepoints_tier_rates"
    )
    events = execute_query_dict(
        """SELECT event_code,label,event_group,enabled,requires_explicit_approval,rule_version,updated_at
           FROM minepoints_eligible_events ORDER BY event_group,event_code"""
    )
    return tiers, {str(row["tier_code"]): row for row in rates}, events


def award_points_for_verified_event(client_id, account_id, tier_code, event_code,
                                    source_id, eligible_amount, rule_version="v1"):
    """Award once for a source event already authoritatively verified by caller.

    This helper deliberately does not discover or trust client-provided event
    details. It is for server-side transaction integration only. Amounts are
    rounded down to whole 100-Emerald units; zero-point awards are skipped.
    The source ID uniqueness constraint prevents the same event being awarded
    under a second request.
    """
    amount = Decimal(str(eligible_amount))
    if amount <= 0 or not source_id or len(str(source_id)) > 128:
        return 0
    event = execute_query_dict(
        """SELECT event_code,enabled,requires_explicit_approval,rule_version
           FROM minepoints_eligible_events WHERE event_code=%s""",
        (event_code,),
    )
    if not event or not event[0]["enabled"]:
        return 0
    rate = execute_query_dict(
        """SELECT points_per_100_emeralds,enabled,rule_version FROM minepoints_tier_rates
           WHERE tier_code=%s""",
        (tier_code,),
    )
    if not rate or not rate[0]["enabled"] or rate[0]["points_per_100_emeralds"] is None:
        return 0
    points = int((amount * Decimal(int(rate[0]["points_per_100_emeralds"])) / Decimal(100))
                 .to_integral_value(rounding=ROUND_FLOOR))
    if points <= 0:
        return 0
    source_key = f"{event_code}:{source_id}"
    idem = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    source_key = f"{event_code}:{source_id}"
    idem = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    rows = execute_query_dict(
        """WITH inserted_source AS (
             INSERT INTO minepoints_award_sources
               (client_id,account_id,event_code,source_id,eligible_amount,tier_code,
                rule_version,points_awarded)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT(event_code,source_id) DO NOTHING
             RETURNING client_id
           ), inserted_ledger AS (
             INSERT INTO minepoints_ledger
               (client_id,entry_type,points_delta,idempotency_key,reason,source_type,
                source_id,related_account_id,rule_version)
             SELECT %s,'AWARD',%s,%s,%s,%s,%s,%s,%s FROM inserted_source
             ON CONFLICT(client_id,idempotency_key) DO NOTHING
             RETURNING id
           )
           SELECT id FROM inserted_ledger""",
        (client_id, account_id, event_code, str(source_id), amount, tier_code,
         f"{rule_version}|{rate[0]['rule_version']}", points,
         client_id, points, "mp-award-" + idem[:110],
         f"Eligible activity: {event_code}", event_code, str(source_id),
         account_id, f"{rule_version}|{rate[0]['rule_version']}"),
        commit=True,
    )
    if not rows:
        return 0
    return points


def award_completed_external_transfer(transaction_id, client_id):
    """Verify a completed transfer from the authoritative ledger, then award once."""
    rows = execute_query_dict(
        """SELECT l.transaction_id,l.amount,l.status,l.transaction_type,l.transfer_kind,
                  s.id AS account_id,t.code AS tier_code,s.client_id,
                  r.client_id AS recipient_client_id
           FROM ledger_transactions l
           JOIN bank_accounts s ON s.id=l.sender_account_id
           JOIN account_tiers_v2 t ON t.id=s.tier_id
           LEFT JOIN bank_accounts r ON r.id=l.recipient_account_id
           WHERE l.transaction_id=%s AND s.client_id=%s
             AND l.transaction_type='TRANSFER' AND l.status='COMPLETED'
             AND COALESCE(l.transfer_kind,'BALANCE') NOT IN ('CASHLINE','DYNAMIC_CASHLINE')
             AND l.recipient_account_id IS NOT NULL
             AND (r.client_id IS NULL OR r.client_id<>s.client_id)
           LIMIT 1""",
        (str(transaction_id), client_id),
    )
    if not rows:
        return 0
    row = rows[0]
    return award_points_for_verified_event(
        client_id=int(row["client_id"]),
        account_id=int(row["account_id"]),
        tier_code=str(row["tier_code"]),
        event_code="EXTERNAL_TRANSFER_OUT",
        source_id=str(row["transaction_id"]),
        eligible_amount=row["amount"],
        rule_version="transfer-v1",
    )


def award_completed_external_transfer_by_id(transaction_id):
    """Resolve the source owner from the ledger before processing an approval."""
    rows = execute_query_dict(
        """SELECT s.client_id FROM ledger_transactions l
           JOIN bank_accounts s ON s.id=l.sender_account_id
           WHERE l.transaction_id=%s AND l.status='COMPLETED'
             AND l.transaction_type='TRANSFER'""",
        (str(transaction_id),),
    )
    if not rows:
        return 0
    return award_completed_external_transfer(transaction_id, int(rows[0]["client_id"]))
