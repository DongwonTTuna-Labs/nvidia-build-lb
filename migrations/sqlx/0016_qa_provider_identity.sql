-- A boolean live flag is not sufficient evidence that a QA run exercised the
-- hosted NVIDIA service. Preserve the provider provenance on every run and
-- make pre-provenance live history non-authoritative until it is rerun.

ALTER TABLE nblb.qa_runs
    ADD COLUMN IF NOT EXISTS provider_identity text;

UPDATE nblb.qa_runs
SET provider_identity = CASE WHEN live THEN 'unverified' ELSE 'fake' END
WHERE provider_identity IS NULL;

ALTER TABLE nblb.qa_runs
    ALTER COLUMN provider_identity SET DEFAULT 'fake',
    ALTER COLUMN provider_identity SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'qa_runs_provider_identity_contract'
          AND conrelid = 'nblb.qa_runs'::regclass
    ) THEN
        ALTER TABLE nblb.qa_runs
            ADD CONSTRAINT qa_runs_provider_identity_contract CHECK (
                (live = false AND provider_identity = 'fake')
                OR (
                    live = true
                    AND provider_identity IN ('nvidia_hosted', 'unverified')
                )
            );
    END IF;
END
$$;
