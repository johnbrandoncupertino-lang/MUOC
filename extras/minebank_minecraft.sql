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

COMMENT ON TABLE minebank_minecraft_cards IS
    'Revocable card credentials for MineBank Minecraft integration. Store only SHA-256 token hashes; never store raw card tokens, passwords, or Wallet PINs.';
