-- MineBank MinePoints ledger and rewards catalogue foundation.
-- Additive migration; apply explicitly after review. Never run this DDL in a request.
-- No reward rows are seeded: earning and redemption rules require explicit approval.

CREATE TABLE IF NOT EXISTS minepoints_ledger (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    entry_type VARCHAR(16) NOT NULL
        CHECK (entry_type IN ('AWARD', 'REDEMPTION', 'REVERSAL', 'ADJUSTMENT')),
    points_delta BIGINT NOT NULL CHECK (points_delta <> 0),
    idempotency_key VARCHAR(128) NOT NULL,
    reason VARCHAR(240) NOT NULL,
    source_type VARCHAR(40),
    source_id VARCHAR(128),
    related_account_id BIGINT REFERENCES bank_accounts(id) ON DELETE SET NULL,
    related_entry_id BIGINT REFERENCES minepoints_ledger(id) ON DELETE RESTRICT,
    rule_version VARCHAR(40),
    actor_client_id BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT minepoints_ledger_idempotency_uq UNIQUE (client_id, idempotency_key),
    CONSTRAINT minepoints_ledger_type_sign_check CHECK (
        (entry_type = 'AWARD' AND points_delta > 0)
        OR (entry_type = 'REDEMPTION' AND points_delta < 0)
        OR entry_type IN ('REVERSAL', 'ADJUSTMENT')
    ),
    CONSTRAINT minepoints_ledger_reversal_link_check CHECK (
        entry_type <> 'REVERSAL' OR related_entry_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS minepoints_ledger_client_created_idx
    ON minepoints_ledger(client_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS minepoints_ledger_source_idx
    ON minepoints_ledger(source_type, source_id)
    WHERE source_type IS NOT NULL;

COMMENT ON TABLE minepoints_ledger IS
    'Append-only MinePoints history. Points are loyalty units, never Emerald deposits or bank-account money.';

CREATE TABLE IF NOT EXISTS minepoints_rewards (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(64) NOT NULL UNIQUE,
    title VARCHAR(120) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    reward_type VARCHAR(24) NOT NULL
        CHECK (reward_type IN ('SERVICE_DISCOUNT', 'MINECLUB_EXPERIENCE', 'EXCLUSIVE_OFFER')),
    points_cost BIGINT NOT NULL CHECK (points_cost > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'ACTIVE', 'PAUSED', 'RETIRED')),
    eligibility_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    fulfilment_instructions TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS minepoints_rewards_status_idx
    ON minepoints_rewards(status, points_cost);

CREATE TABLE IF NOT EXISTS minepoints_redemptions (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
    reward_id BIGINT NOT NULL REFERENCES minepoints_rewards(id) ON DELETE RESTRICT,
    idempotency_key VARCHAR(128) NOT NULL,
    reward_code_snapshot VARCHAR(64) NOT NULL,
    reward_title_snapshot VARCHAR(120) NOT NULL,
    points_cost BIGINT NOT NULL CHECK (points_cost > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'REQUESTED'
        CHECK (status IN ('REQUESTED', 'PROCESSING', 'FULFILLED', 'FAILED', 'CANCELLED')),
    ledger_entry_id BIGINT REFERENCES minepoints_ledger(id) ON DELETE RESTRICT,
    fulfilment_reference VARCHAR(128),
    failure_reason VARCHAR(240),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT minepoints_redemption_idempotency_uq UNIQUE (client_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS minepoints_redemptions_client_idx
    ON minepoints_redemptions(client_id, created_at DESC, id DESC);

COMMENT ON TABLE minepoints_rewards IS
    'Operator-managed MinePoints reward catalogue. No active rewards are seeded by the foundation migration.';
COMMENT ON TABLE minepoints_redemptions IS
    'Auditable reward fulfilment workflow. A redemption must be paired with one MinePoints ledger debit.';
