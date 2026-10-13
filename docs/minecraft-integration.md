# MineBank × Minecraft (CC:Tweaked)

## Status and deployment gates

The integration now supports card issuance/revocation, live balance reads, merchant-authorized fixed-amount checkout payments, and ATM withdrawals. Money movement remains unavailable unless `MINEBANK_MINECRAFT_ENABLED=true` is explicitly configured.

Before enabling it:
1. Apply `extras/minebank_minecraft.sql` to the intended PostgreSQL database through the normal migration process.
2. Run tests and verify the feature in staging.
3. Confirm Vercel HTTPS/proxy forwarding and database transaction behavior.
4. Provision a dedicated merchant credential out of band. Do not use a player-controlled machine as a merchant terminal.

This change does not apply the migration, change deployment environment variables, or prove that the live deployment is healthy.

## Merchant checkout payment flow

1. A trusted merchant terminal authenticates with a dedicated `mbmerchant_` credential.
2. The merchant terminal submits the amount and description to `POST /merchant/checkouts` with an `Idempotency-Key`.
3. MineBank validates the merchant credential, checks the merchant account/profile is active, stores a short-lived checkout (five minutes), and returns a checkout ID.
4. The customer presents an active MineBank card. The payment request contains the checkout ID, not an amount.
5. MineBank reads the amount from the stored checkout, validates card/account/customer status, checks banking suspension/locks, checks configured outgoing limits and sufficient balance, credits the merchant account, marks the checkout paid, writes the ledger and audit entries, and records the idempotent operation in one database transaction.

A checkout can be settled only once. A cardholder cannot change its amount. Reusing an idempotency key with different request parameters is rejected. The merchant credential is a privileged secret: provision it with `extras/provision_minecraft_merchant.py` and keep it only on a locked-down merchant-controlled computer.

## ATM withdrawal flow

1. The customer inserts their MineBank card floppy and enters a whole Emerald amount.
2. The turtle verifies it has enough `minecraft:emerald` items and persists a local request journal before contacting MineBank.
3. MineBank validates the active card/account/customer, account limits, balance, banking locks and suspension. It atomically debits the account, inserts a `WITHDRAWAL` ledger row, stores the operation/idempotency result, and writes an audit event.
4. The API returns the stable transaction/withdrawal ID. The turtle persists authorization, persists a `DISPENSE_STARTED` state before dropping items, and records `COMPLETE` only after the drop loop finishes.

The database side is atomic and idempotent. Physical item dispensing is not part of the PostgreSQL transaction. If the turtle stops after dispensing starts, the journal blocks automatic retry to prevent double dispensing. The output may be partial and needs operator reconciliation against the journal and ledger. Do not delete a non-complete journal or simply start a new withdrawal to bypass the lock.

## Database and ledger integration

The additive migration creates:
- `minebank_minecraft_cards`: revocable token hashes linked to a specific client/account pair.
- `minebank_minecraft_operations`: per-customer idempotency records linked to the card and account.
- `minebank_minecraft_merchants`: active merchant tokens stored as SHA-256 hashes, bound to a MineBank client and receiving account.
- `minebank_minecraft_checkouts`: fixed amounts, merchant ownership, expiry and settlement status.

Money movement uses the existing `bank_accounts.balance`, `account_tiers_v2` limits, `ledger_transactions`, and `audit_events` structures. It does not create a separate balance ledger or use CashLine. Payment/withdrawal balance updates, ledger rows, checkout settlement (for payments), idempotency records, and audit events commit or roll back together.

The API checks:
- valid card token, active/revocation state and client/account ownership;
- active customer, account and merchant;
- system suspension and per-client banking locks;
- whole-Emerald amounts and maximum amount bounds;
- configured single, daily and monthly outgoing limits;
- sufficient ordinary balance and merchant maximum balance;
- idempotency key uniqueness and request fingerprint consistency.

## Known boundary and security model

PostgreSQL cannot atomically commit an item drop in Minecraft. The ATM uses a durable local journal and fail-closed recovery rather than attempting to promise impossible exactly-once physical dispensing. Operator reconciliation is required for an interrupted/partial dispense.

The merchant terminal must be trusted and locked down. Its merchant token must never be committed to Git, put on a floppy, or exposed to players. Customer card floppies contain only revocable card credentials, never passwords or Wallet PINs. All API calls require HTTPS.

## Endpoints

- `POST /accounts`: authenticated customer account selection.
- `POST /cards`: issue a card credential.
- `GET /account`: read live balance.
- `POST /cards/revoke`: revoke a card.
- `POST /merchant/checkouts`: create fixed-amount checkout with merchant credential and idempotency key.
- `POST /payments`: settle a checkout using card credential and idempotency key.
- `POST /withdrawals`: atomically debit balance and return a stable ATM dispense record.

All routes remain hidden while the feature flag is unset/false.
