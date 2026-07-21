-- Additive, privacy-safe operations evidence foundation.
--
-- This schema stores request topology, status classes, timings, and bounded
-- operator evidence only. It intentionally has no columns for request or
-- response bodies, prompts, tool arguments, media, headers, or credentials.

ALTER TABLE nblb.downstream_credentials
    ADD COLUMN IF NOT EXISTS key_prefix text,
    ADD COLUMN IF NOT EXISTS expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS model_allowlist text[],
    ADD COLUMN IF NOT EXISTS rpm_limit integer,
    ADD COLUMN IF NOT EXISTS max_concurrency smallint,
    ADD COLUMN IF NOT EXISTS request_limit_day integer,
    ADD COLUMN IF NOT EXISTS rotated_at timestamptz,
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION nblb.jsonb_is_scalar_object(value jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_each(value) AS item
           WHERE jsonb_typeof(item.value) NOT IN ('null', 'boolean', 'number', 'string')
              OR (jsonb_typeof(item.value) = 'string' AND length(item.value #>> '{}') > 256)
       )
       AND (SELECT count(*) FROM jsonb_object_keys(value)) <= 32
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'downstream_credentials_operations_contract'
          AND conrelid = 'nblb.downstream_credentials'::regclass
    ) THEN
        ALTER TABLE nblb.downstream_credentials
            ADD CONSTRAINT downstream_credentials_operations_contract
            CHECK (
                (key_prefix IS NULL OR length(key_prefix) BETWEEN 6 AND 24)
                AND (
                    model_allowlist IS NULL
                    OR (
                        cardinality(model_allowlist) BETWEEN 1 AND 64
                        AND array_position(model_allowlist, NULL) IS NULL
                    )
                )
                AND (rpm_limit IS NULL OR rpm_limit BETWEEN 1 AND 100000)
                AND (max_concurrency IS NULL OR max_concurrency BETWEEN 1 AND 64)
                AND (request_limit_day IS NULL OR request_limit_day BETWEEN 1 AND 10000000)
                AND (expires_at IS NULL OR expires_at > created_at)
                AND nblb.jsonb_is_scalar_object(metadata)
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS downstream_credentials_expiry_idx
    ON nblb.downstream_credentials (active, expires_at);

CREATE TABLE IF NOT EXISTS nblb.proxy_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL UNIQUE,
    owner_id uuid REFERENCES nblb.gateway_instances(id) ON DELETE SET NULL,
    downstream_credential_id uuid
        REFERENCES nblb.downstream_credentials(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    endpoint text NOT NULL CHECK (length(endpoint) BETWEEN 1 AND 128),
    profile_id text NOT NULL
        REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    stream boolean NOT NULL DEFAULT false,
    modality text NOT NULL CHECK (length(modality) BETWEEN 1 AND 64),
    outcome text NOT NULL DEFAULT 'started'
        CHECK (outcome IN (
            'started',
            'succeeded',
            'failed',
            'cancelled',
            'rejected',
            'abandoned_after_restart'
        )),
    status_code smallint CHECK (status_code IS NULL OR status_code BETWEEN 100 AND 599),
    error_class text CHECK (error_class IS NULL OR length(error_class) BETWEEN 1 AND 80),
    duration_ms bigint CHECK (duration_ms IS NULL OR duration_ms >= 0),
    ttfb_ms bigint CHECK (ttfb_ms IS NULL OR ttfb_ms >= 0),
    failover_count smallint NOT NULL DEFAULT 0 CHECK (failover_count BETWEEN 0 AND 8),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    rolled_up_at timestamptz,
    CONSTRAINT proxy_requests_terminal_contract CHECK (
        (outcome = 'started' AND finished_at IS NULL)
        OR (outcome <> 'started' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS proxy_requests_started_idx
    ON nblb.proxy_requests (started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS proxy_requests_client_started_idx
    ON nblb.proxy_requests (downstream_credential_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS proxy_requests_profile_started_idx
    ON nblb.proxy_requests (profile_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS proxy_requests_rollup_idx
    ON nblb.proxy_requests (finished_at, id)
    WHERE finished_at IS NOT NULL AND rolled_up_at IS NULL;
CREATE INDEX IF NOT EXISTS proxy_requests_owner_idx
    ON nblb.proxy_requests (owner_id, finished_at);

ALTER TABLE nblb.request_attempts
    ADD COLUMN IF NOT EXISTS proxy_request_id uuid
        REFERENCES nblb.proxy_requests(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS attempt_no smallint,
    ADD COLUMN IF NOT EXISTS status_code smallint,
    ADD COLUMN IF NOT EXISTS error_class text,
    ADD COLUMN IF NOT EXISTS latency_ms bigint,
    ADD COLUMN IF NOT EXISTS ttfb_ms bigint,
    ADD COLUMN IF NOT EXISTS response_started boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS cooldown_applied_until timestamptz,
    ADD COLUMN IF NOT EXISTS bytes_out bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'request_attempts_observability_contract'
          AND conrelid = 'nblb.request_attempts'::regclass
    ) THEN
        ALTER TABLE nblb.request_attempts
            ADD CONSTRAINT request_attempts_observability_contract
            CHECK (
                (attempt_no IS NULL OR attempt_no BETWEEN 1 AND 8)
                AND (status_code IS NULL OR status_code BETWEEN 100 AND 599)
                AND (error_class IS NULL OR length(error_class) BETWEEN 1 AND 80)
                AND (latency_ms IS NULL OR latency_ms >= 0)
                AND (ttfb_ms IS NULL OR ttfb_ms >= 0)
                AND (bytes_out IS NULL OR bytes_out >= 0)
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS request_attempts_proxy_request_idx
    ON nblb.request_attempts (proxy_request_id, attempt_no, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS request_attempts_attempt_no_idx
    ON nblb.request_attempts (proxy_request_id, attempt_no)
    WHERE proxy_request_id IS NOT NULL AND attempt_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS nblb.probe_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('credential', 'profile', 'catalog')),
    upstream_id uuid
        REFERENCES nblb.upstream_keys(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    profile_id text
        REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'passed', 'failed', 'cancelled')),
    status_code smallint CHECK (status_code IS NULL OR status_code BETWEEN 100 AND 599),
    latency_ms bigint CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_class text CHECK (error_class IS NULL OR length(error_class) BETWEEN 1 AND 80),
    billable boolean NOT NULL DEFAULT false,
    requested_by text NOT NULL DEFAULT 'local_admin'
        CHECK (requested_by IN ('local_admin', 'scheduler', 'live_qa', 'request_success')),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT probe_runs_shape_contract CHECK (
        (kind = 'catalog' AND upstream_id IS NULL AND profile_id IS NULL)
        OR (kind = 'credential' AND upstream_id IS NOT NULL AND profile_id IS NULL)
        OR (kind = 'profile' AND upstream_id IS NOT NULL AND profile_id IS NOT NULL)
    ),
    CONSTRAINT probe_runs_terminal_contract CHECK (
        (status IN ('queued', 'running') AND finished_at IS NULL)
        OR (status IN ('passed', 'failed', 'cancelled') AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS probe_runs_created_idx
    ON nblb.probe_runs (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS probe_runs_upstream_profile_idx
    ON nblb.probe_runs (upstream_id, profile_id, created_at DESC);

ALTER TABLE nblb.profile_probe_receipts
    ADD COLUMN IF NOT EXISTS last_probe_run_id uuid
        REFERENCES nblb.probe_runs(id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS invalidated_at timestamptz,
    ADD COLUMN IF NOT EXISTS invalidation_reason text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'profile_probe_receipts_invalidation_contract'
          AND conrelid = 'nblb.profile_probe_receipts'::regclass
    ) THEN
        ALTER TABLE nblb.profile_probe_receipts
            ADD CONSTRAINT profile_probe_receipts_invalidation_contract
            CHECK (
                (invalidated_at IS NULL AND invalidation_reason IS NULL)
                OR (
                    invalidated_at IS NOT NULL
                    AND invalidation_reason IS NOT NULL
                    AND length(invalidation_reason) BETWEEN 1 AND 80
                )
            );
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS nblb.metric_buckets_minute (
    bucket_start timestamptz NOT NULL,
    endpoint text NOT NULL CHECK (length(endpoint) BETWEEN 1 AND 128),
    profile_id text NOT NULL
        REFERENCES nblb.routing_state(profile_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    outcome_class text NOT NULL
        CHECK (outcome_class IN ('success', 'client_error', 'upstream_error', 'cancelled', 'rejected')),
    request_count bigint NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    failover_count bigint NOT NULL DEFAULT 0 CHECK (failover_count >= 0),
    duration_sum_ms numeric(24, 0) NOT NULL DEFAULT 0 CHECK (duration_sum_ms >= 0),
    ttfb_sum_ms numeric(24, 0) NOT NULL DEFAULT 0 CHECK (ttfb_sum_ms >= 0),
    duration_sample_count bigint NOT NULL DEFAULT 0 CHECK (duration_sample_count >= 0),
    ttfb_sample_count bigint NOT NULL DEFAULT 0 CHECK (ttfb_sample_count >= 0),
    duration_le_250 bigint NOT NULL DEFAULT 0 CHECK (duration_le_250 >= 0),
    duration_le_500 bigint NOT NULL DEFAULT 0 CHECK (duration_le_500 >= 0),
    duration_le_1000 bigint NOT NULL DEFAULT 0 CHECK (duration_le_1000 >= 0),
    duration_le_2500 bigint NOT NULL DEFAULT 0 CHECK (duration_le_2500 >= 0),
    duration_le_5000 bigint NOT NULL DEFAULT 0 CHECK (duration_le_5000 >= 0),
    duration_gt_5000 bigint NOT NULL DEFAULT 0 CHECK (duration_gt_5000 >= 0),
    ttfb_le_250 bigint NOT NULL DEFAULT 0 CHECK (ttfb_le_250 >= 0),
    ttfb_le_500 bigint NOT NULL DEFAULT 0 CHECK (ttfb_le_500 >= 0),
    ttfb_le_1000 bigint NOT NULL DEFAULT 0 CHECK (ttfb_le_1000 >= 0),
    ttfb_le_2500 bigint NOT NULL DEFAULT 0 CHECK (ttfb_le_2500 >= 0),
    ttfb_le_5000 bigint NOT NULL DEFAULT 0 CHECK (ttfb_le_5000 >= 0),
    ttfb_gt_5000 bigint NOT NULL DEFAULT 0 CHECK (ttfb_gt_5000 >= 0),
    PRIMARY KEY (bucket_start, endpoint, profile_id, outcome_class)
);

CREATE INDEX IF NOT EXISTS metric_buckets_minute_profile_idx
    ON nblb.metric_buckets_minute (profile_id, bucket_start DESC);

CREATE TABLE IF NOT EXISTS nblb.routing_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version integer NOT NULL UNIQUE CHECK (version > 0),
    active boolean NOT NULL DEFAULT false,
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    CONSTRAINT routing_policies_activation_contract CHECK (
        (active = false) OR (active = true AND activated_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS routing_policies_single_active_idx
    ON nblb.routing_policies (active) WHERE active = true;

CREATE TABLE IF NOT EXISTS nblb.incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$' AND length(slug) <= 120),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    status text NOT NULL CHECK (status IN ('investigating', 'identified', 'monitoring', 'resolved')),
    severity text NOT NULL CHECK (severity IN ('minor', 'major', 'critical')),
    public boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT incidents_resolution_contract CHECK (
        (status = 'resolved' AND resolved_at IS NOT NULL)
        OR (status <> 'resolved' AND resolved_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS incidents_status_started_idx
    ON nblb.incidents (status, started_at DESC);

CREATE TABLE IF NOT EXISTS nblb.incident_updates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid NOT NULL
        REFERENCES nblb.incidents(id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('investigating', 'identified', 'monitoring', 'resolved')),
    public_message text NOT NULL CHECK (length(public_message) BETWEEN 1 AND 4000),
    published_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS incident_updates_incident_idx
    ON nblb.incident_updates (incident_id, published_at ASC);

CREATE TABLE IF NOT EXISTS nblb.audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 120),
    resource_kind text NOT NULL CHECK (length(resource_kind) BETWEEN 1 AND 80),
    resource_id uuid,
    actor_kind text NOT NULL DEFAULT 'local_admin'
        CHECK (actor_kind IN ('local_admin', 'scheduler', 'system')),
    request_id uuid,
    outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (nblb.jsonb_is_scalar_object(detail)),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_events_created_idx
    ON nblb.audit_events (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS audit_events_resource_idx
    ON nblb.audit_events (resource_kind, resource_id, created_at DESC);

CREATE TABLE IF NOT EXISTS nblb.qa_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    suite text NOT NULL
        CHECK (suite IN ('smoke', 'distribution', 'failover', 'persistence', 'multimodal', 'hermes-e2e')),
    live boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'passed', 'failed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT qa_runs_terminal_contract CHECK (
        (status IN ('queued', 'running') AND finished_at IS NULL)
        OR (status IN ('passed', 'failed', 'cancelled') AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS qa_runs_created_idx
    ON nblb.qa_runs (created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS nblb.qa_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL
        REFERENCES nblb.qa_runs(id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 160),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'passed', 'failed', 'skipped')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (nblb.jsonb_is_scalar_object(evidence)),
    started_at timestamptz,
    finished_at timestamptz,
    CONSTRAINT qa_cases_terminal_contract CHECK (
        (status IN ('pending', 'running') AND finished_at IS NULL)
        OR (status IN ('passed', 'failed', 'skipped') AND finished_at IS NOT NULL)
    ),
    UNIQUE (run_id, name)
);

CREATE INDEX IF NOT EXISTS qa_cases_run_idx
    ON nblb.qa_cases (run_id, id);
