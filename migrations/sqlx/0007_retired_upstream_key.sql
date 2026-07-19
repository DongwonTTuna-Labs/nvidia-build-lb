-- Retired encrypted credentials remain addressable by request_attempts so
-- rotation never rewrites historical key identity. Retired rows are excluded
-- from the two active routing slots but retained for audit and restart proof.
ALTER TABLE nblb.upstream_keys
    ADD COLUMN IF NOT EXISTS retired boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS upstream_keys_active_idx
    ON nblb.upstream_keys (retired, enabled, cooldown_until, id);
