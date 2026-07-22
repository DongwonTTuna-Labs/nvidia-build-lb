-- Make QA execution authority database-global and keep Hermes evidence live-only.

-- A restart may have left queued work that was committed before its in-process
-- runner was spawned. Older duplicate active rows cannot remain authoritative.
WITH ranked AS (
    SELECT id,
           row_number() OVER (ORDER BY created_at DESC, id DESC) AS active_rank
    FROM nblb.qa_runs
    WHERE status IN ('queued', 'running')
)
UPDATE nblb.qa_cases AS qa_case
SET status = 'failed',
    evidence = jsonb_build_object('error_code', 'superseded_active_qa_run'),
    started_at = COALESCE(qa_case.started_at, now()),
    finished_at = now()
FROM ranked
WHERE qa_case.run_id = ranked.id
  AND ranked.active_rank > 1
  AND qa_case.status IN ('pending', 'running');

WITH ranked AS (
    SELECT id,
           row_number() OVER (ORDER BY created_at DESC, id DESC) AS active_rank
    FROM nblb.qa_runs
    WHERE status IN ('queued', 'running')
)
UPDATE nblb.qa_runs AS run
SET status = 'failed',
    started_at = COALESCE(run.started_at, now()),
    finished_at = now()
FROM ranked
WHERE run.id = ranked.id
  AND ranked.active_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS qa_runs_single_active_idx
    ON nblb.qa_runs ((true))
    WHERE status IN ('queued', 'running');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'qa_runs_hermes_live_contract'
          AND conrelid = 'nblb.qa_runs'::regclass
    ) THEN
        ALTER TABLE nblb.qa_runs
            ADD CONSTRAINT qa_runs_hermes_live_contract
            CHECK (suite <> 'hermes-e2e' OR live) NOT VALID;
    END IF;
END
$$;
