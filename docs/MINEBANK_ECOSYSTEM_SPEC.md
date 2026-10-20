# MineBank Ecosystem Specification

**Status:** Proposed product specification and implementation plan  
**Repository:** `johnbrandoncupertino-lang/MUOC`  
**Brand constraint:** Preserve MineBank's existing diamond mark, emerald palette, dark surfaces, typography, and "The New Bank" identity. This is a services expansion, not a visual rebrand.

## 1. Product principles

1. MineBank remains the authoritative source for account identity, account tier, balances, fees, limits, transactions, and customer status.
2. The six existing tiers remain unchanged: Personal, Personal Pro, Personal Private, Business, Business Pro, and Corporate. The implementation must use the tier codes already stored in `account_tiers_v2`, not create a competing tier table or infer entitlements from display labels.
3. Entitlements are evaluated server-side. Templates, browser JavaScript, ATM Lua programs, and Minecraft terminals may display an entitlement but cannot grant it.
4. New optional services must fail closed for actions and fail gracefully for nonessential display data. A missing rewards table must not break login, transfers, withdrawals, or the existing dashboard.
5. Every points award, reversal, expiry (if introduced later), and redemption must be ledgered and idempotent. A balance must be derived from or reconciled against ledger entries.
6. Do not silently change existing banking fees, credit eligibility, CashLine limits, account limits, loan terms, or transaction authorisation rules as part of this programme.
7. No production database schema changes are to run implicitly during an ordinary web request. Use a reviewed, versioned SQL migration and an explicit deployment procedure.

## 2. Brand and service definitions

### MineBank ClubCash

The official branded ATM network, presented as **MineBank ClubCash** (short form: **ClubCash**). It includes physical CC:Tweaked ATM installations and their customer-facing software.

- Core deposit, withdrawal, balance, and recovery behaviour remains available according to current product rules and machine capability.
- CashLine Advance is a separate ATM action: it creates a Standard CashLine credit draw and dispenses physical Emeralds; it must never debit the ordinary balance or silently fall back to it. Require an active Standard CashLine facility, successful CrediCheck/product access, available credit, customer PIN verification, system/account checks, an idempotency key, and the existing dispense journal/recovery flow. Dynamic CashLine is not eligible for this ATM advance in the initial release.
- Tier-based fee discounts, enhanced allowances, or other ATM privileges are configuration-driven and must be checked server-side before charging or authorising an operation.
- An ATM's pairing credential identifies the machine; a customer's card/account credential identifies the customer. These identities must not be conflated.
- ATM journal/idempotency/recovery controls remain mandatory. A cosmetic tier label must never affect physical cash accounting.
- A future partner-ATM network is out of scope until another institution and settlement model exist.

### MineClubs

Physical member lounges and private service spaces inside the Minecraft world. They are places and access entitlements, not bank accounts.

- A club location has a stable ID, name, world/location description, published facilities, operating status, and access policy.
- Access checks use a server-side account/member credential and current Privileges result; never trust a client-supplied tier.
- Admission price per visit: Personal and Business pay 50 Emeralds; Personal Pro and Business Pro pay 25 Emeralds; Personal Private and Corporate pay no per-visit admission fee (unlimited visits).
- Charge once per admission, not per interaction. Verify and debit server-side, and use an idempotent admission record so retry/recovery cannot double-charge. If payment succeeds but the terminal disconnects, resume the same admission without charging again. The account used for admission determines eligibility; benefits do not carry across a customer's separate accounts.
- Possible facilities: member reception, account service terminal, quiet/private rooms, business networking, and tier-specific events.
- The first release should provide the data model and entitlement checks before any Minecraft building automation. Physical builds and their coordinates are operator-managed content.
- MineClubs access does not imply access to another player's account, private messages, or business data.

### MinePoints Rewards

A loyalty points programme. MinePoints are **not Emeralds, a deposit, an account balance, a transferable asset, or a credit facility**.

- Customers earn points only from explicitly eligible events/rules (initially configurable promotional awards and approved qualifying activity).
- Points can be redeemed against a defined catalogue of rewards such as eligible service-fee discounts, MineClub experiences, and exclusive offers.
- No cash-out, peer-to-peer transfer, interest, overdraft, CashLine repayment, or loan repayment using points in the initial release.
- No implied Emerald conversion rate. Each reward defines its own points price and eligibility.
- Keep an append-only ledger with unique idempotency keys, customer ID, entry type, signed points delta, reason/source, related account/transaction where applicable, rule version, actor, and timestamp.
- Redemption must be transactional: validate eligibility and sufficient points, create a unique redemption, debit points once, and record the fulfilment state. A failed fulfilment must be recoverable without double-debiting.
- Reverse qualifying awards when their underlying transaction is reversed/refunded, using a linked reversal entry; do not delete history.
- Initial release: no points expiry. If expiry is ever added, disclose it before launch and ledger expiry as explicit entries.
- Award rules must be conservative and prevent farming through self-transfers, repeated reversals, failed transactions, or circular activity. Transfers between the customer's own accounts are not eligible activity by default.
- Show available points, pending/reversed entries where applicable, history, reward catalogue, and clear terms.

### MineBank Privileges

The central, server-enforced entitlement framework that maps an account's actual tier and status to capabilities across the portal, ClubCash, and MineClubs.

- Store entitlement definitions/rules in one module or controlled configuration rather than duplicating tier checks across templates and Lua programs.
- Provide a predictable server API: resolve the canonical tier code; return entitlement decisions and safe display metadata; check an action with context where required.
- Fail closed for restricted actions if tier/status data is unavailable. Unknown tier codes receive no Premium-only entitlements.
- Entitlements may vary by account tier, account type, account status, product status, and feature rollout. Do not assume a customer's personal account grants privileges to their business account.
- Administrative overrides, if ever supported, require an explicit audit record and expiry/review semantics; no hidden client-side overrides.

## 3. Tier framework

Keep the existing six account tiers and their existing financial parameters:

| Account type | Existing tier | Position in proposed service programme |
|---|---|---|
| Personal | Personal | Core member |
| Personal | Personal Pro | Enhanced member |
| Personal | Personal Private | Private member |
| Business | Business | Core business member |
| Business | Business Pro | Enhanced business member |
| Business | Corporate | Corporate member |

The words "Core", "Enhanced", "Private", and "Corporate" in this table are service-programme groupings, not replacement account tiers. The code must use the actual `account_tiers_v2.code` values already in production.

### Proposed benefits (subject to product-owner approval)

| Capability | Personal / Business | Pro tiers | Personal Private / Corporate |
|---|---|---|---|
| ClubCash | Standard machine access | Candidate fee/allowance benefits | Candidate enhanced fee/allowance benefits |
| MineClubs | Eligible general club access, subject to configured policy | Additional club/event access | Private lounge access and priority experiences |
| MinePoints | Base earning rules | Configured earning multiplier/bonus | Highest configured multiplier and selected exclusive rewards |
| Portal insights | Existing banking dashboard | Enhanced reports and activity insights | More comprehensive reporting and service overview |
| Offers | General catalogue | Pro-eligible offers | Private/corporate catalogue items |

MineClubs admission pricing is approved and fixed: Personal and Business 50 Emeralds per admission; Personal Pro and Business Pro 25 Emeralds per admission; Personal Private and Corporate unlimited admissions at no per-visit fee. Unlimited admissions do not waive unrelated fees. The points programme remains a loyalty currency redeemable for approved discounts and exclusive benefits, but the catalogue, award rules, point prices, and earning multipliers remain unapproved and must not be invented in code. No other financial discounts or limit changes are approved.

Business benefits must be business-relevant. Corporate access does not imply Personal Private access unless the entitlement rule explicitly says so.

## 4. Data and integration dependencies

### Existing repository components verified on the current `main` branch

- `bank_lib/database.py`: PostgreSQL connection/query helpers and schema helpers.
- `bank_lib/minebank_auth.py`: account creation, account-tier lookup, and tier-change requests. Accounts join `bank_accounts.tier_id` to `account_tiers_v2`.
- `bank_lib/minebank_core.py`: authoritative Emerald ledger and transfer/deposit/withdrawal/CashLine logic. Do not embed loyalty accounting in this core ledger.
- `app.py`: Flask routes and dashboard assembly.
- `templates/minebank_layout.html` and `templates/minebank_portal.html`: shared navigation and customer portal.
- `bank_lib/minebank_minecraft.py`, `extras/minebank_minecraft.sql`, and `minecraft/cc_tweaked/`: feature-flagged Minecraft API, card/ATM/merchant integration, and Lua clients.
- `bank_lib/minebank_bonus.py`: optional bonus-suite schema/routes. Review its registration and schema conventions before integrating; do not overload it with unrelated core financial ledger logic without clear separation.

### New persistence (planned, via reviewed migration)

1. `minebank_points_ledger`: append-only signed points entries with idempotency key and references.
2. `minebank_points_redemptions`: redemption request, catalogue item/rule version, points cost, status, and fulfilment audit.
3. `minebank_rewards_catalogue`: approved reward definitions, price, active dates, eligibility policy, and fulfilment type.
4. `minebank_clubs`: operator-managed lounge metadata and published facilities.
5. Optional `minebank_club_access_events`: access attempts and grants for operational audit, avoiding sensitive data beyond what's necessary.
6. Privilege rules should initially be code/configuration with tests, unless operational requirements prove database-managed rules are needed. Do not create a second source of truth for account tiers.

All tables should have appropriate foreign keys/indexes, uniqueness for idempotency, bounded statuses, and least-privilege access. Migration must be repeatable and documented; schema readiness must not cause a production request to run DDL.

### API and UI integration

- Portal: new MinePoints Rewards page, MineClubs directory/member status, and a Privileges summary; consistent navigation under Services or Membership.
- Dashboard: show points and relevant benefits only when data is available; optional-service failures must not break core banking.
- Admin: controlled reward catalogue management, point adjustment/reversal with reason and audit trail, redemption fulfilment, and club directory management. Any manual points adjustment must be explicit and traceable.
- Minecraft API: expose only authenticated, customer-scoped point summary/redemption and club access verification endpoints after schema and authentication review. Do not put customer credentials or privileged point-writing logic in client-side Lua.
- ClubCash: display branded network identity and server-confirmed entitlement information; preserve per-machine credentials and operation reconciliation.
- Public/institutional site: describe the programme without implying benefits are available before their backend is active.

## 5. Security, accounting, and operational requirements

- All reward writes are server-side, customer-scoped, transactional, and idempotent.
- Enforce CSRF protection on browser mutations and authentication/authorisation on APIs.
- Do not accept customer ID, tier, points balance, multiplier, discount, or reward price as authoritative client input.
- Redemption checks the current account status and eligibility at commit time.
- Transaction reversals cannot produce unlimited points by retrying or repeatedly reversing/reposting the same source.
- Use integer points, reject non-positive prices and invalid amounts, and prevent negative available points unless an explicitly approved debt policy is later designed (none in the initial release).
- Keep audit entries for administrator adjustments, catalogue changes, fulfilment, access grants, and failed restricted attempts.
- Ensure every optional service degrades independently. Existing transfers, withdrawals, deposits, loan repayments, and login must continue when the rewards/club service is unavailable.
- Do not store raw card tokens or Wallet PINs in points or lounge records.
- Public copy must state that MinePoints are loyalty points and have no cash value unless the product owner later explicitly changes the model.

## 6. Coordinated implementation stages

### Stage 0 — Specification and inventory
- This document records the intended product boundaries and dependencies.
- Verify exact live tier codes, route registration, current database migration conventions, tests, and UI navigation before schema work.
- Record decisions that remain unapproved: earning rules, multipliers, reward prices, ClubCash fee benefits, lounge locations, and launch eligibility.

### Stage 1 — Privileges foundation
- Implement a pure, centrally tested entitlement resolver keyed to canonical tier codes.
- Add tests for all six tiers, personal/business separation, unknown tiers, inactive/closed/frozen account status, and fail-closed behaviour.
- No new customer-facing financial benefits until the resolver is integrated with authoritative account data.
- Keep the current visual style unchanged.

### Stage 2 — MinePoints ledger and catalogue
- Add reviewed SQL migration, transactional award/reversal/redeem services, idempotency, and tests.
- Start with no cash conversion, no expiry, no transferability, and no automatic points on generic transfers.
- Add catalogue administration and customer-facing history/rewards only after accounting tests pass.

### Stage 3 — MineClubs
- Add club metadata, tier-aware server-side access checks, and audit events.
- Publish physical Minecraft lounge locations only when operator data exists.
- Integrate access terminals only after API tests and credential boundaries are verified.

### Stage 4 — ClubCash and Minecraft integration
- Apply the brand consistently and surface entitlement metadata.
- Add the MineBank ClubCash branding and a dedicated Standard CashLine Advance action. Reuse the existing ATM dispense journal and operation-status recovery, but record the banking transaction as a CashLine credit draw; never debit the ordinary balance. Require an active Standard CashLine facility, CrediCheck/product eligibility, sufficient unused limit, system/account authorisation, and idempotency. Dynamic CashLine advances remain disabled.
- Test operation-status/recovery behaviour and customer tier boundaries.

### Stage 5 — Portal, admin, and institutional website
- Add shared navigation and polished service pages using existing components and styles.
- Provide graceful empty states before customers have points or clubs are configured.
- Avoid changing unrelated layouts or banking workflows.

### Stage 6 — Verification and rollout
- Run focused unit tests, route/template checks, migration checks against a test database, and the full available test suite.
- Test tier upgrades/downgrades, reversals, repeated requests, insufficient points, API auth, closed/frozen accounts, optional schema unavailable, and interrupted redemption fulfilment.
- Use feature flags for incremental exposure. Do not enable customer-facing earning/redemption until the migration and recovery tests pass.
- Report exactly which tests ran and which behaviours still require a live Vercel/PostgreSQL or CC:Tweaked test environment.

## 7. Acceptance criteria

- The existing six account tiers and financial rules are unchanged.
- One canonical entitlement resolver controls new tier-based service permissions.
- Unknown/missing tier data never grants premium access.
- Points cannot be created or redeemed twice by request retries.
- Points ledger is auditable and does not mutate the Emerald banking ledger.
- Failed redemption fulfilment can be reconciled without duplicate points debits.
- MineClubs access and the exact 50/25/unlimited admission tariff are checked server-side and charged at most once per admission.
- ClubCash CashLine Advance creates a standard CashLine debt entry and dispenses no more than once; it cannot debit balance or fall back to balance.
- ClubCash retains transaction journals and physical inventory safeguards.
- Optional ecosystem services cannot break login or core banking.
- Branding is consistent with the existing MineBank look; no unrelated redesign.
- Tests and deployment limitations are documented honestly.


## 6. MinePoints foundation implementation status

The first implementation stage provides an additive PostgreSQL migration for the append-only points ledger, a draft-only reward catalogue, and a redemption/fulfilment record model. The internal ledger primitive serializes changes per customer, derives balances from signed entries, rejects negative balances, and protects idempotency keys against payload changes. MinePoints never modifies Emerald balances or CashLine/loan records.

No rewards are seeded or activated, and no customer-facing earning/redemption route is enabled in this stage. Before enabling those flows, explicitly approve: eligible earning events and rates, anti-farming/reversal rules, reward catalogue and per-reward points prices, eligibility conditions, and fulfilment/cancellation handling. No points expiry is introduced.


## MineClubs physical admission

`POST /api/v1/minecraft/clubs/admissions` authorizes admission at an active, server-registered venue using a trusted terminal credential, active MineBank card credential, Wallet PIN, and idempotency key. The server resolves the canonical account tier and applies the approved tariff: Personal/Business 50 Emeralds per visit; Personal Pro/Business Pro 25 Emeralds; Personal Private/Corporate 0 Emeralds with unlimited admissions. Fees are paid from Balance only, never CashLine. Admission, fee ledger transaction, idempotency envelope, and audit event commit atomically; retries return the prior result. Venue/terminal provisioning is an operator task. Only a SHA-256 hash of the terminal secret is stored. The API does not itself open doors; the venue must deploy a trusted entrance controller that grants entry only after a successful response.

## Physical MineClubs entrance implementation

The physical admission path consists of three separately deployed pieces:

- `extras/mineclubs.sql`: explicit PostgreSQL migration for venues, trusted terminals, admission journal, and operation idempotency support.
- `scripts/provision_mineclub.py`: operator-run, dry-run-by-default provisioning tool. With explicit `--activate`, it creates an active venue if absent and an active terminal, stores only the SHA-256 token hash, and prints the plaintext terminal secret once after the database commit.
- `minecraft/cc_tweaked/mineclubs_entrance.lua`: customer-facing CC:Tweaked controller. It requires the MineBank card credential and six-digit Wallet PIN, persists the idempotency key before sending the request, and only pulses the configured redstone output after a valid `ADMITTED` response. An uncertain result can be resumed with the same key.

Tariffs are resolved from the canonical server-side account tier. Personal/Business pay 50 Emeralds per admission, Pro tiers pay 25, and Personal Private/Corporate pay zero with unlimited admission. Fees debit Balance only. The API response, admission record, fee ledger transaction, shared idempotency record, and audit event are committed as one database transaction.

**Deployment gate:** the migration must be applied explicitly; no venue is provisioned by deployment. Stage and test the complete physical relay, network-failure recovery, insufficient Balance, inactive venue, and revoked terminal before enabling the Minecraft integration flags in production. Use relays that fail locked on loss of power. The repository code cannot safely choose the real venue names, production database, terminal token storage, or physical door wiring on behalf of an operator.