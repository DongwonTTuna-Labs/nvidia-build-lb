-- Preserve downstream usage metadata across gateway restarts.
ALTER TABLE nblb.downstream_credentials
    ADD COLUMN IF NOT EXISTS request_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_used_at timestamptz;
