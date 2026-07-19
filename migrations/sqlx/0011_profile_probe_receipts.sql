-- A key-level probe is not enough to claim that every advertised model works.
-- Keep one durable provider receipt per profile and upstream key; successful
-- OpenAI-compatible requests refresh the receipt without storing request data.
CREATE TABLE IF NOT EXISTS nblb.profile_probe_receipts (
    profile_id text NOT NULL REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    key_id uuid NOT NULL REFERENCES nblb.upstream_keys(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, key_id)
);

CREATE INDEX IF NOT EXISTS profile_probe_receipts_key_idx
    ON nblb.profile_probe_receipts (key_id, verified_at DESC);
