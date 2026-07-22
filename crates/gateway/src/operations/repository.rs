//! PostgreSQL queries for sanitized public operations projections.

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use sqlx::{FromRow, PgPool};
use std::collections::{BTreeMap, HashMap, HashSet};
use uuid::Uuid;

use super::dto::{
    MODEL_SPECS, PublicIncident, PublicIncidentUpdate, PublicMetric, PublicMetricPoint, PublicModel,
};

pub(crate) async fn eligible_key_ids(pool: &PgPool, profile_id: &str) -> Result<HashSet<Uuid>> {
    Ok(sqlx::query_scalar::<_, Uuid>(
        "SELECT key.id FROM nblb.upstream_keys key JOIN nblb.profile_probe_receipts proof ON proof.key_id=key.id AND proof.profile_id=$1 AND proof.invalidated_at IS NULL WHERE key.retired=false AND key.enabled=true AND key.verified=true AND (key.cooldown_until IS NULL OR key.cooldown_until<=now()) AND proof.verified_at>=now()-make_interval(secs=>(SELECT proof_freshness_seconds FROM nblb.operations_settings WHERE singleton=true)) ORDER BY key.slot_no",
    )
    .bind(profile_id)
    .fetch_all(pool)
    .await
    .context("load hard-eligible upstream keys")?
    .into_iter()
    .collect())
}

pub(crate) async fn base_eligible_key_ids(pool: &PgPool) -> Result<HashSet<Uuid>> {
    Ok(sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM nblb.upstream_keys WHERE retired=false AND enabled=true AND verified=true AND (cooldown_until IS NULL OR cooldown_until<=now()) ORDER BY slot_no",
    )
    .fetch_all(pool)
    .await
    .context("load base-eligible upstream keys")?
    .into_iter()
    .collect())
}

#[derive(Debug, FromRow)]
struct MetricRow {
    bucket: DateTime<Utc>,
    request_count: i64,
    success_count: i64,
    cancelled_count: i64,
    failover_count: i64,
    eligible_provider_count: Option<i16>,
    duration_sample_count: i64,
    duration_le_250: i64,
    duration_le_500: i64,
    duration_le_1000: i64,
    duration_le_2500: i64,
    duration_le_5000: i64,
    ttfb_sample_count: i64,
    ttfb_le_250: i64,
    ttfb_le_500: i64,
    ttfb_le_1000: i64,
    ttfb_le_2500: i64,
    ttfb_le_5000: i64,
}

fn non_negative(value: i64) -> u64 {
    u64::try_from(value).unwrap_or_default()
}

fn percentile_bucket(sample_count: i64, buckets: &[(i64, u64)]) -> Option<u64> {
    if sample_count <= 0 {
        return None;
    }
    let target = (sample_count.saturating_mul(95).saturating_add(99)) / 100;
    buckets
        .iter()
        .find_map(|(count, boundary)| (*count >= target).then_some(*boundary))
        .or_else(|| {
            buckets
                .last()
                .map(|(_, boundary)| boundary.saturating_add(1))
        })
}

fn metric_from_row(row: &MetricRow) -> PublicMetric {
    let samples = non_negative(row.request_count);
    if samples < 5 {
        return PublicMetric {
            eligible_provider_count: row
                .eligible_provider_count
                .and_then(|value| u8::try_from(value).ok()),
            ..PublicMetric::default()
        };
    }
    PublicMetric {
        sample_count: samples,
        success_rate: (samples > 0)
            .then_some(non_negative(row.success_count) as f64 / samples as f64),
        failover_rate: (samples > 0)
            .then_some(non_negative(row.failover_count) as f64 / samples as f64),
        cancellation_rate: (samples > 0)
            .then_some(non_negative(row.cancelled_count) as f64 / samples as f64),
        latency_p95_ms: percentile_bucket(
            row.duration_sample_count,
            &[
                (row.duration_le_250, 250),
                (row.duration_le_500, 500),
                (row.duration_le_1000, 1_000),
                (row.duration_le_2500, 2_500),
                (row.duration_le_5000, 5_000),
            ],
        ),
        ttfb_p95_ms: percentile_bucket(
            row.ttfb_sample_count,
            &[
                (row.ttfb_le_250, 250),
                (row.ttfb_le_500, 500),
                (row.ttfb_le_1000, 1_000),
                (row.ttfb_le_2500, 2_500),
                (row.ttfb_le_5000, 5_000),
            ],
        ),
        eligible_provider_count: row
            .eligible_provider_count
            .and_then(|value| u8::try_from(value).ok()),
    }
}

pub(crate) async fn metric_summary(pool: &PgPool, window: &str) -> Result<PublicMetric> {
    let rows = metric_rows(pool, window, "1 minute").await?;
    let Some(first) = rows.first() else {
        return Ok(PublicMetric::default());
    };
    let mut total = MetricRow {
        bucket: first.bucket,
        request_count: 0,
        success_count: 0,
        cancelled_count: 0,
        failover_count: 0,
        eligible_provider_count: None,
        duration_sample_count: 0,
        duration_le_250: 0,
        duration_le_500: 0,
        duration_le_1000: 0,
        duration_le_2500: 0,
        duration_le_5000: 0,
        ttfb_sample_count: 0,
        ttfb_le_250: 0,
        ttfb_le_500: 0,
        ttfb_le_1000: 0,
        ttfb_le_2500: 0,
        ttfb_le_5000: 0,
    };
    for row in rows {
        total.request_count += row.request_count;
        total.success_count += row.success_count;
        total.cancelled_count += row.cancelled_count;
        total.failover_count += row.failover_count;
        if row.eligible_provider_count.is_some() {
            total.eligible_provider_count = row.eligible_provider_count;
        }
        total.duration_sample_count += row.duration_sample_count;
        total.duration_le_250 += row.duration_le_250;
        total.duration_le_500 += row.duration_le_500;
        total.duration_le_1000 += row.duration_le_1000;
        total.duration_le_2500 += row.duration_le_2500;
        total.duration_le_5000 += row.duration_le_5000;
        total.ttfb_sample_count += row.ttfb_sample_count;
        total.ttfb_le_250 += row.ttfb_le_250;
        total.ttfb_le_500 += row.ttfb_le_500;
        total.ttfb_le_1000 += row.ttfb_le_1000;
        total.ttfb_le_2500 += row.ttfb_le_2500;
        total.ttfb_le_5000 += row.ttfb_le_5000;
    }
    Ok(metric_from_row(&total))
}

pub(crate) async fn metric_points(
    pool: &PgPool,
    window: &str,
    step: &str,
) -> Result<Vec<PublicMetricPoint>> {
    Ok(metric_rows(pool, window, step)
        .await?
        .into_iter()
        .map(|row| PublicMetricPoint {
            at: row.bucket,
            metric: metric_from_row(&row),
        })
        .collect())
}

async fn metric_rows(pool: &PgPool, window: &str, step: &str) -> Result<Vec<MetricRow>> {
    sqlx::query_as::<_, MetricRow>(
        "WITH metric AS (SELECT date_bin($2::interval,bucket_start,timestamptz '2000-01-01') AS bucket,COALESCE(sum(request_count),0)::bigint AS request_count,COALESCE(sum(request_count) FILTER (WHERE outcome_class='success'),0)::bigint AS success_count,COALESCE(sum(request_count) FILTER (WHERE outcome_class='cancelled'),0)::bigint AS cancelled_count,COALESCE(sum(failover_count),0)::bigint AS failover_count,COALESCE(sum(duration_sample_count),0)::bigint AS duration_sample_count,COALESCE(sum(duration_le_250),0)::bigint AS duration_le_250,COALESCE(sum(duration_le_500),0)::bigint AS duration_le_500,COALESCE(sum(duration_le_1000),0)::bigint AS duration_le_1000,COALESCE(sum(duration_le_2500),0)::bigint AS duration_le_2500,COALESCE(sum(duration_le_5000),0)::bigint AS duration_le_5000,COALESCE(sum(ttfb_sample_count),0)::bigint AS ttfb_sample_count,COALESCE(sum(ttfb_le_250),0)::bigint AS ttfb_le_250,COALESCE(sum(ttfb_le_500),0)::bigint AS ttfb_le_500,COALESCE(sum(ttfb_le_1000),0)::bigint AS ttfb_le_1000,COALESCE(sum(ttfb_le_2500),0)::bigint AS ttfb_le_2500,COALESCE(sum(ttfb_le_5000),0)::bigint AS ttfb_le_5000 FROM nblb.metric_buckets_minute WHERE bucket_start >= now()-$1::interval GROUP BY bucket),capacity AS (SELECT date_bin($2::interval,bucket_start,timestamptz '2000-01-01') AS bucket,(array_agg(eligible_provider_count ORDER BY bucket_start DESC))[1]::smallint AS eligible_provider_count FROM nblb.capacity_buckets_minute WHERE bucket_start >= now()-$1::interval GROUP BY bucket),buckets AS (SELECT bucket FROM metric UNION SELECT bucket FROM capacity) SELECT buckets.bucket,COALESCE(metric.request_count,0)::bigint AS request_count,COALESCE(metric.success_count,0)::bigint AS success_count,COALESCE(metric.cancelled_count,0)::bigint AS cancelled_count,COALESCE(metric.failover_count,0)::bigint AS failover_count,capacity.eligible_provider_count,COALESCE(metric.duration_sample_count,0)::bigint AS duration_sample_count,COALESCE(metric.duration_le_250,0)::bigint AS duration_le_250,COALESCE(metric.duration_le_500,0)::bigint AS duration_le_500,COALESCE(metric.duration_le_1000,0)::bigint AS duration_le_1000,COALESCE(metric.duration_le_2500,0)::bigint AS duration_le_2500,COALESCE(metric.duration_le_5000,0)::bigint AS duration_le_5000,COALESCE(metric.ttfb_sample_count,0)::bigint AS ttfb_sample_count,COALESCE(metric.ttfb_le_250,0)::bigint AS ttfb_le_250,COALESCE(metric.ttfb_le_500,0)::bigint AS ttfb_le_500,COALESCE(metric.ttfb_le_1000,0)::bigint AS ttfb_le_1000,COALESCE(metric.ttfb_le_2500,0)::bigint AS ttfb_le_2500,COALESCE(metric.ttfb_le_5000,0)::bigint AS ttfb_le_5000 FROM buckets LEFT JOIN metric USING(bucket) LEFT JOIN capacity USING(bucket) ORDER BY buckets.bucket",
    )
    .bind(window)
    .bind(step)
    .fetch_all(pool)
    .await
    .context("load aggregate public metrics")
}

pub(crate) async fn public_models(pool: &PgPool) -> Result<Vec<PublicModel>> {
    let eligible_ids = base_eligible_key_ids(pool).await?;
    let freshness_seconds = sqlx::query_scalar::<_, i32>(
        "SELECT proof_freshness_seconds FROM nblb.operations_settings WHERE singleton=true",
    )
    .fetch_optional(pool)
    .await
    .context("load public proof freshness")?
    .map(i64::from)
    .unwrap_or(604_800);
    let rows = sqlx::query_as::<_, (String, Uuid, DateTime<Utc>)>(
        "SELECT profile_id,key_id,verified_at FROM nblb.profile_probe_receipts WHERE invalidated_at IS NULL",
    )
    .fetch_all(pool)
    .await
    .context("load public model proof aggregate")?;
    let advertised =
        sqlx::query_as::<_, (String, bool)>("SELECT profile_id,advertised FROM nblb.model_catalog")
            .fetch_all(pool)
            .await
            .context("load public model catalog")?
            .into_iter()
            .collect::<HashMap<_, _>>();
    let mut proofs: HashMap<String, BTreeMap<Uuid, DateTime<Utc>>> = HashMap::new();
    for (profile, key_id, verified_at) in rows {
        proofs
            .entry(profile)
            .or_default()
            .insert(key_id, verified_at);
    }
    let now = Utc::now();
    Ok(MODEL_SPECS
        .iter()
        .map(|spec| {
            let profile_proofs = proofs.get(spec.id);
            let verified_count = profile_proofs.map_or(0, |items| {
                items
                    .values()
                    .filter(|verified_at| {
                        now.signed_duration_since(**verified_at).num_seconds() <= freshness_seconds
                    })
                    .count()
            });
            let available_now = profile_proofs.is_some_and(|items| {
                items.iter().any(|(key_id, verified_at)| {
                    eligible_ids.contains(key_id)
                        && now.signed_duration_since(*verified_at).num_seconds()
                            <= freshness_seconds
                })
            });
            let proof_status = if verified_count >= 2 {
                "pair_verified"
            } else if verified_count == 1 {
                "provider_verified"
            } else if eligible_ids.is_empty() {
                "unavailable"
            } else {
                "proof_required"
            };
            let verified_age_seconds = profile_proofs
                .and_then(|items| items.values().max())
                .and_then(|verified_at| {
                    u64::try_from(now.signed_duration_since(*verified_at).num_seconds().max(0)).ok()
                });
            PublicModel {
                id: spec.id,
                endpoint: spec.endpoint,
                input_modalities: spec.input_modalities,
                output_modalities: spec.output_modalities,
                streaming: spec.streaming,
                tool_calling: spec.tool_calling,
                advertised: advertised.get(spec.id).copied().unwrap_or(false),
                proof_status,
                available_now,
                verified_age_seconds,
            }
        })
        .collect())
}

#[derive(Debug, FromRow)]
struct IncidentRow {
    id: Uuid,
    slug: String,
    title: String,
    status: String,
    severity: String,
    started_at: DateTime<Utc>,
    resolved_at: Option<DateTime<Utc>>,
}

pub(crate) async fn public_incidents(
    pool: &PgPool,
    slug: Option<&str>,
) -> Result<Vec<PublicIncident>> {
    let incidents = sqlx::query_as::<_, IncidentRow>(
        "SELECT id,slug,title,status,severity,started_at,resolved_at FROM nblb.incidents WHERE public=true AND ($1::text IS NULL OR slug=$1) ORDER BY started_at DESC,id DESC LIMIT 100",
    )
    .bind(slug)
    .fetch_all(pool)
    .await
    .context("load sanitized public incidents")?;
    let ids: Vec<Uuid> = incidents.iter().map(|item| item.id).collect();
    let updates = if ids.is_empty() {
        Vec::new()
    } else {
        sqlx::query_as::<_, (Uuid, String, String, DateTime<Utc>)>(
            "SELECT incident_id,status,public_message,published_at FROM nblb.incident_updates WHERE incident_id=ANY($1) ORDER BY published_at,id",
        )
        .bind(&ids)
        .fetch_all(pool)
        .await
        .context("load sanitized public incident updates")?
    };
    let mut by_incident: HashMap<Uuid, Vec<PublicIncidentUpdate>> = HashMap::new();
    for (incident_id, status, message, published_at) in updates {
        by_incident
            .entry(incident_id)
            .or_default()
            .push(PublicIncidentUpdate {
                status,
                message,
                published_at,
            });
    }
    Ok(incidents
        .into_iter()
        .map(|item| PublicIncident {
            slug: item.slug,
            title: item.title,
            status: item.status,
            severity: item.severity,
            started_at: item.started_at,
            resolved_at: item.resolved_at,
            updates: by_incident.remove(&item.id).unwrap_or_default(),
        })
        .collect())
}

#[cfg(test)]
mod metric_query_tests {
    use super::metric_points;
    use chrono::{Duration, Timelike, Utc};

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn capacity_only_bucket_uses_its_latest_observation(pool: sqlx::PgPool) {
        let now = Utc::now() - Duration::hours(1);
        let bucket = now
            .with_minute(0)
            .and_then(|value| value.with_second(0))
            .and_then(|value| value.with_nanosecond(0))
            .expect("hour bucket");
        for (offset, eligible) in [(1, 0_i16), (20, 2_i16), (40, 1_i16)] {
            sqlx::query(
                "INSERT INTO nblb.capacity_buckets_minute(bucket_start,eligible_provider_count) VALUES($1,$2)",
            )
            .bind(bucket + Duration::minutes(offset))
            .bind(eligible)
            .execute(&pool)
            .await
            .expect("seed capacity observation");
        }

        let points = metric_points(&pool, "24 hours", "1 hour")
            .await
            .expect("load capacity-only point");
        let point = points
            .iter()
            .find(|point| point.at == bucket)
            .expect("capacity-only bucket must be present");
        assert_eq!(point.metric.sample_count, 0);
        assert_eq!(point.metric.eligible_provider_count, Some(1));
    }
}
