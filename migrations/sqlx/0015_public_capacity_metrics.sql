-- Preserve privacy-safe eligible-provider capacity observations for the public status trend.

CREATE TABLE IF NOT EXISTS nblb.capacity_buckets_minute (
    bucket_start timestamptz PRIMARY KEY,
    eligible_provider_count smallint NOT NULL
        CHECK (eligible_provider_count BETWEEN 0 AND 2)
);

CREATE INDEX IF NOT EXISTS capacity_buckets_minute_recent_idx
    ON nblb.capacity_buckets_minute (bucket_start DESC);
