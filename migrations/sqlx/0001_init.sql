-- SQLx forward migration for the Rust gateway's minimal serving surface.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;
CREATE SCHEMA IF NOT EXISTS nblb;

CREATE TABLE IF NOT EXISTS nblb.upstream_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
    fingerprint bytea NOT NULL CHECK (octet_length(fingerprint) = 32),
    ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) > 16),
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    enabled boolean NOT NULL DEFAULT true,
    cooldown_until timestamptz,
    request_count bigint NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    failure_count bigint NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nblb.downstream_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
    digest bytea NOT NULL UNIQUE CHECK (octet_length(digest) = 32),
    scopes text[] NOT NULL CHECK (cardinality(scopes) BETWEEN 1 AND 6),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS nblb.routing_state (
    profile_id text PRIMARY KEY CHECK (profile_id IN ('z-ai/glm-5.2','microsoft/phi-4-multimodal-instruct','nvidia/vila','nvidia/nvclip','black-forest-labs/flux.1-kontext-dev','stabilityai/stable-video-diffusion','nvidia/magpie-tts-multilingual')),
    next_slot smallint NOT NULL DEFAULT 1 CHECK (next_slot IN (1, 2)),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
);

CREATE TABLE IF NOT EXISTS nblb.request_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id uuid NOT NULL,
    profile_id text NOT NULL,
    key_id uuid NOT NULL REFERENCES nblb.upstream_keys(id),
    outcome text NOT NULL CHECK (outcome IN ('started','succeeded','failed','abandoned_after_restart')),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS request_attempts_request_idx ON nblb.request_attempts (request_id, created_at);
CREATE INDEX IF NOT EXISTS upstream_keys_eligible_idx ON nblb.upstream_keys (enabled, cooldown_until, id);
