-- MineClubs admission and trusted entrance terminal schema.
-- Apply explicitly after review; API requests never run DDL.
CREATE TABLE IF NOT EXISTS minebank_club_locations (
 id BIGSERIAL PRIMARY KEY,
 code VARCHAR(48) NOT NULL UNIQUE,
 display_name VARCHAR(120) NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','PAUSED','CLOSED')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS minebank_club_terminals (
 id BIGSERIAL PRIMARY KEY,
 club_id BIGINT NOT NULL REFERENCES minebank_club_locations(id) ON DELETE RESTRICT,
 label VARCHAR(80) NOT NULL DEFAULT 'MineClubs entrance',
 token_hash CHAR(64) NOT NULL UNIQUE,
 status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REVOKED')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_used_at TIMESTAMPTZ,
 revoked_at TIMESTAMPTZ,
 CHECK ((status='ACTIVE' AND revoked_at IS NULL) OR (status='REVOKED' AND revoked_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS minebank_club_terminals_club_idx ON minebank_club_terminals(club_id,status);
CREATE TABLE IF NOT EXISTS minebank_club_admissions (
 id BIGSERIAL PRIMARY KEY,
 client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE RESTRICT,
 account_id BIGINT NOT NULL,
 club_id BIGINT NOT NULL REFERENCES minebank_club_locations(id) ON DELETE RESTRICT,
 terminal_id BIGINT NOT NULL REFERENCES minebank_club_terminals(id) ON DELETE RESTRICT,
 idempotency_key VARCHAR(128) NOT NULL,
 tier_code VARCHAR(40) NOT NULL,
 fee_amount BIGINT NOT NULL CHECK (fee_amount >= 0),
 currency VARCHAR(16) NOT NULL DEFAULT 'Emerald',
 transaction_id VARCHAR(80),
 status VARCHAR(16) NOT NULL DEFAULT 'ADMITTED' CHECK (status IN ('ADMITTED','REVIEW','REVOKED')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT minebank_club_admissions_idempotency_uq UNIQUE (client_id,idempotency_key),
 CONSTRAINT minebank_club_admission_account_owner_fk FOREIGN KEY (account_id,client_id) REFERENCES bank_accounts(id,client_id),
 CONSTRAINT minebank_club_admission_transaction_uq UNIQUE (transaction_id)
);
CREATE INDEX IF NOT EXISTS minebank_club_admissions_client_idx ON minebank_club_admissions(client_id,created_at DESC);
CREATE INDEX IF NOT EXISTS minebank_club_admissions_club_idx ON minebank_club_admissions(club_id,created_at DESC);
ALTER TABLE minebank_minecraft_operations DROP CONSTRAINT IF EXISTS minebank_minecraft_operation_amount_check;
ALTER TABLE minebank_minecraft_operations DROP CONSTRAINT IF EXISTS minebank_minecraft_operations_amount_check;
ALTER TABLE minebank_minecraft_operations ADD CONSTRAINT minebank_minecraft_operation_amount_check CHECK (amount >= 0);
ALTER TABLE minebank_minecraft_operations DROP CONSTRAINT IF EXISTS minebank_minecraft_operations_operation_type_check;
ALTER TABLE minebank_minecraft_operations ADD CONSTRAINT minebank_minecraft_operations_operation_type_check CHECK (operation_type IN ('PAYMENT','WITHDRAWAL','TRANSFER','DEPOSIT','CLUB_ADMISSION'));
