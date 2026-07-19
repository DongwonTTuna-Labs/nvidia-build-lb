-- Client disconnects are terminal request attempts and must not leave a
-- started row behind when the stream guard is dropped.
ALTER TABLE nblb.request_attempts
    DROP CONSTRAINT IF EXISTS request_attempts_outcome_check;

ALTER TABLE nblb.request_attempts
    ADD CONSTRAINT request_attempts_outcome_check
    CHECK (outcome IN ('started', 'succeeded', 'failed', 'cancelled', 'abandoned_after_restart'));
