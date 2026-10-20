# MinePoints Rewards — Portal Design and Implementation Plan

**Status:** Product direction approved; numerical earning rates, reward prices, and qualifying-event policy values still require configuration approval.  
**Brand:** Preserve MineBank's existing diamond mark, dark banking surfaces, emerald accents, typography, and “The New Bank” identity.  
**Scope:** Customer-facing portal, server-side points engine, reward catalogue, and owner/admin operations. This document does not activate production earning or redemption.

## 1. Decisions now fixed

1. **Earning source:** MinePoints are earned through eligible banking activity only. No points for logins, page views, referrals, arbitrary promotional grants, or other non-banking activity in the initial release.
2. **Redemption:** The catalogue includes both:
   - **Experiences:** MineClubs visits/events and other explicitly configured member experiences.
   - **Discounts:** approved discounts on specified MineBank service fees.
3. **Tier differentiation:** Each of the six existing account tiers has a configurable earning rate. The rate is determined by the tier of the account involved in the eligible activity, not by the customer's highest tier elsewhere.
4. MinePoints remain a loyalty unit, not Emeralds, cash, a deposit, or a transferable balance. No cash-out, peer-to-peer transfer, interest, CashLine repayment, or loan repayment.
5. No points expiry at launch. Reversals must be recorded as new ledger entries; never delete history.
6. Numerical multipliers, points-per-qualifying-event values, reward prices, and the exact initial qualifying-event allowlist are not set by this decision and must not be guessed or hard-coded as business policy.

## 2. Customer portal information architecture

Keep **Privileges & Rewards** in the Services navigation. It becomes the entry point for MinePoints rather than adding a disconnected loyalty app.

### Page A — MinePoints overview

Desktop layout:
- **Header:** “MinePoints Rewards” with a concise explanation: “Earn points through eligible banking activity. Redeem them for member experiences and selected service-fee discounts.”
- **Primary balance card:** available points, pending points (only if pending awards are implemented), and a “Browse rewards” action. State explicitly: “Points are separate from your Emerald balance and have no cash value.”
- **Earning-rate strip:** the rate attached to each eligible account, labelled by account nickname/number and tier. Never show a single blended rate if the customer owns multiple accounts.
- **Activity panel:** latest awards, redemptions, reversals, and adjustments with date, reason, source transaction reference where safe, signed points delta, and status.
- **Helpful empty state:** if balance is zero, explain how eligible activity earns points and link to the eligible-activity rules. Do not suggest that every transaction earns points.

Mobile layout:
- Balance card first; account-specific rates in a horizontally scrollable or stacked list; activity table becomes compact rows/cards.
- All important details remain available without hover. Provide visible focus states and readable contrast.

### Page B — Rewards catalogue

Use two visible categories/tabs:
- **Experiences**
- **Service discounts**

Each reward card shows:
- Reward title and concise description.
- Points price.
- Eligibility (eligible account tier/type, status requirements, or other explicit rules).
- Availability/status and any usage limits.
- Fulfilment method and material conditions before the customer confirms.

Cards that the customer cannot redeem remain visible only if the reason is useful; they must be clearly disabled and explain why. Do not imply a discount applies to a transaction unless the backend supports that exact fee and checks eligibility at charge time.

### Page C — Reward details and confirmation

Before submission show:
- Reward and fulfilment details.
- Points cost and current available balance.
- Post-redemption balance.
- Any non-points fee or restriction, if applicable.
- A clear confirmation action.

After submission show a durable redemption reference and status: Requested, Processing, Fulfilled, Failed, or Cancelled. A refresh/retry must not create a second redemption or debit points twice.

### Page D — Points activity

Filters: All activity, Earned, Redeemed, Reversed/adjusted; optional date range. Every row shows:
- Timestamp and entry type.
- Signed points amount.
- Reason and qualifying activity reference.
- Related account (masked account number plus tier).
- Status and, for reversals, the original entry reference.

Never expose internal risk scoring, card secrets, Wallet PINs, full sensitive transaction payloads, or another customer's details.

### Page E — Earning rules

A plain-language rules page should answer:
- Which activity types currently qualify.
- Which statuses must be reached before points are awarded.
- How the tier-specific rate is applied.
- What happens if a qualifying transaction is reversed or refunded.
- Explicit exclusions (including own-account transfers and failed/declined transactions unless later explicitly approved).
- That points do not expire at launch, have no cash value, and cannot be transferred or used to repay credit.

Only publish rules that match the active server configuration. If rules are not configured, display “Earning is not active yet” instead of implying awards are live.

## 3. Tier-specific earning model

Use the six canonical codes already stored in `account_tiers_v2`. Do not infer tiers from display names and do not create a competing tier source of truth.

| Existing tier | Earning rate |
|---|---|
| Personal | Configurable base rate |
| Personal Pro | Configurable rate, distinct from Personal |
| Personal Private | Configurable rate, distinct from the other personal tiers |
| Business | Configurable base business rate |
| Business Pro | Configurable rate, distinct from Business |
| Corporate | Configurable rate, distinct from the other business tiers |

The table intentionally does not assign numeric values. Before launch, the owner must set and approve a value for every tier. Validation must prevent missing, negative, malformed, or ambiguous rates. Whether a “rate” means points per Emerald spent, points per eligible transaction, or a tier multiplier over a base rate must be selected explicitly in the rules configuration before production awards are enabled.

**Account-level rule:** If a customer has more than one account, use the tier and account that generated the qualifying event. Never use a Personal Private tier to boost points earned by a separate Personal or Business account.

## 4. Eligible banking activity and anti-abuse controls

Implement an explicit server-side allowlist. Do not award points merely because a transaction exists in the ledger.

Initial exclusions:
- Failed, rejected, pending, cancelled, or reversed transactions.
- Transfers between a customer's own accounts.
- CashLine draws, CashLine repayments, loan disbursements, loan repayments, interest, and fees, unless separately approved in a future rule revision.
- Administrative mint/burn entries and manual balance corrections.
- Duplicate event deliveries and repeated API requests.

For every eligible event:
1. Resolve the authoritative account, account owner/member relationship, current account status, canonical tier code, and final transaction status from the database.
2. Evaluate the active earning rule server-side.
3. Generate a deterministic idempotency key from the qualifying source event and rule version.
4. Append one positive ledger entry with source type/ID, related account, reason, rule version, and timestamp.
5. On reversal/refund of a qualifying source event, append a linked reversal entry. Do not delete the original award. If the customer has already spent the points, record the reversal/debt policy explicitly rather than silently allowing a negative balance.
6. Make processing safe under concurrent retries and repeated webhooks/jobs.

The exact qualifying event allowlist is a launch gate and must be documented and tested before any live awards begin.

## 5. Redemption and discount safety

- Check reward status, points balance, eligibility, account/customer status, and reward availability on the server at redemption time.
- Use a database transaction to create the redemption, debit points once, and store the ledger link.
- Enforce idempotency at the database level; client-side button disabling is only a usability aid.
- A failed fulfilment must be retriable/reconcilable without duplicate debit.
- If a redemption is cancelled and points are returned, append a compensating ledger entry linked to the original redemption; do not rewrite history.
- **Service-fee discounts:** discount redemption and application to a fee must be one atomic or otherwise recoverable workflow. The same discount must not be applied twice. A reward must name the eligible fee type and any cap/validity window.
- **MineClubs experiences:** define whether redemption grants a voucher, a reservation, or admission. A points voucher must not bypass current club status, identity verification, access policy, or entrance idempotency. Do not deduct Emeralds for an admission that the reward explicitly covers.
- Points must never be treated as money in the bank ledger.

## 6. Owner/admin management

Add a restricted **MinePoints Management** area for the bank-owner ADMIN account, with separate permissions from the customer rewards page.

Owner capabilities:
- Configure and version the earning rule for each canonical tier.
- Activate/pause individual eligible event types.
- Create/edit/pause/retire reward catalogue items and set points prices.
- Define eligibility and fulfilment instructions.
- Review redemption queue and mark fulfilment states with an audit trail.
- Inspect award/reversal history and idempotency conflicts.
- Make exceptional point adjustments only with a mandatory reason, actor, and audit event; adjustments must be ledger entries, not direct balance edits.

No reward or earning rule should become active by accident when a draft is saved. Activation should require a clear confirmation, validation summary, and immutable rule version. Customer pages must not expose owner-only controls.

## 7. Persistence and service architecture

The existing `extras/minepoints.sql` foundation provides:
- `minepoints_ledger`: signed, idempotent points history.
- `minepoints_rewards`: catalogue with draft/active/paused/retired statuses.
- `minepoints_redemptions`: redemption and fulfilment lifecycle.

Before implementing live earning:
1. Review the current ledger helper and its transaction/locking behavior.
2. Add a reviewed, explicit migration for tier earning rules and qualifying-event configuration/versioning if no existing suitable table exists.
3. Keep the ledger append-only; derive available balance from ledger entries or use a reconciled cache only if required by measured performance.
4. Add service functions in a dedicated MinePoints module rather than embedding loyalty writes in the core Emerald ledger.
5. Keep optional MinePoints reads fail-soft: missing or disabled MinePoints must not break login, transfers, account balances, CashLine, loans, or statements.
6. Never run schema DDL automatically on normal portal requests.

## 8. Delivery sequence

### Stage 1 — UX and read-only portal
- Build overview, account-specific earning-rate display, activity list, catalogue, reward details, and rules/empty states.
- Connect only to existing MinePoints tables.
- Seed no active rewards and show earning as inactive until rules are configured.
- Validate responsive layout, keyboard navigation, contrast, and graceful behavior when tables are unavailable.

### Stage 2 — Rule configuration and eligible-event engine
- Add tier rates and explicit eligible-event allowlist with versioning.
- Add deterministic award processing, idempotency, reversal handling, and audit logs.
- Write unit and PostgreSQL integration tests for every eligible and excluded event.
- Keep feature flags off until rates and event types are explicitly approved.

### Stage 3 — Owner management
- Add owner-only earning rules, reward catalogue, and fulfilment queue.
- Enforce server-side role checks on every route and mutation, plus CSRF protection and audit logging.
- Test that non-admin users cannot reach management endpoints even by direct URL.

### Stage 4 — Redemptions and fulfilment
- Enable transactional redemption and compensating entries.
- Implement the specific service-fee discount and MineClubs experience fulfilment adapters.
- Test retries, double submissions, concurrency, insufficient points, cancellations, and interrupted fulfilment.

### Stage 5 — Staged release
- Validate on a staging database using the explicit migration.
- Use feature flags for read-only preview, earning, and redemption independently.
- Run acceptance tests and reconcile ledger totals before enabling production.
- Do not claim production activation until a live smoke test verifies awards and redemptions end to end.

## 9. Acceptance criteria

- Each account displays the rate derived from its own canonical tier.
- Qualifying completed activity creates at most one award, even with retries or concurrency.
- Excluded activity creates no award.
- A reversal creates a linked compensating entry and preserves history.
- Points never modify Emerald balances, CashLine, or loan balances.
- A redemption cannot overdraw the points balance or debit twice.
- Reward fulfilment can recover after a timeout without duplicate debit.
- Discount rewards are applied only to their configured fee type and only once.
- Customers see accurate status, prices, eligibility, and a durable reference.
- Only the bank-owner ADMIN can access or mutate owner management.
- Missing optional MinePoints data never breaks core banking.
- No earning/redemption feature is enabled before tier rates, eligible event types, reward prices, and fulfilment policy are approved.
