-- Canonical PostgreSQL contracts for the Rust serving surface.
CREATE OR REPLACE FUNCTION nblb.scopes_are_canonical(value text[])
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    allowed constant text[] := ARRAY[
        'models:read',
        'chat:write',
        'embeddings:write',
        'images:write',
        'audio:write',
        'media:write'
    ];
    item text;
    position integer;
    previous integer := 0;
BEGIN
    IF value IS NULL OR cardinality(value) NOT BETWEEN 1 AND cardinality(allowed) THEN
        RETURN false;
    END IF;
    FOREACH item IN ARRAY value LOOP
        position := array_position(allowed, item);
        IF position IS NULL OR position <= previous THEN
            RETURN false;
        END IF;
        previous := position;
    END LOOP;
    RETURN true;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'upstream_keys_label_contract'
          AND conrelid = 'nblb.upstream_keys'::regclass
    ) THEN
        ALTER TABLE nblb.upstream_keys
            ADD CONSTRAINT upstream_keys_label_contract
            CHECK (length(label) BETWEEN 1 AND 128);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'downstream_credentials_canonical_scopes'
          AND conrelid = 'nblb.downstream_credentials'::regclass
    ) THEN
        ALTER TABLE nblb.downstream_credentials
            ADD CONSTRAINT downstream_credentials_canonical_scopes
            CHECK (
                nblb.scopes_are_canonical(scopes)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'request_attempts_terminal_contract'
          AND conrelid = 'nblb.request_attempts'::regclass
    ) THEN
        ALTER TABLE nblb.request_attempts
            ADD CONSTRAINT request_attempts_terminal_contract
            CHECK (
                (outcome = 'started' AND finished_at IS NULL)
                OR (outcome <> 'started' AND finished_at IS NOT NULL)
            );
    END IF;
END $$;

ALTER TABLE nblb.request_attempts
    DROP CONSTRAINT IF EXISTS request_attempts_profile_fk;
ALTER TABLE nblb.request_attempts
    ADD CONSTRAINT request_attempts_profile_fk
    FOREIGN KEY (profile_id) REFERENCES nblb.routing_state(profile_id)
    ON UPDATE CASCADE ON DELETE RESTRICT;
