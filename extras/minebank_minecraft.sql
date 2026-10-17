-- MineBank Minecraft / CC:Tweaked card foundation.
-- Additive only: this file is intentionally not auto-applied by application requests.
-- Apply through the normal reviewed database migration process before enabling routes.

CREATE UNIQUE INDEX IF NOT EXISTS bank_accounts_id_client_id_uq
    ON bank_accounts(id, client_id);

CREATE TABLE IF NOT EXISTS minebank_minecraft_cards (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL,
    account_id BIGINT NOT NULL,
    card_prefix VARCHAR(16) NOT NULL UNIQUE,
    card_token_hash CHAR(64) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'REVOKED')),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    label VARCHAR(80) NOT NULL DEFAULT 'Minecraft card',
    CONSTRAINT minebank_minecraft_cards_owner_fk
        FOREIGN KEY (account_id, client_id)
        REFERENCES bank_accounts(id, client_id),
    CONSTRAINT minebank_minecraft_cards_revocation_check
        CHECK (
            (status = 'ACTIVE' AND revoked_at IS NULL)
            OR (status = 'REVOKED' AND revoked_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS minebank_minecraft_cards_client_idx
    ON minebank_minecraft_cards(client_id, issued_at DESC);

CREATE INDEX IF NOT EXISTS minebank_minecraft_cards_account_idx
    ON minebank_minecraft_cards(account_id, status);

-- Per-machine ATM pairings replace the old deployment-wide shared environment token.
-- The ATM secret is shown once by the pairing endpoint; only its SHA-256 hash is stored.
CREATE TABLE IF NOT EXISTS minebank_minecraft_atms (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    enrolled_by_card_id BIGINT NOT NULL,
    token_prefix VARCHAR(16) NOT NULL UNIQUE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    label VARCHAR(80) NOT NULL DEFAULT 'Minecraft ATM',
    status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','REVOKED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT minebank_minecraft_atm_card_owner_fk
        FOREIGN KEY (enrolled_by_card_id, client_id)
        REFERENCES minebank_minecraft_cards(id, client_id),
    CONSTRAINT minebank_minecraft_atm_revocation_check
        CHECK (
            (status='ACTIVE' AND revoked_at IS NULL)
            OR (status='REVOKED' AND revoked_at IS NOT NULL)
        )
);
CREATE INDEX IF NOT EXISTS minebank_minecraft_atms_client_idx
    ON minebank_minecraft_atms(client_id, created_at DESC);

COMMENT ON TABLE minebank_minecraft_atms IS
    'Per-device ATM credentials created after MineBank card and Wallet PIN pairing. Only SHA-256 token hashes are stored.';

COMMENT ON TABLE minebank_minecraft_cards IS
    'Revocable card credentials for MineBank Minecraft integration. Store only SHA-256 token hashes; never store raw card tokens, passwords, or Wallet PINs.';


-- Composite ownership key for operation rows. The public card ID is not a
-- secret and must never be used as the sole authorization check.
CREATE UNIQUE INDEX IF NOT EXISTS minebank_minecraft_cards_id_client_id_uq
    ON minebank_minecraft_cards(id, client_id);

-- Idempotency envelope for future Minecraft payments and withdrawals.
-- A handler must validate card/client/account status and commit its ledger
-- mutation and operation result in the same transaction before enabling money
-- movement. This table by itself does not enable payment or withdrawal routes.
CREATE TABLE IF NOT EXISTS minebank_minecraft_operations (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    card_id BIGINT NOT NULL,
    account_id BIGINT NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    operation_type VARCHAR(16) NOT NULL
        CHECK (operation_type IN ('PAYMENT', 'WITHDRAWAL', 'TRANSFER', 'DEPOSIT')),
    request_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'COMPLETED', 'REJECTED', 'REVIEW')),
    amount BIGINT NOT NULL CHECK (amount > 0),
    transaction_id VARCHAR(80),
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT minebank_minecraft_operation_idempotency_uq
        UNIQUE (client_id, idempotency_key),
    CONSTRAINT minebank_minecraft_operation_card_owner_fk
        FOREIGN KEY (card_id, client_id)
        REFERENCES minebank_minecraft_cards(id, client_id),
    CONSTRAINT minebank_minecraft_operation_account_owner_fk
        FOREIGN KEY (account_id, client_id)
        REFERENCES bank_accounts(id, client_id)
);

CREATE INDEX IF NOT EXISTS minebank_minecraft_operations_client_idx
    ON minebank_minecraft_operations(client_id, created_at DESC);

CREATE INDEX IF NOT EXISTS minebank_minecraft_operations_card_idx
    ON minebank_minecraft_operations(card_id, created_at DESC);

COMMENT ON TABLE minebank_minecraft_operations IS
    'Idempotency envelope for future Minecraft payments/withdrawals. The handler must validate active card and account ownership, lock the operation key, and atomically commit ledger changes before enabling execution.';


-- Trusted merchant credentials and server-authoritative checkout intents.
-- Provision merchant tokens out-of-band: store only SHA-256(token), never the raw token.
CREATE TABLE IF NOT EXISTS minebank_minecraft_merchants (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id),
    account_id BIGINT NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(120) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT minebank_minecraft_merchant_owner_fk
        FOREIGN KEY (account_id, client_id)
        REFERENCES bank_accounts(id, client_id)
);
CREATE INDEX IF NOT EXISTS minebank_minecraft_merchants_account_idx
    ON minebank_minecraft_merchants(account_id, active);

CREATE TABLE IF NOT EXISTS minebank_minecraft_checkouts (
    id BIGSERIAL PRIMARY KEY,
    merchant_id BIGINT NOT NULL REFERENCES minebank_minecraft_merchants(id),
    merchant_idempotency_key VARCHAR(128) NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0 AND amount <= 1000000),
    description VARCHAR(200) NOT NULL,
    merchant_reference VARCHAR(100),
    status VARCHAR(12) NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','PAID','EXPIRED','CANCELLED')),
    transaction_id VARCHAR(80),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    UNIQUE (merchant_id, merchant_idempotency_key),
    UNIQUE (transaction_id)
);
CREATE INDEX IF NOT EXISTS minebank_minecraft_checkouts_expiry_idx
    ON minebank_minecraft_checkouts(status, expires_at);

COMMENT ON TABLE minebank_minecraft_merchants IS
    'Trusted merchant terminal credentials. Only SHA-256 token hashes are stored; provision and rotate tokens out-of-band.';
COMMENT ON TABLE minebank_minecraft_checkouts IS
    'Short-lived, merchant-authorized fixed-amount checkouts. Cardholders can pay a checkout but cannot change its amount.';

ALTER TABLE minebank_minecraft_checkouts
    ADD COLUMN IF NOT EXISTS request_hash CHAR(64);


-- Upgrade existing installations as well as fresh installs. The table's original
-- constraint only allowed PAYMENT/WITHDRAWAL, so CREATE TABLE IF NOT EXISTS is
-- not sufficient after the first migration has already been applied.
ALTER TABLE minebank_minecraft_operations
    DROP CONSTRAINT IF EXISTS minebank_minecraft_operations_operation_type_check;
ALTER TABLE minebank_minecraft_operations
    ADD CONSTRAINT minebank_minecraft_operations_operation_type_check
    CHECK (operation_type IN ('PAYMENT', 'WITHDRAWAL', 'TRANSFER', 'DEPOSIT'));
