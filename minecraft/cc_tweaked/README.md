# MineBank CC:Tweaked terminal programs

These programs connect CC:Tweaked computers/turtles to the feature-flagged MineBank Minecraft API. MineBank remains authoritative for account ownership, balances, checkout amounts, limits and ledger entries.

## Programs

- `branch_terminal.lua`: authenticates a MineBank customer, lets them select an eligible account and issues a revocable card credential onto a floppy.
- `balance_reader.lua`: reads a card and displays the live MineBank account balance.
- `merchant_checkout.lua`: trusted shop terminal that creates a fixed-amount checkout using a dedicated merchant credential, then lets a customer pay with their MineBank card.
- `atm.lua`: turtle-based ATM that debits MineBank atomically and dispenses emerald items. A durable journal blocks automatic re-dispensing if a run is interrupted.

## Server setup

1. Review the code and run the repository tests before enabling the integration.
2. Apply `extras/minebank_minecraft.sql` to the intended PostgreSQL database through the normal reviewed migration process. This is separate from `extras/minebank_v2.sql`; application requests do not apply this migration automatically.
3. Confirm the schema includes `minebank_minecraft_cards`, `minebank_minecraft_operations`, `minebank_minecraft_merchants`, and `minebank_minecraft_checkouts`.
4. Keep `MINEBANK_MINECRAFT_ENABLED` unset or false until migration and staging verification are complete. Enable it explicitly only for the intended deployment.
5. Confirm HTTPS forwarding is correct at the Vercel deployment. All Lua programs require HTTPS.

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
5. For ATM withdrawals, run `atm.lua` on a turtle stocked with `minecraft:emerald` items. The turtle dispenses from its front side, so position the customer/output there.

## Money movement and safety

- All amounts are integer Emeralds. Merchant purchases use the fixed amount from a MineBank-issued checkout; cardholders cannot override it.
- Payment and withdrawal require a valid active card and active customer/account, check the account's configured single/daily/monthly outgoing limits, respect banking locks and system suspension, and write the balance change, ledger transaction, operation record and audit event in one database transaction.
- `Idempotency-Key` is mandatory. Repeating the same request returns the recorded result; reusing the key with different parameters is rejected. A database advisory lock plus a unique database constraint protects concurrent retries.
- ATM withdrawals use ordinary balance only, not CashLine. The API debits first and returns a stable withdrawal ticket. The turtle writes a durable journal before making the request and marks dispensing as started before dropping items. If interrupted after dispensing begins, it refuses automatic replay and requires operator reconciliation. This prevents a retry from automatically dispensing a second withdrawal, but physical item delivery cannot be transactionally atomic with PostgreSQL; interruption may require manual reconciliation.
- A successful database response is not proof that physical items reached the player. Check the ATM journal and ledger before resolving an interrupted dispense.
- The merchant token is not a customer card token. Keep merchant terminals locked down; players with access to the Lua source or computer may be able to recover the merchant token.
- Card payments and withdrawals do not use CashLine, and no transfers/deposits are exposed through these Lua clients.
- If a card is lost, revoke it from the MineBank portal. Revocation invalidates future API requests immediately.

## API contract

- `POST /accounts`: customer login + Wallet PIN for account selection.
- `POST /cards`: issue a card; raw credential returned once.
- `GET /account`: live account balance with a card bearer token.
- `POST /cards/revoke`: revoke a card after customer re-authentication.
- `POST /merchant/checkouts`: trusted merchant bearer token + amount/description + mandatory idempotency key.
- `POST /payments`: card bearer token + server-issued checkout ID + mandatory idempotency key.
- `POST /withdrawals`: card bearer token + integer Emerald amount + mandatory idempotency key; debits MineBank balance and returns a single-use dispense record.

The code change does not itself apply the database migration, set deployment environment variables, or verify a live Vercel deployment. Test in staging before enabling production money movement.
