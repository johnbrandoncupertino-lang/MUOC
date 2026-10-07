-- MineBank v2 foundation schema. All monetary columns are integer Emeralds.
CREATE SEQUENCE IF NOT EXISTS muoc_transaction_seq START 1;

CREATE TABLE IF NOT EXISTS bank_clients (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(320) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'CLIENT' CHECK (role IN ('CLIENT','OPERATOR','ADMIN')),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LIMITED','FROZEN','CLOSED')),
    date_of_birth DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMPTZ
);

-- Authentication hardening for the v2 portal.
CREATE SEQUENCE IF NOT EXISTS muoc_account_number_seq START 100001;

ALTER TABLE bank_clients
    ADD COLUMN IF NOT EXISTS wallet_pin_hash VARCHAR(255),
    ADD COLUMN IF NOT EXISTS wallet_pin_failed_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wallet_pin_locked_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS admin_reauth_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS account_tiers_v2 (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(40) UNIQUE NOT NULL,
    account_type VARCHAR(20) NOT NULL CHECK (account_type IN ('PERSONAL','BUSINESS')),
    display_name VARCHAR(80) NOT NULL,
    monthly_fee INTEGER NOT NULL DEFAULT 0 CHECK (monthly_fee >= 0),
    max_balance INTEGER CHECK (max_balance IS NULL OR max_balance >= 0),
    monthly_outgoing_limit INTEGER CHECK (monthly_outgoing_limit IS NULL OR monthly_outgoing_limit >= 0),
    daily_outgoing_limit INTEGER CHECK (daily_outgoing_limit IS NULL OR daily_outgoing_limit >= 0),
    single_transfer_limit INTEGER CHECK (single_transfer_limit IS NULL OR single_transfer_limit >= 0),
    credit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    default_credit_limit INTEGER NOT NULL DEFAULT 0 CHECK (default_credit_limit >= 0),
    private_or_corporate BOOLEAN NOT NULL DEFAULT FALSE,
    eligibility_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Backward-compatible migration for deployments that created account_tiers_v2
-- before the current limit/credit fields were introduced.
ALTER TABLE account_tiers_v2
    ADD COLUMN IF NOT EXISTS monthly_outgoing_limit INTEGER,
    ADD COLUMN IF NOT EXISTS daily_outgoing_limit INTEGER,
    ADD COLUMN IF NOT EXISTS single_transfer_limit INTEGER,
    ADD COLUMN IF NOT EXISTS credit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS default_credit_limit INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS private_or_corporate BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS eligibility_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS bank_accounts (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id),
    account_number VARCHAR(32) UNIQUE NOT NULL,
    account_type VARCHAR(20) NOT NULL CHECK (account_type IN ('PERSONAL','BUSINESS')),
    tier_id BIGINT NOT NULL REFERENCES account_tiers_v2(id),
    balance BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LIMITED','FROZEN','CLOSED')),
    monthly_outgoing_used BIGINT NOT NULL DEFAULT 0,
    monthly_outgoing_period CHAR(7) NOT NULL DEFAULT TO_CHAR(CURRENT_DATE,'YYYY-MM'),
    last_outgoing_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS one_personal_account_per_client
ON bank_accounts(client_id) WHERE account_type='PERSONAL' AND status <> 'CLOSED';

ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS last_outgoing_at TIMESTAMPTZ;
-- Account lifecycle, business ownership and security extensions.
ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS freeze_type VARCHAR(40);

CREATE TABLE IF NOT EXISTS minebank_business_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT UNIQUE NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    legal_name VARCHAR(200) NOT NULL,
    trading_name VARCHAR(200),
    registration_number VARCHAR(100),
    address VARCHAR(300),
    contact_email VARCHAR(320),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS minebank_business_members (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    role VARCHAR(30) NOT NULL CHECK (role IN ('OWNER','ADMIN','FINANCE_MANAGER','EMPLOYEE','READ_ONLY','PAYROLL_MANAGER')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id,client_id)
);

CREATE TABLE IF NOT EXISTS minebank_security_events (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
    event_type VARCHAR(60) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'INFO',
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address VARCHAR(64),
    user_agent VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS minebank_sessions (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    session_token_hash VARCHAR(128) UNIQUE NOT NULL,
    ip_address VARCHAR(64),
    user_agent VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS minebank_risk_events (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES bank_accounts(id) ON DELETE SET NULL,
    client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
    risk_score INTEGER NOT NULL,
    severity VARCHAR(20) NOT NULL,
    reason VARCHAR(1000),
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_blocked_until TIMESTAMPTZ;
ALTER TABLE bank_clients ADD COLUMN IF NOT EXISTS login_captcha_required BOOLEAN NOT NULL DEFAULT FALSE;



CREATE TABLE IF NOT EXISTS credit_facilities (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT UNIQUE NOT NULL REFERENCES bank_accounts(id),
    credit_limit BIGINT NOT NULL DEFAULT 0 CHECK (credit_limit >= 0),
    interest_monthly_bps INTEGER NOT NULL DEFAULT 700 CHECK (interest_monthly_bps >= 0),
    activation_fee_monthly INTEGER NOT NULL DEFAULT 10 CHECK (activation_fee_monthly >= 0),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','CLOSED')),
    activated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_interest_at TIMESTAMPTZ,
    overdue_since TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id BIGSERIAL PRIMARY KEY,
    transaction_id VARCHAR(32) UNIQUE NOT NULL,
    transaction_type VARCHAR(40) NOT NULL,
    amount BIGINT NOT NULL CHECK (amount >= 0),
    fee BIGINT NOT NULL DEFAULT 0 CHECK (fee >= 0),
    currency VARCHAR(32) NOT NULL DEFAULT 'Emerald',
    sender_account_id BIGINT REFERENCES bank_accounts(id),
    recipient_account_id BIGINT REFERENCES bank_accounts(id),
    status VARCHAR(30) NOT NULL,
    description VARCHAR(500),
    reference_id VARCHAR(100),
    approved_by BIGINT REFERENCES bank_clients(id),
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ledger_sender_idx ON ledger_transactions(sender_account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ledger_recipient_idx ON ledger_transactions(recipient_account_id, created_at DESC);


CREATE TABLE IF NOT EXISTS minebank_banking_locks (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    reason VARCHAR(200),
    locked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    unlocked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS minebank_banking_locks_client_idx
    ON minebank_banking_locks(client_id, unlocked_at);

CREATE TABLE IF NOT EXISTS bank_notifications (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id),
    account_id BIGINT REFERENCES bank_accounts(id),
    notification_type VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    message VARCHAR(1000) NOT NULL,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS bank_notifications_client_idx ON bank_notifications(client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fee_rules (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    transaction_type VARCHAR(40) NOT NULL,
    account_type VARCHAR(20),
    tier_code VARCHAR(40),
    min_amount BIGINT,
    max_amount BIGINT,
    percentage_bps INTEGER NOT NULL DEFAULT 0,
    fixed_amount BIGINT NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 100,
    conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_billing (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    billing_type VARCHAR(30) NOT NULL,
    amount BIGINT NOT NULL,
    billing_period CHAR(7) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, billing_type, billing_period)
);

CREATE TABLE IF NOT EXISTS bank_requests_v2 (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES bank_clients(id),
    account_id BIGINT REFERENCES bank_accounts(id),
    request_type VARCHAR(40) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    reviewed_by BIGINT REFERENCES bank_clients(id),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    actor_client_id BIGINT REFERENCES bank_clients(id),
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id VARCHAR(100),
    account_id BIGINT REFERENCES bank_accounts(id),
    transaction_id VARCHAR(32),
    ip_address INET,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Append-only at the application layer: no UPDATE/DELETE API is exposed for these rows.
CREATE INDEX IF NOT EXISTS audit_events_actor_idx ON audit_events(actor_client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_target_idx ON audit_events(target_type, target_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_credentials (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES bank_clients(id),
    name VARCHAR(100) NOT NULL,
    key_prefix VARCHAR(16) NOT NULL,
    secret_hash VARCHAR(255) NOT NULL,
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS transfer_idempotency (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id),
    transaction_id VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO account_tiers_v2
(code,account_type,display_name,monthly_fee,max_balance,monthly_outgoing_limit,daily_outgoing_limit,single_transfer_limit,credit_enabled,default_credit_limit,private_or_corporate,eligibility_config)
VALUES
('PERSONAL','PERSONAL','Personal',0,5000,5000,5000,5000,FALSE,0,FALSE,'{}'),
('PERSONAL_PRO','PERSONAL','Personal Pro',0,NULL,20000,10000,10000,TRUE,2000,FALSE,'{}'),
('PERSONAL_PRIVATE','PERSONAL','Personal Private',0,NULL,NULL,NULL,NULL,TRUE,1500,TRUE,'{"minimum_age":18,"minimum_balance":0,"minimum_operations":0,"max_requested_credit":20000}'),
('BUSINESS','BUSINESS','Business',0,NULL,NULL,NULL,NULL,TRUE,0,FALSE,'{}'),
('BUSINESS_PRO','BUSINESS','Business Pro',0,NULL,NULL,NULL,NULL,TRUE,0,FALSE,'{}'),
('CORPORATE','BUSINESS','Corporate',0,NULL,NULL,NULL,NULL,TRUE,0,TRUE,'{"minimum_age":18,"minimum_balance":0,"minimum_operations":0}')
ON CONFLICT (code) DO NOTHING;


CREATE TABLE IF NOT EXISTS minebank_saved_recipients (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    account_number VARCHAR(32) NOT NULL,
    nickname VARCHAR(120) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(client_id, account_number)
);

CREATE TABLE IF NOT EXISTS minebank_transfer_templates (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    recipient_account_number VARCHAR(32) NOT NULL,
    amount BIGINT CHECK (amount IS NULL OR amount > 0),
    description VARCHAR(500),
    reference VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS minebank_scheduled_transfers (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    recipient_account_number VARCHAR(32) NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    schedule_type VARCHAR(20) NOT NULL CHECK (schedule_type IN ('ONCE','DAILY','WEEKLY','MONTHLY','CUSTOM')),
    next_run_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    description VARCHAR(500),
    reference VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','PAUSED','COMPLETED','CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE minebank_scheduled_transfers ADD COLUMN IF NOT EXISTS recurrence_config JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE minebank_scheduled_transfers ADD COLUMN IF NOT EXISTS last_error VARCHAR(500);
ALTER TABLE minebank_scheduled_transfers ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE minebank_scheduled_transfers DROP CONSTRAINT IF EXISTS minebank_scheduled_transfers_schedule_type_check;
ALTER TABLE minebank_scheduled_transfers ADD CONSTRAINT minebank_scheduled_transfers_schedule_type_check
CHECK (schedule_type IN ('ONCE','DAILY','WEEKLY','MONTHLY','CUSTOM'));

CREATE TABLE IF NOT EXISTS minebank_business_payment_approvals (
    id BIGSERIAL PRIMARY KEY,
    transaction_id VARCHAR(32) UNIQUE NOT NULL REFERENCES ledger_transactions(transaction_id),
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    requested_by BIGINT NOT NULL REFERENCES bank_clients(id),
    approved_by BIGINT REFERENCES bank_clients(id),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    risk_score INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS minebank_payment_requests (
    id BIGSERIAL PRIMARY KEY,
    requester_client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    payer_client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
    requester_account_id BIGINT NOT NULL REFERENCES bank_accounts(id),
    payer_account_number VARCHAR(32) NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    description VARCHAR(500),
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PAID','REJECTED','EXPIRED','CANCELLED')),
    expires_at TIMESTAMPTZ NOT NULL,
    paid_transaction_id VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS transfer_kind VARCHAR(40);
ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS category VARCHAR(60);

CREATE INDEX IF NOT EXISTS minebank_payment_requests_payer_idx ON minebank_payment_requests(payer_account_number,status);
CREATE INDEX IF NOT EXISTS minebank_payment_requests_requester_idx ON minebank_payment_requests(requester_client_id,created_at DESC);

CREATE TABLE IF NOT EXISTS minebank_limit_overrides (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
    requested_by BIGINT NOT NULL REFERENCES bank_clients(id),
    reviewed_by BIGINT REFERENCES bank_clients(id),
    field_name VARCHAR(40) NOT NULL,
    old_value BIGINT,
    new_value BIGINT NOT NULL,
    reason VARCHAR(500) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','APPROVED','REJECTED')),
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS minebank_notification_preferences (
    client_id BIGINT PRIMARY KEY REFERENCES bank_clients(id) ON DELETE CASCADE,
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    transfer_email BOOLEAN NOT NULL DEFAULT TRUE,
    security_email BOOLEAN NOT NULL DEFAULT TRUE,
    request_email BOOLEAN NOT NULL DEFAULT TRUE,
    statement_email BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS minebank_email_outbox (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES bank_clients(id),
    email VARCHAR(320) NOT NULL,
    notification_type VARCHAR(30) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'QUEUED',
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS minebank_email_outbox_idx ON minebank_email_outbox(status,created_at);

ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS category VARCHAR(60);


-- MineBank pricing and credit policy (2026-10-07)
-- Canonical MineBank pricing, limits and CashLine terms.
ALTER TABLE account_tiers_v2 ADD COLUMN IF NOT EXISTS opening_fee INTEGER NOT NULL DEFAULT 0;
ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS interest_annual_bps INTEGER NOT NULL DEFAULT 1830;
ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS minimum_due_percent_bps INTEGER NOT NULL DEFAULT 500;
ALTER TABLE credit_facilities ADD COLUMN IF NOT EXISTS minimum_due_floor INTEGER NOT NULL DEFAULT 40;

UPDATE account_tiers_v2 SET monthly_fee=0, opening_fee=0, max_balance=5000,
  monthly_outgoing_limit=10000, daily_outgoing_limit=1000, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=1800, private_or_corporate=FALSE WHERE code='PERSONAL';
UPDATE account_tiers_v2 SET monthly_fee=10, opening_fee=40, max_balance=NULL,
  monthly_outgoing_limit=100000, daily_outgoing_limit=10000, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=5000, private_or_corporate=FALSE WHERE code='PERSONAL_PRO';
UPDATE account_tiers_v2 SET monthly_fee=50, opening_fee=50, max_balance=NULL,
  monthly_outgoing_limit=NULL, daily_outgoing_limit=NULL, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=50000, private_or_corporate=TRUE WHERE code='PERSONAL_PRIVATE';
UPDATE account_tiers_v2 SET monthly_fee=10, opening_fee=10, max_balance=50000,
  monthly_outgoing_limit=50000, daily_outgoing_limit=5000, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=5000, private_or_corporate=FALSE WHERE code='BUSINESS';
UPDATE account_tiers_v2 SET monthly_fee=50, opening_fee=50, max_balance=NULL,
  monthly_outgoing_limit=1000000, daily_outgoing_limit=100000, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=100000, private_or_corporate=FALSE WHERE code='BUSINESS_PRO';
UPDATE account_tiers_v2 SET monthly_fee=100, opening_fee=200, max_balance=NULL,
  monthly_outgoing_limit=NULL, daily_outgoing_limit=NULL, single_transfer_limit=NULL,
  credit_enabled=TRUE, default_credit_limit=5000000, private_or_corporate=TRUE WHERE code='CORPORATE';

UPDATE credit_facilities cf SET
  interest_annual_bps=CASE WHEN t.account_type='PERSONAL' THEN 1830 ELSE 2370 END,
  interest_monthly_bps=CASE WHEN t.account_type='PERSONAL' THEN 1830 ELSE 2370 END,
  minimum_due_percent_bps=500, minimum_due_floor=40,
  activation_fee_monthly=CASE WHEN cf.credit_limit>5000 THEN 20 ELSE 10 END
FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id
WHERE cf.account_id=a.id;

CREATE TABLE IF NOT EXISTS credit_statements (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  principal_amount BIGINT NOT NULL DEFAULT 0,
  interest_amount BIGINT NOT NULL DEFAULT 0,
  credit_fee BIGINT NOT NULL DEFAULT 0,
  other_costs BIGINT NOT NULL DEFAULT 0,
  total_due BIGINT NOT NULL DEFAULT 0,
  minimum_due BIGINT NOT NULL DEFAULT 40,
  amount_paid BIGINT NOT NULL DEFAULT 0,
  status VARCHAR(20) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','PARTIALLY_PAID','PAID','OVERDUE')),
  issued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  due_at TIMESTAMPTZ NOT NULL,
  UNIQUE(account_id,period_start,period_end)
);
CREATE INDEX IF NOT EXISTS credit_statements_account_idx ON credit_statements(account_id,due_at DESC);

CREATE TABLE IF NOT EXISTS credit_statement_items (
  id BIGSERIAL PRIMARY KEY,
  statement_id BIGINT NOT NULL REFERENCES credit_statements(id) ON DELETE CASCADE,
  item_type VARCHAR(30) NOT NULL,
  amount BIGINT NOT NULL,
  description VARCHAR(500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

UPDATE credit_facilities SET interest_annual_bps=1830, minimum_due_percent_bps=500, minimum_due_floor=40
WHERE account_id IN (SELECT a.id FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE t.account_type='PERSONAL');
UPDATE credit_facilities SET interest_annual_bps=2370, minimum_due_percent_bps=500, minimum_due_floor=40
WHERE account_id IN (SELECT a.id FROM bank_accounts a JOIN account_tiers_v2 t ON t.id=a.tier_id WHERE t.account_type='BUSINESS');


-- MineBank canonical fee rules
INSERT INTO fee_rules(name,transaction_type,tier_code,min_amount,max_amount,fixed_amount,priority,active)
VALUES
('TRANSFER_STANDARD_UP_TO_500','TRANSFER',NULL,NULL,500,0,10,TRUE),
('TRANSFER_STANDARD_OVER_500','TRANSFER',NULL,501,NULL,5,20,TRUE),
('TRANSFER_PRIVATE_FREE','TRANSFER','PERSONAL_PRIVATE',NULL,NULL,0,1,TRUE),
('TRANSFER_CORPORATE_FREE','TRANSFER','CORPORATE',NULL,NULL,0,1,TRUE),
('PAYMENT_REQUEST','PAYMENT_REQUEST',NULL,NULL,NULL,1,10,TRUE),
('SCHEDULE_SETUP','SCHEDULED_PAYMENT_SETUP',NULL,NULL,NULL,2,10,TRUE),
('SCHEDULE_EXECUTION','SCHEDULED_PAYMENT',NULL,NULL,NULL,1,10,TRUE),
('PRINTED_STATEMENT','PRINTED_STATEMENT',NULL,NULL,NULL,1,10,TRUE)
ON CONFLICT(name) DO UPDATE SET fixed_amount=EXCLUDED.fixed_amount,active=TRUE;
