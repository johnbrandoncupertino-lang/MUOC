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
    credit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    default_credit_limit INTEGER NOT NULL DEFAULT 0 CHECK (default_credit_limit >= 0),
    private_or_corporate BOOLEAN NOT NULL DEFAULT FALSE,
    eligibility_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
(code,account_type,display_name,monthly_fee,max_balance,monthly_outgoing_limit,credit_enabled,default_credit_limit,private_or_corporate,eligibility_config)
VALUES
('PERSONAL','PERSONAL','Personal',0,5000,5000,FALSE,0,FALSE,'{}'),
('PERSONAL_PRO','PERSONAL','Personal Pro',0,NULL,20000,TRUE,2000,FALSE,'{}'),
('PERSONAL_PRIVATE','PERSONAL','Personal Private',0,NULL,NULL,TRUE,1500,TRUE,'{"minimum_age":18,"minimum_balance":0,"minimum_operations":0,"max_requested_credit":20000}'),
('BUSINESS','BUSINESS','Business',0,NULL,NULL,TRUE,0,FALSE,'{}'),
('BUSINESS_PRO','BUSINESS','Business Pro',0,NULL,NULL,TRUE,0,FALSE,'{}'),
('CORPORATE','BUSINESS','Corporate',0,NULL,NULL,TRUE,0,TRUE,'{"minimum_age":18,"minimum_balance":0,"minimum_operations":0}')
ON CONFLICT (code) DO NOTHING;
