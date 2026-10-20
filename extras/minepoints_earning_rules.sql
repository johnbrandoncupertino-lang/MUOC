-- MinePoints earning rules v2. Explicit migration; do not run DDL in portal requests.
-- Rates are intentionally NULL and disabled until approved by the bank owner.
CREATE TABLE IF NOT EXISTS minepoints_tier_rates (
  tier_code VARCHAR(64) PRIMARY KEY,
  points_per_100_emeralds BIGINT NULL CHECK (points_per_100_emeralds IS NULL OR points_per_100_emeralds >= 0),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  rule_version VARCHAR(40) NOT NULL DEFAULT 'v1',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS minepoints_eligible_events (
  event_code VARCHAR(64) PRIMARY KEY,
  label VARCHAR(120) NOT NULL,
  event_group VARCHAR(32) NOT NULL CHECK (event_group IN ('EXTERNAL_TRANSFER','PURCHASE','FEE')),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  requires_explicit_approval BOOLEAN NOT NULL DEFAULT FALSE,
  rule_version VARCHAR(40) NOT NULL DEFAULT 'v1',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by BIGINT REFERENCES bank_clients(id) ON DELETE SET NULL
);
INSERT INTO minepoints_eligible_events(event_code,label,event_group,enabled,requires_explicit_approval)
VALUES
 ('EXTERNAL_TRANSFER_OUT','Completed external outgoing transfer','EXTERNAL_TRANSFER',FALSE,FALSE),
 ('PURCHASE','Completed purchase','PURCHASE',FALSE,FALSE),
 ('APPROVED_FEE','Explicitly approved fee-paying service','FEE',FALSE,TRUE)
ON CONFLICT (event_code) DO NOTHING;
CREATE TABLE IF NOT EXISTS minepoints_award_sources (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL REFERENCES bank_clients(id) ON DELETE CASCADE,
  account_id BIGINT NOT NULL REFERENCES bank_accounts(id) ON DELETE RESTRICT,
  event_code VARCHAR(64) NOT NULL REFERENCES minepoints_eligible_events(event_code),
  source_id VARCHAR(128) NOT NULL,
  eligible_amount NUMERIC(20,2) NOT NULL CHECK (eligible_amount > 0),
  tier_code VARCHAR(64) NOT NULL,
  rule_version VARCHAR(40) NOT NULL,
  points_awarded BIGINT NOT NULL CHECK (points_awarded > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(event_code, source_id)
);
CREATE INDEX IF NOT EXISTS minepoints_tier_rates_enabled_idx ON minepoints_tier_rates(enabled);
COMMENT ON TABLE minepoints_tier_rates IS 'Owner-configured MinePoints earned per 100 Emeralds of eligible activity; NULL/disabled means no earning.';
COMMENT ON TABLE minepoints_eligible_events IS 'Server-side allowlist. Every event is disabled by default; fee events require explicit owner approval.';
