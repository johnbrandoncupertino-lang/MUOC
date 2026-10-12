# MineBank × Minecraft (CC: Tweaked) — integration foundation

## Status

This is an **initial API foundation**. The code now includes feature-flagged card issuance, card revocation, and read-only linked-account access. It is not live by default, and it cannot process payments. Do not enable it in production until the schema migration has been applied, CI and security review pass, and the operator has verified the deployment's HTTPS/proxy configuration.

The feature must remain disabled unless the deployment explicitly sets:

`MINEBANK_MINECRAFT_ENABLED=true`

The application must treat a missing, empty, or unrecognized value as disabled.

## Intended first release

1. A player visits an official MineBank branch terminal in Minecraft.
2. The terminal sends MineBank credentials over HTTPS. MineBank performs its normal login protection, verifies the Wallet PIN, and checks that the selected account belongs to the authenticated client and is usable.
3. MineBank creates one revocable card credential linked to that client and account. The raw credential is returned once and written to the floppy disk; only its SHA-256 hash is stored in the database.
4. The disk never contains the account password or Wallet PIN. It should contain only the card credential and non-sensitive display metadata.
5. A read-only endpoint validates the card credential, checks its active/revoked state and account status, then returns only the minimum account information needed by the terminal.
6. Card revocation must invalidate the credential immediately. Lost disks are treated as lost cards.

## Initial API contract

All routes are under `/api/v1/minecraft` and return 404 unless `MINEBANK_MINECRAFT_ENABLED=true` is set explicitly. Requests require HTTPS.

- `POST /cards`: JSON body with `email`, `password`, `wallet_pin`, `account_id`, optional `label`, and optional `captcha_answer`. Returns the raw card credential exactly once, along with a card ID and issue timestamp. Keep the credential only on the floppy disk.
- `GET /account`: `Authorization: Bearer <card credential>`. Returns the linked account's type, tier, status, account number, and Emerald balance. This is read-only.
- `POST /cards/revoke`: JSON body with `email`, `password`, `wallet_pin`, and `card_id`. Revokes a card owned by the authenticated customer.

Card issuance reuses MineBank's existing login checks and Wallet PIN lockout, accepts customer-role identities only, restricts each customer to five active cards and ten issuances per rolling 24 hours (including revoked cards), and records issuance, read, and revocation audit events. The account lookup is always scoped to the authenticated customer. The raw credential is never written to the database or application logs.

## Data model

`extras/minebank_minecraft.sql` adds `minebank_minecraft_cards` with:
- a client/account ownership relationship enforced by a composite foreign key;
- a unique non-reversible token hash and non-secret lookup prefix;
- explicit ACTIVE/REVOKED lifecycle state;
- issuance, last-use, and revocation timestamps.

The migration is additive and does not reset or delete existing banking data. It is deliberately not run automatically on requests.

## Security requirements before endpoints can be enabled

- Use HTTPS only; reject insecure production requests.
- Reuse MineBank's login failure counters, temporary blocks, CAPTCHA/challenge policy, and Wallet PIN lockout. Do not implement a parallel password checker that bypasses those protections.
- Never log submitted passwords, Wallet PINs, or raw card credentials.
- Rate-limit authentication, PIN attempts, card issuance, and card-token checks. Use shared/server-side limits that work across serverless instances.
- Hash card tokens before storage and compare safely; return the raw token only once during issuance.
- Require explicit ownership checks for every account lookup. Never trust a client-supplied account ID by itself.
- Audit successful issuance, failed authentication, card access, and revocation without recording secrets.
- Do not store long-lived API service secrets in Lua source or on player-accessible floppy disks.
- Do not implement transfers, deposits, withdrawals, CashLine purchases, or checkout settlement in this phase. Financial writes require idempotency, duplicate-request tests, server-authoritative amount validation, and atomic ledger handling.
- Keep all Minecraft routes unavailable by default. Do not change existing portal UI or banking behavior as part of this integration.

## Planned implementation sequence

1. Add reviewed card schema and initial API implementation (this draft PR).
2. Confirm CI passes and review the authentication/session behavior and Vercel HTTPS forwarding before applying the migration.
3. Add deeper database-backed tests for account ownership, card issuance/revocation, revoked tokens, and locked accounts.
4. Only after that passes review, design idempotent payment authorization and settlement as a separate change.

## CC: Tweaked terminal contract

The terminal should use HTTPS requests only, display a confirmation before any future payment, and fail closed on timeouts or malformed responses. It must not locally decide available balance, account limits, CashLine eligibility, interest, or transaction status. MineBank remains authoritative.
