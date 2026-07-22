WITH selected AS (
    SELECT
        id,
        endpoint,
        profile_id,
        outcome,
        status_code,
        duration_ms,
        ttfb_ms,
        failover_count,
        date_trunc('minute', finished_at) AS bucket_start
    FROM nblb.proxy_requests
    WHERE finished_at IS NOT NULL
      AND rolled_up_at IS NULL
    ORDER BY finished_at, id
    LIMIT $1
    FOR UPDATE SKIP LOCKED
),
aggregated AS (
    SELECT
        bucket_start,
        endpoint,
        profile_id,
        CASE
            WHEN outcome = 'succeeded' THEN 'success'
            WHEN outcome = 'cancelled' THEN 'cancelled'
            WHEN outcome = 'rejected' THEN 'rejected'
            -- A provider-side 4xx such as 401/403/429 is still an upstream
            -- failure. Client validation failures use the explicit rejected
            -- outcome, so status code alone must not misclassify rate limits
            -- as downstream client errors.
            WHEN outcome IN ('failed', 'abandoned_after_restart') THEN 'upstream_error'
            WHEN status_code BETWEEN 400 AND 499 THEN 'client_error'
            ELSE 'upstream_error'
        END AS outcome_class,
        count(*)::bigint AS request_count,
        sum(failover_count)::bigint AS failover_count,
        sum(COALESCE(duration_ms, 0))::numeric AS duration_sum_ms,
        sum(COALESCE(ttfb_ms, 0))::numeric AS ttfb_sum_ms,
        count(duration_ms)::bigint AS duration_sample_count,
        count(ttfb_ms)::bigint AS ttfb_sample_count,
        count(*) FILTER (WHERE duration_ms <= 250)::bigint AS duration_le_250,
        count(*) FILTER (WHERE duration_ms <= 500)::bigint AS duration_le_500,
        count(*) FILTER (WHERE duration_ms <= 1000)::bigint AS duration_le_1000,
        count(*) FILTER (WHERE duration_ms <= 2500)::bigint AS duration_le_2500,
        count(*) FILTER (WHERE duration_ms <= 5000)::bigint AS duration_le_5000,
        count(*) FILTER (WHERE duration_ms > 5000)::bigint AS duration_gt_5000,
        count(*) FILTER (WHERE ttfb_ms <= 250)::bigint AS ttfb_le_250,
        count(*) FILTER (WHERE ttfb_ms <= 500)::bigint AS ttfb_le_500,
        count(*) FILTER (WHERE ttfb_ms <= 1000)::bigint AS ttfb_le_1000,
        count(*) FILTER (WHERE ttfb_ms <= 2500)::bigint AS ttfb_le_2500,
        count(*) FILTER (WHERE ttfb_ms <= 5000)::bigint AS ttfb_le_5000,
        count(*) FILTER (WHERE ttfb_ms > 5000)::bigint AS ttfb_gt_5000
    FROM selected
    GROUP BY bucket_start, endpoint, profile_id, outcome_class
),
upserted AS (
    INSERT INTO nblb.metric_buckets_minute (
        bucket_start,
        endpoint,
        profile_id,
        outcome_class,
        request_count,
        failover_count,
        duration_sum_ms,
        ttfb_sum_ms,
        duration_sample_count,
        ttfb_sample_count,
        duration_le_250,
        duration_le_500,
        duration_le_1000,
        duration_le_2500,
        duration_le_5000,
        duration_gt_5000,
        ttfb_le_250,
        ttfb_le_500,
        ttfb_le_1000,
        ttfb_le_2500,
        ttfb_le_5000,
        ttfb_gt_5000
    )
    SELECT
        bucket_start,
        endpoint,
        profile_id,
        outcome_class,
        request_count,
        failover_count,
        duration_sum_ms,
        ttfb_sum_ms,
        duration_sample_count,
        ttfb_sample_count,
        duration_le_250,
        duration_le_500,
        duration_le_1000,
        duration_le_2500,
        duration_le_5000,
        duration_gt_5000,
        ttfb_le_250,
        ttfb_le_500,
        ttfb_le_1000,
        ttfb_le_2500,
        ttfb_le_5000,
        ttfb_gt_5000
    FROM aggregated
    ON CONFLICT (bucket_start, endpoint, profile_id, outcome_class)
    DO UPDATE SET
        request_count = nblb.metric_buckets_minute.request_count + EXCLUDED.request_count,
        failover_count = nblb.metric_buckets_minute.failover_count + EXCLUDED.failover_count,
        duration_sum_ms = nblb.metric_buckets_minute.duration_sum_ms + EXCLUDED.duration_sum_ms,
        ttfb_sum_ms = nblb.metric_buckets_minute.ttfb_sum_ms + EXCLUDED.ttfb_sum_ms,
        duration_sample_count = nblb.metric_buckets_minute.duration_sample_count + EXCLUDED.duration_sample_count,
        ttfb_sample_count = nblb.metric_buckets_minute.ttfb_sample_count + EXCLUDED.ttfb_sample_count,
        duration_le_250 = nblb.metric_buckets_minute.duration_le_250 + EXCLUDED.duration_le_250,
        duration_le_500 = nblb.metric_buckets_minute.duration_le_500 + EXCLUDED.duration_le_500,
        duration_le_1000 = nblb.metric_buckets_minute.duration_le_1000 + EXCLUDED.duration_le_1000,
        duration_le_2500 = nblb.metric_buckets_minute.duration_le_2500 + EXCLUDED.duration_le_2500,
        duration_le_5000 = nblb.metric_buckets_minute.duration_le_5000 + EXCLUDED.duration_le_5000,
        duration_gt_5000 = nblb.metric_buckets_minute.duration_gt_5000 + EXCLUDED.duration_gt_5000,
        ttfb_le_250 = nblb.metric_buckets_minute.ttfb_le_250 + EXCLUDED.ttfb_le_250,
        ttfb_le_500 = nblb.metric_buckets_minute.ttfb_le_500 + EXCLUDED.ttfb_le_500,
        ttfb_le_1000 = nblb.metric_buckets_minute.ttfb_le_1000 + EXCLUDED.ttfb_le_1000,
        ttfb_le_2500 = nblb.metric_buckets_minute.ttfb_le_2500 + EXCLUDED.ttfb_le_2500,
        ttfb_le_5000 = nblb.metric_buckets_minute.ttfb_le_5000 + EXCLUDED.ttfb_le_5000,
        ttfb_gt_5000 = nblb.metric_buckets_minute.ttfb_gt_5000 + EXCLUDED.ttfb_gt_5000
    RETURNING 1
),
marked AS (
    UPDATE nblb.proxy_requests
    SET rolled_up_at = now()
    WHERE id IN (SELECT id FROM selected)
    RETURNING 1
)
SELECT count(*) FROM marked
