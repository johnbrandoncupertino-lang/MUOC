# MUOC -> MineBank Core Migration Plan

## Phase 1 — foundation
- Introduce client/account/role model.
- Introduce integer-Emerald ledger.
- Introduce tier policy tables for Personal and Business.
- Introduce configurable fees and credit facilities.
- Introduce immutable audit events.
- Keep the existing wallet tables temporarily for compatibility.

## Phase 2 — authentication and permissions
- Replace wallet-name session identity with client/account context.
- Implement Client / Bank Operator / Admin authorization.
- Add sensitive-operation reauthentication and wallet PIN authorization.

## Phase 3 — ledger-backed operations
- Move transfers, fees, credit, refunds and admin adjustments onto the ledger.
- Add idempotency and concurrency controls.
- Add reservation/release for pending transfers.

## Phase 4 — UI
- Dashboard becomes account-centric.
- Replace Tier Selection with purchase/request workflows.
- Add statements, transaction details and approval queues.

## Phase 5 — API v1
- Versioned API and scoped credentials.
- Minecraft server credentials and revocation.
- Accounts, transfers, transactions, credit and users endpoints.

## Phase 6 — migration/deprecation
- Migrate existing wallets to clients + accounts.
- Reconcile balances against opening ledger entries.
- Remove legacy wallet mutations only after reconciliation tests pass.
