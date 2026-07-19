-- Track gateway ownership so one process cannot close another live process's
-- streaming attempts during a rolling restart. Attempts from pre-lease
-- installations remain nullable and are reconciled only by stale-owner
-- cleanup.
CREATE TABLE IF NOT EXISTS nblb.gateway_instances (
    id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE nblb.request_attempts
    ADD COLUMN IF NOT EXISTS owner_id uuid
    REFERENCES nblb.gateway_instances(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS request_attempts_owner_idx
    ON nblb.request_attempts (owner_id, finished_at);

CREATE INDEX IF NOT EXISTS gateway_instances_last_seen_idx
    ON nblb.gateway_instances (last_seen_at);

-- Rows written before owner leases cannot be safely attributed to a live
-- process. Close them once during the migration so old `started` evidence is
-- not left open forever.
UPDATE nblb.request_attempts
SET outcome = 'abandoned_after_restart', finished_at = now()
WHERE finished_at IS NULL AND owner_id IS NULL;
