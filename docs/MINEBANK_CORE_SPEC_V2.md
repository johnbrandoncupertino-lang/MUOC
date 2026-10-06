# MineBank / MUOC Core Specification v2

## Purpose
MUOC is the secure functioning banking platform ("MineBank - The New Bank"). A separate public website will explain products/services and link into MUOC; MUOC owns identity, accounts, balances and all financial operations.

## Identity and accounts
- Client login: email + password.
- One client has one login and may hold exactly one Personal account plus multiple Business accounts.
- Every account receives a permanent account number.
- Roles: Client, Bank Operator, Admin.
- Admins can create, delete, suspend users.
- Future SSO/API integration is possible but not required for this phase.

## Personal tiers
| Tier | Max balance | Monthly outgoing transfer limit | Credit |
|---|---:|---:|---|
| Personal | 5,000 | 5,000 | None |
| Personal Pro | Unlimited | 20,000 | Up to 2,000 |
| Personal Private | Unlimited | Unlimited | 1,500 pre-approved; requestable increase up to 20,000 |

- Tier selection is an upgrade/request workflow, not a free selector.
- Personal Pro can be purchased immediately.
- Personal Private requires configurable bank eligibility requirements (minimum age, balance and transaction/operations volume) and approval.
- Downgrades are supported and immediately remove privileges that no longer apply.
- Every tier has a monthly fee configured by the bank.
- The system may suggest upgrades based on balance, transaction volume and account age, but suggestions never auto-upgrade an account.

## Business tiers
- Business, Business Pro, Corporate.
- Multiple Business accounts per client are allowed.
- Corporate has configurable minimum age, balance and operations-volume requirements.
- Exact Business limits/fees are configurable bank policy and must not be hardcoded.

## Transfers
Every transfer has:
- permanent transaction ID (format such as MUOC-2026-000001)
- sender account
- recipient account
- amount
- fee
- timestamp
- status
- description/reference

Rules:
- Recipient can be resolved by permanent account number and/or email.
- No self-transfers.
- Transfers are integer Emerald amounts.
- All outgoing transfers have a 30-second per-account cooldown.
- Monthly outgoing limits are enforced per account.
- Fees are calculated before confirmation and shown to the client.
- Transfers >= 5,000 enter PENDING_APPROVAL; funds are reserved immediately, recipient is notified, and delivery occurs only after Bank Operator/Admin approval.
- Operators and Admins can approve/reject.
- Admins can originate transfers on behalf of users.
- Users can see incoming/outgoing/pending/failed transfers, fees, IDs and downloadable statements.
- Refund/chargeback requests require bank approval. Approved chargebacks create a separate immutable reversal transaction. If the recipient cannot cover it, the recipient account is allowed to go negative and the bank is notified.

## Ledger
- Every money movement creates an immutable ledger transaction.
- Never rewrite historical transactions to simulate reversals.
- Reversals are separate transactions linked to the original.
- Ledger must support atomic balance updates and concurrency-safe reservations.
- Fees belong to the bank/admin account.
- Monthly tier fees are separate billing entries.
- Users can inspect complete transaction details.
- Admins have a global ledger.

## Credit
Credit is tier-controlled.
- Example: balance 100, available credit 1,000, outgoing spend 400 => balance -300.
- Credit is limited by approved credit limit.
- When credit limit is exhausted, outgoing/withdrawal operations are rejected; incoming transfers remain allowed.
- Incoming money does not automatically repay debt. The client explicitly chooses repayment.
- Standard interest: 7% per month, configurable by the bank.
- Interest is accounted monthly and appears in the monthly statement.
- Credit activation fee: 10 Emerald/month for small lines (including Personal Pro and Business basic); 40 Emerald/month for credit lines over 5,000.
- A credit line can incur its activation fee even when unused.
- If a debt is not repaid within one month, the system issues notices, suspends all credit lines, then freezes the account pending bank intervention.
- Credit suspension/freeze state transitions must be auditable.

## Fees
Central configurable Fee Engine:
- Bank Admin can modify fees without code.
- Operators cannot modify fees.
- Fees may vary by tier, amount, transaction type, account type, time or promotion.
- Client sees applicable fee before confirmation.
- Fees go to the bank account.
- Fees are not separate user-facing transfers except recurring monthly tier/credit billing entries.

## Account states
Core states:
ACTIVE -> overdue notifications -> all credit lines suspended -> FROZEN pending bank response.
Also support LIMITED and CLOSED as administrative states where needed.
Frozen accounts require bank intervention for restoration.

## Permissions
### Client
Own accounts, transfers, credit actions permitted by tier, statements, requests, profile/security.

### Bank Operator
Can approve user requests, approve/reject qualifying transfers, approve credit/tier requests, process refunds/chargebacks, lock/unlock accounts, view ledger/user information, and edit permitted client information.
Cannot change bank settings, fee rules, tier definitions, limits, or create/delete Admins.

### Admin
Full bank control, including users, operators, tiers, fees, limits, balances, ledger actions, reversals, settings, audit logs, statistics and system configuration.
Sensitive operations require admin password re-entry.

## Security
- Wallet/transaction authorization requires a wallet password/PIN.
- Transfer confirmation is mandatory.
- 3 incorrect wallet-password attempts cause a 5-minute lockout.
- Successful authentication resets the failed-attempt counter.
- Sensitive admin operations require re-authentication.
- Immutable audit trail records actor, action, target, timestamp, IP/context and related transaction/account identifiers.

## Currency
- Single currency: Emerald.
- Integer Emeralds only.
- Minecraft Emerald icon/branding.
- Future multi-currency is out of scope for this phase.

## Minecraft integration
Target architecture:
Minecraft Server -> ComputerCraft bank terminal -> MUOC API -> MineBank account.
API capabilities:
- balance
- deposit
- withdraw
- transfer
- receive/inspect transfers
- transaction history
Minecraft servers use separate API credentials and must be scoped/revocable.

## Public API
Design API v1 from the beginning, even if not all endpoints are immediately exposed:
- /api/v1/accounts
- /api/v1/transfers
- /api/v1/transactions
- /api/v1/credit
- /api/v1/users
- authentication/authorization and Minecraft server credentials
All financial endpoints must enforce authorization, idempotency where appropriate, validation and atomic database operations.

## UI
- MineBank - The New Bank.
- Visual language inspired by Fineco circa 2005-2012: gradients, panels, beveled controls, restrained retro textures/icons.
- Dashboard: welcome message, account situation overview, important messages and basic operations.
