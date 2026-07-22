-- Complete the typed operations console with stable slots, client permits,
-- model catalog, persisted settings, and an active routing policy.

ALTER TABLE nblb.qa_runs
    ADD COLUMN IF NOT EXISTS deployment_commit text NOT NULL DEFAULT 'unknown'
        CHECK (deployment_commit = 'unknown' OR deployment_commit ~ '^[0-9a-f]{40}$');

ALTER TABLE nblb.upstream_keys
    ADD COLUMN IF NOT EXISTS slot_no smallint;

WITH numbered AS (
    SELECT id,
           CASE
               WHEN retired THEN ((abs(hashtext(id::text)) % 2) + 1)::smallint
               ELSE row_number() OVER (PARTITION BY retired ORDER BY created_at, id)::smallint
           END AS slot_no
    FROM nblb.upstream_keys
)
UPDATE nblb.upstream_keys AS key
SET slot_no = numbered.slot_no
FROM numbered
WHERE key.id = numbered.id AND key.slot_no IS NULL;

ALTER TABLE nblb.upstream_keys
    ALTER COLUMN slot_no SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'upstream_keys_slot_contract'
          AND conrelid = 'nblb.upstream_keys'::regclass
    ) THEN
        ALTER TABLE nblb.upstream_keys
            ADD CONSTRAINT upstream_keys_slot_contract CHECK (slot_no IN (1, 2));
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS upstream_keys_active_slot_idx
    ON nblb.upstream_keys (slot_no) WHERE retired = false;

UPDATE nblb.downstream_credentials
SET key_prefix = 'legacy_' || left(replace(id::text, '-', ''), 12)
WHERE key_prefix IS NULL;

ALTER TABLE nblb.downstream_credentials
    ALTER COLUMN key_prefix SET NOT NULL;

CREATE TABLE IF NOT EXISTS nblb.downstream_request_permits (
    request_id uuid PRIMARY KEY,
    downstream_credential_id uuid NOT NULL
        REFERENCES nblb.downstream_credentials(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    owner_id uuid
        REFERENCES nblb.gateway_instances(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    profile_id text NOT NULL
        REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    CONSTRAINT downstream_request_permits_time_contract CHECK (
        released_at IS NULL OR released_at >= acquired_at
    )
);

CREATE INDEX IF NOT EXISTS downstream_request_permits_active_idx
    ON nblb.downstream_request_permits (downstream_credential_id, acquired_at)
    WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS nblb.qa_failure_fixtures (
    run_id uuid NOT NULL REFERENCES nblb.qa_runs(id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('before_first_frame', 'after_first_frame')),
    downstream_credential_id uuid NOT NULL REFERENCES nblb.downstream_credentials(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    armed_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz,
    request_id uuid,
    key_id uuid REFERENCES nblb.upstream_keys(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (run_id, kind),
    CONSTRAINT qa_failure_fixture_consumption_contract CHECK (
        (consumed_at IS NULL AND request_id IS NULL AND key_id IS NULL)
        OR (consumed_at IS NOT NULL AND request_id IS NOT NULL AND key_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS qa_failure_fixture_request_idx
    ON nblb.qa_failure_fixtures (request_id) WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS nblb.model_catalog (
    profile_id text PRIMARY KEY
        REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    endpoint text NOT NULL CHECK (endpoint LIKE '/v1/%' AND length(endpoint) <= 128),
    input_modalities text[] NOT NULL CHECK (cardinality(input_modalities) BETWEEN 1 AND 8),
    output_modalities text[] NOT NULL CHECK (cardinality(output_modalities) BETWEEN 1 AND 8),
    streaming boolean NOT NULL,
    tool_calling boolean NOT NULL,
    advertised boolean NOT NULL DEFAULT true,
    billable_probe boolean NOT NULL DEFAULT false,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO nblb.model_catalog
    (profile_id, endpoint, input_modalities, output_modalities, streaming, tool_calling, advertised, billable_probe)
VALUES
    ('z-ai/glm-5.2','/v1/chat/completions',ARRAY['text'],ARRAY['text'],true,true,true,false),
    ('microsoft/phi-4-multimodal-instruct','/v1/chat/completions',ARRAY['text','image','audio'],ARRAY['text'],true,false,true,false),
    ('nvidia/vila','/v1/nvidia/inference',ARRAY['text','image','video'],ARRAY['text'],false,false,true,false),
    ('nvidia/nvclip','/v1/embeddings',ARRAY['text','image'],ARRAY['embedding'],false,false,true,false),
    ('black-forest-labs/flux.1-kontext-dev','/v1/images/generations',ARRAY['text','image'],ARRAY['image'],false,false,true,true),
    ('stabilityai/stable-video-diffusion','/v1/videos/generations',ARRAY['image'],ARRAY['video'],false,false,true,true),
    ('nvidia/magpie-tts-multilingual','/v1/audio/speech',ARRAY['text'],ARRAY['audio'],false,false,true,false),
    ('nvidia/parakeet-ctc-1.1b','/v1/audio/transcriptions',ARRAY['audio'],ARRAY['text'],false,false,true,false)
ON CONFLICT (profile_id) DO UPDATE SET
    endpoint = EXCLUDED.endpoint,
    input_modalities = EXCLUDED.input_modalities,
    output_modalities = EXCLUDED.output_modalities,
    streaming = EXCLUDED.streaming,
    tool_calling = EXCLUDED.tool_calling,
    billable_probe = EXCLUDED.billable_probe,
    updated_at = now();

CREATE TABLE IF NOT EXISTS nblb.operations_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    proof_freshness_seconds integer NOT NULL DEFAULT 604800
        CHECK (proof_freshness_seconds BETWEEN 300 AND 2592000),
    request_retention_days integer NOT NULL DEFAULT 30
        CHECK (request_retention_days BETWEEN 7 AND 90),
    metric_retention_days integer NOT NULL DEFAULT 90
        CHECK (metric_retention_days BETWEEN 7 AND 365),
    public_incidents_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO nblb.operations_settings(singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

INSERT INTO nblb.routing_policies(version, active, document, activated_at)
SELECT 1, true,
    '{"retryable_statuses":[402,408,429,500,502,503,504],"default_cooldown_seconds":2,"stream_failover_before_first_frame_only":true,"generation_retry":false}'::jsonb,
    now()
WHERE NOT EXISTS (SELECT 1 FROM nblb.routing_policies);

-- Crash recovery may release only permits owned by an expired gateway lease.
UPDATE nblb.downstream_request_permits AS permit
SET released_at = now()
WHERE permit.released_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM nblb.gateway_instances AS instance
      WHERE instance.id = permit.owner_id
        AND instance.last_seen_at >= now() - interval '30 seconds'
  );
