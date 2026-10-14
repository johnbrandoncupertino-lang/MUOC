# MineBank CC:Tweaked terminal programs

These programs connect CC:Tweaked computers/turtles to the feature-flagged MineBank Minecraft API. MineBank remains authoritative for account ownership, balances, checkout amounts, limits and ledger entries.

## Programs

- `branch_terminal.lua`: authenticates a MineBank customer, lets them select an eligible account and issues a revocable card credential onto a floppy.
- Balance checks are part of the ATM workflow; the redundant standalone `balance_reader.lua` program has been removed.
- `merchant_checkout.lua`: trusted shop terminal that creates a fixed-amount checkout using a dedicated merchant credential, then lets a customer pay with their MineBank card.
- `atm.lua`: inventory-based ATM with withdrawals, deposits, transfers up to 1,000 Emeralds, recent history, card revocation, and durable journals for physical inventory operations.
- `vending_machine.lua`: local per-machine product catalogue, fixed-price MineBank checkout, stock/output inventory checks, and a fail-closed fulfilment journal.
- `minebank_ui.lua`: shared MineBank terminal UI, HTTP client, floppy-card reader, inventory discovery and setup utilities.

## Server setup

1. Review the code and run the repository tests before enabling the integration.
2. Apply `extras/minebank_minecraft.sql` to the intended PostgreSQL database through the normal reviewed migration process. This is separate from `extras/minebank_v2.sql`; application requests do not apply this migration automatically.
3. Confirm the schema includes `minebank_minecraft_cards`, `minebank_minecraft_operations`, `minebank_minecraft_merchants`, and `minebank_minecraft_checkouts`.
4. Keep `MINEBANK_MINECRAFT_ENABLED` unset or false until the card schema is migrated and staging is verified. Keep `MINEBANK_MINECRAFT_TRANSACTIONS_ENABLED` unset/false until the transaction schema is migrated, `MINEBANK_MINECRAFT_ATM_TOKEN` is configured, and money-movement tests pass; set it to `true` only for the intended deployment after explicit review.
5. Configure a strong random `MINEBANK_MINECRAFT_ATM_TOKEN` (at least 32 characters) in the API deployment and enter the same secret in the trusted ATM setup wizard. Keep it off the floppy cards, out of the repository and away from player-accessible machines.
6. Confirm HTTPS forwarding is correct at the Vercel deployment. All Lua programs require HTTPS.

## Merchant provisioning

Provision a merchant against an active MineBank client and account using a trusted machine with the application's database environment configured:

```sh
python extras/provision_minecraft_merchant.py --client-id 123 --account-id 456 --name "Spawn Shop"
```

Replace the example IDs with real IDs. The utility stores only a SHA-256 token hash and prints the raw `mbmerchant_` token once. Put it only in `MERCHANT_TOKEN` on a locked-down shop terminal, then remove it from shell history/output and keep a secure backup for rotation. Do not put this token on a player-accessible computer, public floppy, repository, or server shared with untrusted players. Rotate/revoke it by setting that merchant row's `active` field to false and provisioning a replacement.

Merchant checkout amounts are supplied by the trusted merchant terminal and stored server-side in a five-minute checkout. The customer's payment request supplies only the checkout ID; it cannot change the amount. A merchant credential is a privileged secret and must only be used on a machine controlled by the merchant.

## CC:Tweaked setup

1. Replace `API_BASE` in the Lua programs with the HTTPS MineBank API origin ending in `/api/v1/minecraft`.
2. Enable the CC:Tweaked HTTP API and permit the MineBank HTTPS hostname.
3. Issue a card with `branch_terminal.lua`; the floppy stores only the revocable card token and non-sensitive display metadata. Never store a password or Wallet PIN on it.
4. For shop checkout, configure the dedicated merchant token in `merchant_checkout.lua`. The customer inserts their MineBank card floppy only when asked to pay.
5. Run `atm.lua` on a computer with three distinct inventory peripherals: deposit input, secure stock/escrow, and output. Run `vending_machine.lua` on a trusted shop computer with separate stock and output inventories. Both programs provide local setup wizards.

## Money movement and safety

- All amounts are integer Emeralds. Merchant purchases use the fixed amount from a MineBank-issued checkout; cardholders cannot override it.
- Payment, transfer and withdrawal require a valid active card and active customer/account, plus a fresh Wallet PIN; physical ATM deposits and withdrawals additionally require the trusted ATM machine token. The API checks configured limits, banking locks and system suspension, and writes each balance change, ledger transaction, operation record and audit event atomically.
- `Idempotency-Key` is mandatory. Repeating the same request returns the recorded result; reusing the key with different parameters is rejected. A database advisory lock plus a unique database constraint protects concurrent retries.
- ATM withdrawals use ordinary balance only, not CashLine. The API debits first and returns a stable withdrawal ticket. The turtle writes a durable journal before making the request and marks dispensing as started before dropping items. If interrupted after dispensing begins, it refuses automatic replay and requires operator reconciliation. This prevents a retry from automatically dispensing a second withdrawal, but physical item delivery cannot be transactionally atomic with PostgreSQL; interruption may require manual reconciliation.
- A successful database response is not proof that physical items reached the player. Check the ATM journal and ledger before resolving an interrupted dispense.
- The merchant token is not a customer card token. Keep merchant terminals locked down; players with access to the Lua source or computer may be able to recover the merchant token.
- Card payments, transfers and withdrawals require a fresh six-digit Wallet PIN; CashLine is not used by Minecraft money movement. Transfers are capped at 1,000 Emeralds and are limited to personal accounts so business permissions/approval rules cannot be bypassed.
- ATM deposits accept only Emeralds moved from the configured deposit inventory into secure stock/escrow before the API request. If the API cannot confirm the deposit, the journal and escrow must be preserved and retried with the same idempotency key; never move or spend those items manually until reconciled.
- Vending machines store catalogue and pricing locally per machine. They only deliver after MineBank confirms payment. An interrupted dispense is locked for manual reconciliation rather than automatically replayed.
- If a card is lost, revoke it from the MineBank portal. Revocation invalidates future API requests immediately.

## API contract

- `POST /accounts`: customer login + Wallet PIN for account selection.
- `POST /cards`: issue a card; raw credential returned once.
- `GET /account`: live account balance with a card bearer token.
- `POST /cards/revoke`: revoke a card after customer re-authentication.
- `POST /merchant/checkouts`: trusted merchant bearer token + amount/description + mandatory idempotency key.
- `POST /payments`: card bearer token + server-issued checkout ID + mandatory idempotency key.
- `POST /withdrawals`: card bearer token + integer Emerald amount + Wallet PIN + mandatory idempotency key.
- `POST /deposits`: card bearer token + amount secured in ATM escrow + Wallet PIN + mandatory idempotency key.
- `POST /transfers`: card bearer token + recipient account number + amount up to 1,000 + Wallet PIN + mandatory idempotency key; business-account transfers are rejected for portal authorization.
- `GET /history`: latest ten ledger transactions for the linked card account.
- `GET /merchant/checkouts/<id>`: status lookup for the merchant that created the checkout.
- `POST /cards/revoke-self`: revoke the inserted card with its credential, Wallet PIN and explicit confirmation.

The code change does not itself apply the database migration, set deployment environment variables, or verify a live Vercel deployment. Test in staging before enabling production money movement.


## All-in-one installer

On a CC:Tweaked computer with HTTP enabled, run `wget run https://raw.githubusercontent.com/johnbrandoncupertino-lang/MUOC/main/minecraft/cc_tweaked/install.lua`. Alternatively, download `install.lua` from this directory and run it locally. The installer downloads Basalt2 and the supported MineBank programs into `/minebank`, stages all downloads before replacing existing files, and keeps the previous installation if a download fails. It does not request or embed customer credentials.

The installer includes a pinned Basalt2 full release and its MIT license. Basalt2 project: https://github.com/Pyroxenium/Basalt2

## Interface redesign notes

- The ATM is the primary place to check a live balance; the standalone balance reader is no longer part of a fresh installation.
- Do not use a monitor as a keyboard-prompt mirror. A monitor should be treated as a touch-capable display with readable, bounded content.
- Keep operator-only setup separate from customer flows. The trusted ATM token must never be written to a floppy card.
- The shared API client times out after 10 seconds. A timeout is not proof that a financial request failed. Retry only with the original idempotency key; definitive HTTP 4xx rejections are marked cancelled, and a rejected deposit is cancelled only after all physical Emeralds are returned. Physical item movement after a withdrawal/deposit is uncertain until inventory and journal state agree; never erase that record just to unlock the ATM.
