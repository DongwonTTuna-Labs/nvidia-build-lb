//! Typed PostgreSQL projections and transactions for the admin v2 surface.

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool, Postgres, Transaction};
use std::{
    collections::{HashMap, HashSet},
    error::Error,
    fmt,
};
use uuid::Uuid;

use super::dto::{
    AdminModel, Attempt, AuditEvent, Client, Incident, IncidentUpdate, MODEL_SPECS, ProbeRun,
    ProfileProof, ProxyRequest, QaCase, QaRun, RequestAggregate, RoutingPolicy, Settings, Upstream,
};

fn as_u64(value: i64) -> u64 {
    u64::try_from(value.max(0)).unwrap_or_default()
}

fn as_u32(value: i32) -> u32 {
    u32::try_from(value.max(0)).unwrap_or_default()
}

fn as_u16(value: i16) -> u16 {
    u16::try_from(value.max(0)).unwrap_or_default()
}

fn as_u8(value: i16) -> u8 {
    u8::try_from(value.max(0)).unwrap_or_default()
}

#[derive(Debug, FromRow)]
struct ProofRow {
    profile_id: String,
    key_id: Uuid,
    verified_at: DateTime<Utc>,
}

async fn proof_rows(pool: &PgPool) -> Result<Vec<ProofRow>> {
    sqlx::query_as::<_, ProofRow>(
        "SELECT profile_id,key_id,verified_at FROM nblb.profile_probe_receipts WHERE invalidated_at IS NULL ORDER BY profile_id,key_id",
    )
    .fetch_all(pool)
    .await
    .context("load admin profile proofs")
}

async fn proof_freshness_seconds(pool: &PgPool) -> Result<i64> {
    Ok(sqlx::query_scalar::<_, i32>(
        "SELECT proof_freshness_seconds FROM nblb.operations_settings WHERE singleton=true",
    )
    .fetch_optional(pool)
    .await
    .context("load proof freshness setting")?
    .map(i64::from)
    .unwrap_or(604_800))
}

fn profile_proofs(
    rows: &[ProofRow],
    eligible_ids: &HashSet<Uuid>,
    only_key: Option<Uuid>,
    freshness_seconds: i64,
) -> Vec<ProfileProof> {
    let now = Utc::now();
    MODEL_SPECS
        .iter()
        .map(|spec| {
            let matching: Vec<&ProofRow> = rows
                .iter()
                .filter(|row| {
                    row.profile_id == spec.id && only_key.is_none_or(|key_id| key_id == row.key_id)
                })
                .collect();
            let verified_key_count = u8::try_from(
                matching
                    .iter()
                    .filter(|row| {
                        now.signed_duration_since(row.verified_at).num_seconds()
                            <= freshness_seconds
                    })
                    .count()
                    .min(2),
            )
            .unwrap_or_default();
            let last_verified_at = matching.iter().map(|row| row.verified_at).max();
            let status = if verified_key_count >= 2 {
                "pair_verified"
            } else if verified_key_count == 1 {
                "provider_verified"
            } else if eligible_ids.is_empty() {
                "unavailable"
            } else {
                "proof_required"
            };
            ProfileProof {
                profile_id: spec.id.into(),
                status,
                verified_key_count,
                last_verified_at,
                stale: last_verified_at.is_some_and(|verified_at| {
                    now.signed_duration_since(verified_at).num_seconds() > freshness_seconds
                }),
            }
        })
        .collect()
}

#[derive(Debug, FromRow)]
struct UpstreamRow {
    id: Uuid,
    slot_no: i16,
    label: String,
    enabled: bool,
    verified: bool,
    retired: bool,
    cooldown_until: Option<DateTime<Utc>>,
    request_count: i64,
    failure_count: i64,
}

pub(crate) async fn upstreams(pool: &PgPool) -> Result<Vec<Upstream>> {
    let rows = sqlx::query_as::<_, UpstreamRow>(
        "SELECT id,slot_no,label,enabled,verified,retired,cooldown_until,request_count,failure_count FROM nblb.upstream_keys ORDER BY retired,slot_no,created_at,id",
    )
    .fetch_all(pool)
    .await
    .context("load admin upstreams")?;
    let proofs = proof_rows(pool).await?;
    let freshness_seconds = proof_freshness_seconds(pool).await?;
    let now = Utc::now();
    let eligible_ids: HashSet<Uuid> = rows
        .iter()
        .filter(|row| {
            !row.retired
                && row.enabled
                && row.verified
                && row.cooldown_until.is_none_or(|until| until <= now)
                && proofs.iter().any(|proof| {
                    proof.key_id == row.id
                        && proof.profile_id == "z-ai/glm-5.2"
                        && now.signed_duration_since(proof.verified_at).num_seconds()
                            <= freshness_seconds
                })
        })
        .map(|row| row.id)
        .collect();
    Ok(rows
        .into_iter()
        .map(|row| Upstream {
            id: row.id,
            slot_no: as_u8(row.slot_no),
            label: row.label,
            enabled: row.enabled,
            verified: row.verified,
            retired: row.retired,
            eligible_now: eligible_ids.contains(&row.id),
            cooldown_until: row.cooldown_until,
            request_count: as_u64(row.request_count),
            failure_count: as_u64(row.failure_count),
            proofs: profile_proofs(&proofs, &eligible_ids, Some(row.id), freshness_seconds),
        })
        .collect())
}

pub(crate) async fn upstream(pool: &PgPool, id: Uuid) -> Result<Option<Upstream>> {
    Ok(upstreams(pool)
        .await?
        .into_iter()
        .find(|item| item.id == id))
}

pub(crate) async fn upstream_ids_exist(pool: &PgPool, ids: &[Uuid]) -> Result<bool> {
    let existing =
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.upstream_keys WHERE id=ANY($1)")
            .bind(ids)
            .fetch_one(pool)
            .await
            .context("validate admin upstream IDs")?;
    Ok(usize::try_from(existing).is_ok_and(|count| count == ids.len()))
}

#[derive(Debug, FromRow)]
struct ClientRow {
    id: Uuid,
    label: String,
    scopes: Vec<String>,
    active: bool,
    key_prefix: String,
    expires_at: Option<DateTime<Utc>>,
    model_allowlist: Option<Vec<String>>,
    rpm_limit: Option<i32>,
    max_concurrency: Option<i16>,
    request_limit_day: Option<i32>,
    request_count: i64,
    last_used_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    revoked_at: Option<DateTime<Utc>>,
}

fn client_from_row(row: ClientRow) -> Client {
    Client {
        id: row.id,
        label: row.label,
        scopes: row.scopes,
        active: row.active,
        prefix: row.key_prefix,
        expires_at: row.expires_at,
        model_allowlist: row.model_allowlist,
        rpm_limit: row.rpm_limit.map(as_u32),
        max_concurrency: row.max_concurrency.map(|value| u32::from(as_u16(value))),
        request_limit_day: row.request_limit_day.map(as_u32),
        request_count: as_u64(row.request_count),
        last_used_at: row.last_used_at,
        created_at: row.created_at,
        revoked_at: row.revoked_at,
    }
}

pub(crate) async fn clients(pool: &PgPool) -> Result<Vec<Client>> {
    Ok(sqlx::query_as::<_, ClientRow>(
        "SELECT id,label,scopes,active,key_prefix,expires_at,model_allowlist,rpm_limit,max_concurrency,request_limit_day,request_count,last_used_at,created_at,revoked_at FROM nblb.downstream_credentials ORDER BY created_at DESC,id DESC",
    )
    .fetch_all(pool)
    .await
    .context("load admin clients")?
    .into_iter()
    .map(client_from_row)
    .collect())
}

pub(crate) async fn clients_page(
    pool: &PgPool,
    before: Option<(DateTime<Utc>, Uuid)>,
    limit: i64,
) -> Result<Vec<Client>> {
    let (before_at, before_id) = before.map_or((None, None), |(at, id)| (Some(at), Some(id)));
    Ok(sqlx::query_as::<_, ClientRow>(
        "SELECT id,label,scopes,active,key_prefix,expires_at,model_allowlist,rpm_limit,max_concurrency,request_limit_day,request_count,last_used_at,created_at,revoked_at FROM nblb.downstream_credentials WHERE ($1::timestamptz IS NULL OR (created_at,id)<($1,$2)) ORDER BY created_at DESC,id DESC LIMIT $3",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(limit.clamp(1, 101))
    .fetch_all(pool)
    .await
    .context("load paginated admin clients")?
    .into_iter()
    .map(client_from_row)
    .collect())
}

pub(crate) async fn client(pool: &PgPool, id: Uuid) -> Result<Option<Client>> {
    let row = sqlx::query_as::<_, ClientRow>(
        "SELECT id,label,scopes,active,key_prefix,expires_at,model_allowlist,rpm_limit,max_concurrency,request_limit_day,request_count,last_used_at,created_at,revoked_at FROM nblb.downstream_credentials WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
    .context("load admin client")?;
    Ok(row.map(client_from_row))
}

#[derive(Debug, FromRow)]
struct RequestRow {
    request_id: Uuid,
    downstream_credential_id: Option<Uuid>,
    endpoint: String,
    profile_id: String,
    modality: String,
    stream: bool,
    outcome: String,
    status_code: Option<i16>,
    error_class: Option<String>,
    duration_ms: Option<i64>,
    ttfb_ms: Option<i64>,
    failover_count: i16,
    started_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
}

fn request_from_row(row: RequestRow, attempts: Option<Vec<Attempt>>) -> ProxyRequest {
    ProxyRequest {
        request_id: row.request_id,
        client_id: row.downstream_credential_id,
        endpoint: row.endpoint,
        profile_id: row.profile_id,
        modality: row.modality,
        stream: row.stream,
        outcome: row.outcome,
        status_code: row.status_code.map(as_u16),
        error_class: row.error_class,
        duration_ms: row.duration_ms.map(as_u64),
        ttfb_ms: row.ttfb_ms.map(as_u64),
        failover_count: as_u8(row.failover_count),
        started_at: row.started_at,
        finished_at: row.finished_at,
        attempts,
    }
}

pub(crate) async fn requests_page(
    pool: &PgPool,
    before: Option<(DateTime<Utc>, Uuid)>,
    limit: i64,
    filters: &RequestFilters,
) -> Result<Vec<ProxyRequest>> {
    let (before_at, before_id) = before.map_or((None, None), |(at, id)| (Some(at), Some(id)));
    Ok(sqlx::query_as::<_, RequestRow>(
        "SELECT request_id,downstream_credential_id,endpoint,profile_id,modality,stream,outcome,status_code,error_class,duration_ms,ttfb_ms,failover_count,started_at,finished_at FROM nblb.proxy_requests WHERE ($1::timestamptz IS NULL OR (started_at,request_id)<($1,$2)) AND ($3::uuid IS NULL OR request_id=$3) AND ($4::uuid IS NULL OR downstream_credential_id=$4) AND ($5::text IS NULL OR profile_id=$5) AND ($6::text IS NULL OR outcome=$6) AND ($7::text IS NULL OR endpoint=$7) AND ($8::timestamptz IS NULL OR started_at>=$8) AND ($9::timestamptz IS NULL OR started_at<=$9) ORDER BY started_at DESC,request_id DESC LIMIT $10",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(filters.request_id)
    .bind(filters.client_id)
    .bind(filters.profile.as_deref())
    .bind(filters.outcome.as_deref())
    .bind(filters.endpoint.as_deref())
    .bind(filters.since)
    .bind(filters.until)
    .bind(limit.clamp(1, 101))
    .fetch_all(pool)
    .await
    .context("load admin requests")?
    .into_iter()
    .map(|row| request_from_row(row, None))
    .collect())
}

#[derive(Clone, Debug, Default)]
pub(crate) struct RequestFilters {
    pub(crate) request_id: Option<Uuid>,
    pub(crate) client_id: Option<Uuid>,
    pub(crate) profile: Option<String>,
    pub(crate) outcome: Option<String>,
    pub(crate) endpoint: Option<String>,
    pub(crate) since: Option<DateTime<Utc>>,
    pub(crate) until: Option<DateTime<Utc>>,
}

pub(crate) async fn request_aggregate(
    pool: &PgPool,
    filters: &RequestFilters,
) -> Result<RequestAggregate> {
    let row = sqlx::query_as::<_, (i64, i64, i64, i64, i64, Option<i64>)>(
        "SELECT count(*)::bigint,count(*) FILTER (WHERE outcome='succeeded')::bigint,count(*) FILTER (WHERE outcome IN ('failed','rejected','abandoned_after_restart'))::bigint,count(*) FILTER (WHERE outcome='cancelled')::bigint,count(*) FILTER (WHERE outcome='started')::bigint,round(avg(duration_ms))::bigint FROM nblb.proxy_requests WHERE ($1::uuid IS NULL OR request_id=$1) AND ($2::uuid IS NULL OR downstream_credential_id=$2) AND ($3::text IS NULL OR profile_id=$3) AND ($4::text IS NULL OR outcome=$4) AND ($5::text IS NULL OR endpoint=$5) AND ($6::timestamptz IS NULL OR started_at>=$6) AND ($7::timestamptz IS NULL OR started_at<=$7)",
    )
    .bind(filters.request_id)
    .bind(filters.client_id)
    .bind(filters.profile.as_deref())
    .bind(filters.outcome.as_deref())
    .bind(filters.endpoint.as_deref())
    .bind(filters.since)
    .bind(filters.until)
    .fetch_one(pool)
    .await
    .context("aggregate admin requests")?;
    Ok(RequestAggregate {
        total: as_u64(row.0),
        succeeded: as_u64(row.1),
        failed: as_u64(row.2),
        cancelled: as_u64(row.3),
        active: as_u64(row.4),
        average_duration_ms: row.5.map(as_u64),
    })
}

#[derive(Debug, FromRow)]
struct AttemptRow {
    id: Uuid,
    attempt_no: Option<i16>,
    key_id: Uuid,
    slot_no: i16,
    upstream_label: String,
    outcome: String,
    status_code: Option<i16>,
    error_class: Option<String>,
    latency_ms: Option<i64>,
    ttfb_ms: Option<i64>,
    response_started: bool,
    cooldown_applied_until: Option<DateTime<Utc>>,
    bytes_out: Option<i64>,
    created_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
}

pub(crate) async fn request(pool: &PgPool, id: Uuid) -> Result<Option<ProxyRequest>> {
    let row = sqlx::query_as::<_, RequestRow>(
        "SELECT request_id,downstream_credential_id,endpoint,profile_id,modality,stream,outcome,status_code,error_class,duration_ms,ttfb_ms,failover_count,started_at,finished_at FROM nblb.proxy_requests WHERE request_id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
    .context("load admin request")?;
    let Some(row) = row else { return Ok(None) };
    let attempts = sqlx::query_as::<_, AttemptRow>(
        "SELECT attempt.id,attempt.attempt_no,attempt.key_id,key.slot_no,key.label AS upstream_label,attempt.outcome,attempt.status_code,attempt.error_class,attempt.latency_ms,attempt.ttfb_ms,attempt.response_started,attempt.cooldown_applied_until,attempt.bytes_out,attempt.created_at,attempt.finished_at FROM nblb.request_attempts attempt JOIN nblb.upstream_keys key ON key.id=attempt.key_id WHERE attempt.request_id=$1 ORDER BY attempt.attempt_no,attempt.created_at,attempt.id",
    )
    .bind(id)
    .fetch_all(pool)
    .await
    .context("load admin request attempts")?
    .into_iter()
    .map(|attempt| Attempt {
        id: attempt.id,
        attempt_no: as_u8(attempt.attempt_no.unwrap_or(0)),
        upstream_id: attempt.key_id,
        upstream_slot_no: as_u8(attempt.slot_no),
        upstream_label: attempt.upstream_label,
        outcome: attempt.outcome,
        status_code: attempt.status_code.map(as_u16),
        error_class: attempt.error_class,
        latency_ms: attempt.latency_ms.map(as_u64),
        ttfb_ms: attempt.ttfb_ms.map(as_u64),
        response_started: attempt.response_started,
        cooldown_applied_until: attempt.cooldown_applied_until,
        bytes_out: attempt.bytes_out.map(as_u64),
        started_at: attempt.created_at,
        finished_at: attempt.finished_at,
    })
    .collect();
    Ok(Some(request_from_row(row, Some(attempts))))
}

#[derive(Debug, FromRow)]
struct ProbeRow {
    id: Uuid,
    kind: String,
    upstream_id: Option<Uuid>,
    profile_id: Option<String>,
    status: String,
    status_code: Option<i16>,
    latency_ms: Option<i64>,
    error_class: Option<String>,
    billable: bool,
    requested_by: String,
    created_at: DateTime<Utc>,
    started_at: Option<DateTime<Utc>>,
    finished_at: Option<DateTime<Utc>>,
}

fn probe_from_row(row: ProbeRow) -> ProbeRun {
    ProbeRun {
        id: row.id,
        kind: row.kind,
        upstream_id: row.upstream_id,
        profile_id: row.profile_id,
        status: row.status,
        status_code: row.status_code.map(as_u16),
        latency_ms: row.latency_ms.map(as_u64),
        error_class: row.error_class,
        billable: row.billable,
        requested_by: row.requested_by,
        created_at: row.created_at,
        started_at: row.started_at,
        finished_at: row.finished_at,
    }
}

pub(crate) async fn probes_page(
    pool: &PgPool,
    before: Option<(DateTime<Utc>, Uuid)>,
    limit: i64,
) -> Result<Vec<ProbeRun>> {
    let (before_at, before_id) = before.map_or((None, None), |(at, id)| (Some(at), Some(id)));
    Ok(sqlx::query_as::<_, ProbeRow>(
        "SELECT id,kind,upstream_id,profile_id,status,status_code,latency_ms,error_class,billable,requested_by,created_at,started_at,finished_at FROM nblb.probe_runs WHERE ($1::timestamptz IS NULL OR (created_at,id)<($1,$2)) ORDER BY created_at DESC,id DESC LIMIT $3",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(limit.clamp(1, 101))
    .fetch_all(pool)
    .await
    .context("load admin probe runs")?
    .into_iter()
    .map(probe_from_row)
    .collect())
}

pub(crate) async fn probe(pool: &PgPool, id: Uuid) -> Result<Option<ProbeRun>> {
    Ok(sqlx::query_as::<_, ProbeRow>(
        "SELECT id,kind,upstream_id,profile_id,status,status_code,latency_ms,error_class,billable,requested_by,created_at,started_at,finished_at FROM nblb.probe_runs WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
    .context("load admin probe run")?
    .map(probe_from_row))
}

pub(crate) async fn admin_models(pool: &PgPool) -> Result<Vec<AdminModel>> {
    let eligible_ids = super::repository::base_eligible_key_ids(pool).await?;
    let rows = proof_rows(pool).await?;
    let freshness_seconds = proof_freshness_seconds(pool).await?;
    let advertised =
        sqlx::query_as::<_, (String, bool)>("SELECT profile_id,advertised FROM nblb.model_catalog")
            .fetch_all(pool)
            .await
            .context("load admin model catalog")?
            .into_iter()
            .collect::<HashMap<_, _>>();
    let now = Utc::now();
    Ok(MODEL_SPECS
        .iter()
        .map(|spec| {
            let matching: Vec<&ProofRow> = rows
                .iter()
                .filter(|row| row.profile_id == spec.id)
                .collect();
            let verified_key_count = u8::try_from(
                matching
                    .iter()
                    .filter(|row| {
                        now.signed_duration_since(row.verified_at).num_seconds()
                            <= freshness_seconds
                    })
                    .count()
                    .min(2),
            )
            .unwrap_or_default();
            let available_now = matching.iter().any(|row| {
                eligible_ids.contains(&row.key_id)
                    && now.signed_duration_since(row.verified_at).num_seconds() <= freshness_seconds
            });
            let proof_status = if verified_key_count >= 2 {
                "pair_verified"
            } else if verified_key_count == 1 {
                "provider_verified"
            } else if eligible_ids.is_empty() {
                "unavailable"
            } else {
                "proof_required"
            };
            let last_verified = matching.iter().map(|row| row.verified_at).max();
            AdminModel {
                id: spec.id,
                endpoint: spec.endpoint,
                input_modalities: spec.input_modalities,
                output_modalities: spec.output_modalities,
                streaming: spec.streaming,
                tool_calling: spec.tool_calling,
                advertised: advertised.get(spec.id).copied().unwrap_or(false),
                proof_status,
                verified_key_count,
                available_now,
                verified_age_seconds: last_verified.and_then(|at| {
                    u64::try_from(now.signed_duration_since(at).num_seconds().max(0)).ok()
                }),
                proofs: profile_proofs(&rows, &eligible_ids, None, freshness_seconds)
                    .into_iter()
                    .filter(|proof| proof.profile_id == spec.id)
                    .collect(),
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
    public: bool,
    started_at: DateTime<Utc>,
    resolved_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}

async fn incident_updates(
    pool: &PgPool,
    ids: &[Uuid],
) -> Result<HashMap<Uuid, Vec<IncidentUpdate>>> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query_as::<_, (Uuid, Uuid, String, String, DateTime<Utc>)>(
        "SELECT id,incident_id,status,public_message,published_at FROM nblb.incident_updates WHERE incident_id=ANY($1) ORDER BY published_at,id",
    )
    .bind(ids)
    .fetch_all(pool)
    .await
    .context("load admin incident updates")?;
    let mut result = HashMap::new();
    for (id, incident_id, status, public_message, published_at) in rows {
        result
            .entry(incident_id)
            .or_insert_with(Vec::new)
            .push(IncidentUpdate {
                id,
                status,
                public_message,
                published_at,
            });
    }
    Ok(result)
}

pub(crate) async fn incidents(pool: &PgPool) -> Result<Vec<Incident>> {
    let rows = sqlx::query_as::<_, IncidentRow>(
        "SELECT id,slug,title,status,severity,public,started_at,resolved_at,created_at,updated_at FROM nblb.incidents ORDER BY started_at DESC,id DESC LIMIT 100",
    )
    .fetch_all(pool)
    .await
    .context("load admin incidents")?;
    let ids: Vec<Uuid> = rows.iter().map(|row| row.id).collect();
    let mut updates = incident_updates(pool, &ids).await?;
    Ok(rows
        .into_iter()
        .map(|row| Incident {
            id: row.id,
            slug: row.slug,
            title: row.title,
            status: row.status,
            severity: row.severity,
            public: row.public,
            started_at: row.started_at,
            resolved_at: row.resolved_at,
            created_at: row.created_at,
            updated_at: row.updated_at,
            updates: Some(updates.remove(&row.id).unwrap_or_default()),
        })
        .collect())
}

pub(crate) async fn incidents_page(
    pool: &PgPool,
    before: Option<(DateTime<Utc>, Uuid)>,
    limit: i64,
) -> Result<Vec<Incident>> {
    let (before_at, before_id) = before.map_or((None, None), |(at, id)| (Some(at), Some(id)));
    let rows = sqlx::query_as::<_, IncidentRow>(
        "SELECT id,slug,title,status,severity,public,started_at,resolved_at,created_at,updated_at FROM nblb.incidents WHERE ($1::timestamptz IS NULL OR (started_at,id)<($1,$2)) ORDER BY started_at DESC,id DESC LIMIT $3",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(limit.clamp(1, 101))
    .fetch_all(pool)
    .await
    .context("load paginated admin incidents")?;
    let ids: Vec<Uuid> = rows.iter().map(|row| row.id).collect();
    let mut updates = incident_updates(pool, &ids).await?;
    Ok(rows
        .into_iter()
        .map(|row| Incident {
            id: row.id,
            slug: row.slug,
            title: row.title,
            status: row.status,
            severity: row.severity,
            public: row.public,
            started_at: row.started_at,
            resolved_at: row.resolved_at,
            created_at: row.created_at,
            updated_at: row.updated_at,
            updates: Some(updates.remove(&row.id).unwrap_or_default()),
        })
        .collect())
}

#[derive(Debug, FromRow)]
struct AuditRow {
    id: Uuid,
    action: String,
    resource_kind: String,
    resource_id: Option<Uuid>,
    actor_kind: String,
    request_id: Option<Uuid>,
    outcome: String,
    detail: Value,
    created_at: DateTime<Utc>,
}

pub(crate) async fn audit_events_page(
    pool: &PgPool,
    before: Option<(DateTime<Utc>, Uuid)>,
    limit: i64,
) -> Result<Vec<AuditEvent>> {
    let (before_at, before_id) = before.map_or((None, None), |(at, id)| (Some(at), Some(id)));
    Ok(sqlx::query_as::<_, AuditRow>(
        "SELECT id,action,resource_kind,resource_id,actor_kind,request_id,outcome,detail,created_at FROM nblb.audit_events WHERE ($1::timestamptz IS NULL OR (created_at,id)<($1,$2)) ORDER BY created_at DESC,id DESC LIMIT $3",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(limit.clamp(1, 101))
    .fetch_all(pool)
    .await
    .context("load admin audit events")?
    .into_iter()
    .map(|row| AuditEvent { id: row.id, action: row.action, resource_kind: row.resource_kind, resource_id: row.resource_id, actor_kind: row.actor_kind, request_id: row.request_id, outcome: row.outcome, detail: row.detail, created_at: row.created_at })
    .collect())
}

pub(crate) async fn insert_audit(
    tx: &mut Transaction<'_, Postgres>,
    action: &str,
    resource_kind: &str,
    resource_id: Option<Uuid>,
    request_id: Option<Uuid>,
    detail: Value,
) -> Result<Uuid> {
    sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO nblb.audit_events(action,resource_kind,resource_id,request_id,outcome,detail) VALUES ($1,$2,$3,$4,'succeeded',$5) RETURNING id",
    )
    .bind(action)
    .bind(resource_kind)
    .bind(resource_id)
    .bind(request_id)
    .bind(detail)
    .fetch_one(&mut **tx)
    .await
    .context("insert mutation audit event")
}

#[cfg(test)]
mod pagination_tests {
    use super::incidents_page;
    use chrono::{Duration, TimeZone, Utc};
    use std::collections::HashSet;
    use uuid::Uuid;

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn incident_keyset_survives_insert_and_anchor_delete_and_reaches_past_100(
        pool: sqlx::PgPool,
    ) {
        let at = Utc
            .with_ymd_and_hms(2026, 7, 21, 12, 0, 0)
            .single()
            .expect("fixed timestamp");
        for value in 1_u128..=120 {
            sqlx::query(
                "INSERT INTO nblb.incidents(id,slug,title,status,severity,started_at) VALUES($1,$2,$3,'investigating','minor',$4)",
            )
            .bind(Uuid::from_u128(value))
            .bind(format!("incident-{value}"))
            .bind(format!("Incident {value}"))
            .bind(at)
            .execute(&pool)
            .await
            .expect("seed incident");
        }

        let first_fetch = incidents_page(&pool, None, 51)
            .await
            .expect("load first keyset page");
        assert_eq!(first_fetch.len(), 51);
        let first = &first_fetch[..50];
        assert_eq!(
            first.first().map(|item| item.id),
            Some(Uuid::from_u128(120))
        );
        assert_eq!(first.last().map(|item| item.id), Some(Uuid::from_u128(71)));
        let anchor = first.last().expect("first page anchor");
        let cursor = (anchor.started_at, anchor.id);

        let inserted = Uuid::from_u128(121);
        sqlx::query(
            "INSERT INTO nblb.incidents(id,slug,title,status,severity,started_at) VALUES($1,'new-top','New top','investigating','minor',$2)",
        )
        .bind(inserted)
        .bind(at + Duration::seconds(1))
        .execute(&pool)
        .await
        .expect("insert newer incident");
        sqlx::query("DELETE FROM nblb.incidents WHERE id=$1")
            .bind(anchor.id)
            .execute(&pool)
            .await
            .expect("delete cursor anchor");

        let second_fetch = incidents_page(&pool, Some(cursor), 51)
            .await
            .expect("load second keyset page after mutation");
        let second = &second_fetch[..50];
        assert_eq!(
            second.first().map(|item| item.id),
            Some(Uuid::from_u128(70))
        );
        assert_eq!(second.last().map(|item| item.id), Some(Uuid::from_u128(21)));
        assert!(!second.iter().any(|item| item.id == inserted));

        let second_anchor = second.last().expect("second page anchor");
        let third = incidents_page(
            &pool,
            Some((second_anchor.started_at, second_anchor.id)),
            51,
        )
        .await
        .expect("load third keyset page");
        assert_eq!(third.len(), 20);
        assert_eq!(third.first().map(|item| item.id), Some(Uuid::from_u128(20)));
        assert_eq!(third.last().map(|item| item.id), Some(Uuid::from_u128(1)));

        let visible_ids = first
            .iter()
            .map(|item| item.id)
            .chain(second.iter().map(|item| item.id))
            .chain(third.iter().map(|item| item.id))
            .collect::<Vec<_>>();
        assert_eq!(visible_ids.len(), 120);
        assert_eq!(
            visible_ids.iter().copied().collect::<HashSet<_>>().len(),
            120
        );
    }
}

pub(crate) async fn update_routing_policy(
    pool: &PgPool,
    retryable_statuses: &[u16],
    default_cooldown_seconds: u32,
    request_id: Uuid,
) -> Result<(RoutingPolicy, Uuid)> {
    let mut tx = pool.begin().await.context("begin routing policy update")?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext('nblb.routing_policy'))")
        .execute(&mut *tx)
        .await
        .context("lock routing policy")?;
    let version = sqlx::query_scalar::<_, i32>(
        "SELECT COALESCE(max(version),0)+1 FROM nblb.routing_policies",
    )
    .fetch_one(&mut *tx)
    .await
    .context("allocate routing policy version")?;
    sqlx::query("UPDATE nblb.routing_policies SET active=false WHERE active=true")
        .execute(&mut *tx)
        .await
        .context("deactivate routing policy")?;
    let document = json!({
        "retryable_statuses": retryable_statuses,
        "default_cooldown_seconds": default_cooldown_seconds,
        "stream_failover_before_first_frame_only": true,
        "generation_retry": false,
    });
    sqlx::query("INSERT INTO nblb.routing_policies(version,active,document,activated_at) VALUES ($1,true,$2,now())")
        .bind(version)
        .bind(&document)
        .execute(&mut *tx)
        .await
        .context("insert routing policy")?;
    let audit = insert_audit(
        &mut tx,
        "routing.policy.update",
        "routing_policy",
        None,
        Some(request_id),
        json!({"version":version}),
    )
    .await?;
    tx.commit().await.context("commit routing policy update")?;
    Ok((routing_policy(pool).await?, audit))
}

pub(crate) async fn update_settings(
    pool: &PgPool,
    proof_freshness_seconds: Option<u32>,
    request_retention_days: Option<u32>,
    metric_retention_days: Option<u32>,
    public_incidents_enabled: Option<bool>,
    request_id: Uuid,
) -> Result<(Settings, Uuid)> {
    let mut tx = pool.begin().await.context("begin settings update")?;
    let result = sqlx::query(
        "UPDATE nblb.operations_settings SET proof_freshness_seconds=COALESCE($1,proof_freshness_seconds),request_retention_days=COALESCE($2,request_retention_days),metric_retention_days=COALESCE($3,metric_retention_days),public_incidents_enabled=COALESCE($4,public_incidents_enabled),updated_at=now() WHERE singleton=true",
    )
    .bind(proof_freshness_seconds.map(|value| i32::try_from(value).unwrap_or(i32::MAX)))
    .bind(request_retention_days.map(|value| i32::try_from(value).unwrap_or(i32::MAX)))
    .bind(metric_retention_days.map(|value| i32::try_from(value).unwrap_or(i32::MAX)))
    .bind(public_incidents_enabled)
    .execute(&mut *tx)
    .await
    .context("update operations settings")?;
    if result.rows_affected() != 1 {
        bail!("operations settings row missing")
    }
    let audit = insert_audit(
        &mut tx,
        "settings.update",
        "settings",
        None,
        Some(request_id),
        json!({
            "proof_freshness_seconds_changed":proof_freshness_seconds.is_some(),
            "request_retention_days_changed":request_retention_days.is_some(),
            "metric_retention_days_changed":metric_retention_days.is_some(),
            "public_incidents_enabled_changed":public_incidents_enabled.is_some()
        }),
    )
    .await?;
    tx.commit().await.context("commit settings update")?;
    Ok((settings(pool).await?, audit))
}

pub(crate) struct CreateIncident<'a> {
    pub(crate) slug: &'a str,
    pub(crate) title: &'a str,
    pub(crate) status: &'a str,
    pub(crate) severity: &'a str,
    pub(crate) public: bool,
    pub(crate) public_message: &'a str,
    pub(crate) request_id: Uuid,
}

pub(crate) async fn create_incident(
    pool: &PgPool,
    input: CreateIncident<'_>,
) -> Result<(Incident, Uuid)> {
    let mut tx = pool.begin().await.context("begin incident create")?;
    let resolved_at = (input.status == "resolved").then(Utc::now);
    let id = sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO nblb.incidents(slug,title,status,severity,public,resolved_at) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
    )
    .bind(input.slug)
    .bind(input.title)
    .bind(input.status)
    .bind(input.severity)
    .bind(input.public)
    .bind(resolved_at)
    .fetch_one(&mut *tx)
    .await
    .context("insert incident")?;
    sqlx::query(
        "INSERT INTO nblb.incident_updates(incident_id,status,public_message) VALUES ($1,$2,$3)",
    )
    .bind(id)
    .bind(input.status)
    .bind(input.public_message)
    .execute(&mut *tx)
    .await
    .context("insert initial incident update")?;
    let audit = insert_audit(
        &mut tx,
        "incident.create",
        "incident",
        Some(id),
        Some(input.request_id),
        json!({"public":input.public,"status":input.status}),
    )
    .await?;
    tx.commit().await.context("commit incident create")?;
    let item = incidents(pool)
        .await?
        .into_iter()
        .find(|item| item.id == id)
        .ok_or_else(|| anyhow!("created incident missing"))?;
    Ok((item, audit))
}

pub(crate) async fn update_incident(
    pool: &PgPool,
    id: Uuid,
    title: Option<&str>,
    status: Option<&str>,
    severity: Option<&str>,
    public: Option<bool>,
    request_id: Uuid,
) -> Result<(Incident, Uuid)> {
    let mut tx = pool.begin().await.context("begin incident update")?;
    let result = sqlx::query("UPDATE nblb.incidents SET title=COALESCE($2,title),status=COALESCE($3,status),severity=COALESCE($4,severity),public=COALESCE($5,public),resolved_at=CASE WHEN $3::text IS NULL THEN resolved_at WHEN $3='resolved' THEN COALESCE(resolved_at,now()) ELSE NULL END,updated_at=now() WHERE id=$1")
        .bind(id).bind(title).bind(status).bind(severity).bind(public)
        .execute(&mut *tx).await.context("update incident")?;
    if result.rows_affected() != 1 {
        bail!("incident not found")
    }
    let audit = insert_audit(
        &mut tx,
        "incident.update",
        "incident",
        Some(id),
        Some(request_id),
        json!({
            "title_changed":title.is_some(),
            "status_changed":status.is_some(),
            "severity_changed":severity.is_some(),
            "public_changed":public.is_some()
        }),
    )
    .await?;
    tx.commit().await.context("commit incident update")?;
    let item = incidents(pool)
        .await?
        .into_iter()
        .find(|item| item.id == id)
        .ok_or_else(|| anyhow!("updated incident missing"))?;
    Ok((item, audit))
}

pub(crate) async fn create_incident_update(
    pool: &PgPool,
    id: Uuid,
    status: &str,
    message: &str,
    request_id: Uuid,
) -> Result<(Incident, Uuid)> {
    let mut tx = pool.begin().await.context("begin incident message")?;
    let resolved_at = (status == "resolved").then(Utc::now);
    let result = sqlx::query(
        "UPDATE nblb.incidents SET status=$2,resolved_at=$3,updated_at=now() WHERE id=$1",
    )
    .bind(id)
    .bind(status)
    .bind(resolved_at)
    .execute(&mut *tx)
    .await
    .context("update incident status")?;
    if result.rows_affected() != 1 {
        bail!("incident not found")
    }
    sqlx::query(
        "INSERT INTO nblb.incident_updates(incident_id,status,public_message) VALUES ($1,$2,$3)",
    )
    .bind(id)
    .bind(status)
    .bind(message)
    .execute(&mut *tx)
    .await
    .context("insert incident message")?;
    let audit = insert_audit(
        &mut tx,
        "incident.message.create",
        "incident",
        Some(id),
        Some(request_id),
        json!({"status":status}),
    )
    .await?;
    tx.commit().await.context("commit incident message")?;
    let item = incidents(pool)
        .await?
        .into_iter()
        .find(|item| item.id == id)
        .ok_or_else(|| anyhow!("updated incident missing"))?;
    Ok((item, audit))
}

pub(crate) async fn sync_model_catalog(
    pool: &PgPool,
    request_id: Uuid,
) -> Result<(Vec<AdminModel>, Uuid)> {
    let mut tx = pool.begin().await.context("begin model catalog sync")?;
    for spec in MODEL_SPECS {
        sqlx::query("INSERT INTO nblb.model_catalog(profile_id,endpoint,input_modalities,output_modalities,streaming,tool_calling,advertised,billable_probe) VALUES ($1,$2,$3,$4,$5,$6,true,$7) ON CONFLICT(profile_id) DO UPDATE SET endpoint=EXCLUDED.endpoint,input_modalities=EXCLUDED.input_modalities,output_modalities=EXCLUDED.output_modalities,streaming=EXCLUDED.streaming,tool_calling=EXCLUDED.tool_calling,billable_probe=EXCLUDED.billable_probe,updated_at=now()")
            .bind(spec.id).bind(spec.endpoint).bind(spec.input_modalities).bind(spec.output_modalities).bind(spec.streaming).bind(spec.tool_calling).bind(spec.billable_probe)
            .execute(&mut *tx).await.context("sync model catalog row")?;
    }
    let audit = insert_audit(
        &mut tx,
        "models.sync",
        "model_catalog",
        None,
        Some(request_id),
        json!({"catalogued":MODEL_SPECS.len()}),
    )
    .await?;
    tx.commit().await.context("commit model catalog sync")?;
    Ok((admin_models(pool).await?, audit))
}

pub(crate) struct ProbeResultInput<'a> {
    pub(crate) kind: &'a str,
    pub(crate) upstream_id: Uuid,
    pub(crate) profile_id: Option<&'a str>,
    pub(crate) receipt_profile: Option<&'a str>,
    pub(crate) billable: bool,
    pub(crate) status_code: Option<u16>,
    pub(crate) error_class: Option<&'a str>,
    pub(crate) latency_ms: u64,
    pub(crate) request_id: Uuid,
}

pub(crate) async fn record_probe_result(
    pool: &PgPool,
    input: ProbeResultInput<'_>,
) -> Result<(ProbeRun, Uuid)> {
    let mut tx = pool.begin().await.context("begin probe result")?;
    let passed = input.error_class.is_none()
        && input
            .status_code
            .is_some_and(|status| (200..300).contains(&status));
    let id = sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO nblb.probe_runs(kind,upstream_id,profile_id,status,status_code,latency_ms,error_class,billable,started_at,finished_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now(),now()) RETURNING id",
    )
    .bind(input.kind)
    .bind(input.upstream_id)
    .bind(input.profile_id)
    .bind(if passed { "passed" } else { "failed" })
    .bind(input.status_code.map(i32::from))
    .bind(i64::try_from(input.latency_ms).unwrap_or(i64::MAX))
    .bind(input.error_class)
    .bind(input.billable)
    .fetch_one(&mut *tx)
    .await
    .context("insert probe result")?;
    if passed {
        if let Some(profile) = input.receipt_profile {
            sqlx::query("INSERT INTO nblb.profile_probe_receipts(profile_id,key_id,last_probe_run_id,invalidated_at,invalidation_reason) VALUES ($1,$2,$3,NULL,NULL) ON CONFLICT(profile_id,key_id) DO UPDATE SET verified_at=now(),last_probe_run_id=EXCLUDED.last_probe_run_id,invalidated_at=NULL,invalidation_reason=NULL")
                .bind(profile).bind(input.upstream_id).bind(id).execute(&mut *tx).await.context("store probe receipt")?;
        }
    } else if matches!(input.status_code, Some(401 | 403)) {
        sqlx::query("UPDATE nblb.profile_probe_receipts SET invalidated_at=now(),invalidation_reason='provider_auth_error' WHERE key_id=$1 AND invalidated_at IS NULL")
            .bind(input.upstream_id).execute(&mut *tx).await.context("invalidate provider proofs")?;
    }
    let audit = insert_audit(
        &mut tx,
        "probe.complete",
        "probe",
        Some(id),
        Some(input.request_id),
        json!({"billable":input.billable,"kind":input.kind,"passed":passed}),
    )
    .await?;
    tx.commit().await.context("commit probe result")?;
    let item = probe(pool, id)
        .await?
        .ok_or_else(|| anyhow!("recorded probe missing"))?;
    Ok((item, audit))
}

pub(crate) async fn routing_policy(pool: &PgPool) -> Result<RoutingPolicy> {
    let row = sqlx::query_as::<_, (i32, bool, Value)>(
        "SELECT version,active,document FROM nblb.routing_policies WHERE active=true ORDER BY version DESC LIMIT 1",
    )
    .fetch_optional(pool)
    .await
    .context("load active routing policy")?;
    let Some((version, active, document)) = row else {
        return Ok(RoutingPolicy::default());
    };
    let mut policy: RoutingPolicy = serde_json::from_value(json!({
        "version": version,
        "active": active,
        "retryable_statuses": document.get("retryable_statuses").cloned().unwrap_or_else(|| json!([402,408,429,500,502,503,504])),
        "default_cooldown_seconds": document.get("default_cooldown_seconds").cloned().unwrap_or_else(|| json!(2)),
        "stream_failover_before_first_frame_only": document.get("stream_failover_before_first_frame_only").cloned().unwrap_or(json!(true)),
        "generation_retry": document.get("generation_retry").cloned().unwrap_or(json!(false)),
    })).context("decode active routing policy")?;
    policy.version = u32::try_from(version.max(1)).unwrap_or(1);
    Ok(policy)
}

pub(crate) async fn settings(pool: &PgPool) -> Result<Settings> {
    let row = sqlx::query_as::<_, (i32, i32, i32, bool)>(
        "SELECT proof_freshness_seconds,request_retention_days,metric_retention_days,public_incidents_enabled FROM nblb.operations_settings WHERE singleton=true",
    )
    .fetch_optional(pool)
    .await
    .context("load operations settings")?;
    Ok(row.map_or_else(Settings::default, |row| Settings {
        proof_freshness_seconds: as_u32(row.0),
        request_retention_days: as_u32(row.1),
        metric_retention_days: as_u32(row.2),
        public_incidents_enabled: row.3,
    }))
}

#[derive(Debug, FromRow)]
struct QaRunRow {
    id: Uuid,
    suite: String,
    live: bool,
    provider_identity: String,
    deployment_commit: String,
    status: String,
    created_at: DateTime<Utc>,
    started_at: Option<DateTime<Utc>>,
    finished_at: Option<DateTime<Utc>>,
}

#[derive(Debug, FromRow)]
struct QaCaseRow {
    run_id: Uuid,
    id: Uuid,
    name: String,
    status: String,
    evidence: Value,
    started_at: Option<DateTime<Utc>>,
    finished_at: Option<DateTime<Utc>>,
}

fn qa_run_from_row(run: QaRunRow, cases: Vec<QaCase>) -> QaRun {
    QaRun {
        id: run.id,
        suite: run.suite,
        live: run.live,
        provider_identity: run.provider_identity,
        deployment_commit: run.deployment_commit,
        status: run.status,
        created_at: run.created_at,
        started_at: run.started_at,
        finished_at: run.finished_at,
        cases,
    }
}

pub(crate) async fn qa_run(pool: &PgPool, id: Uuid) -> Result<Option<QaRun>> {
    let run = sqlx::query_as::<_, QaRunRow>(
        "SELECT id,suite,live,provider_identity,deployment_commit,status,created_at,started_at,finished_at FROM nblb.qa_runs WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await
    .context("load QA run")?;
    let Some(run) = run else { return Ok(None) };
    let cases = sqlx::query_as::<_, (Uuid, String, String, Value, Option<DateTime<Utc>>, Option<DateTime<Utc>>)>(
        "SELECT id,name,status,evidence,started_at,finished_at FROM nblb.qa_cases WHERE run_id=$1 ORDER BY id",
    )
    .bind(id)
    .fetch_all(pool)
    .await
    .context("load QA cases")?
    .into_iter()
    .map(|row| QaCase { id: row.0, name: row.1, status: row.2, evidence: row.3, started_at: row.4, finished_at: row.5 })
    .collect();
    Ok(Some(qa_run_from_row(run, cases)))
}

pub(crate) async fn qa_runs(
    pool: &PgPool,
    limit: usize,
    before: Option<(DateTime<Utc>, Uuid)>,
) -> Result<Vec<QaRun>> {
    let (before_at, before_id) = before.unzip();
    let rows = sqlx::query_as::<_, QaRunRow>(
        "SELECT id,suite,live,provider_identity,deployment_commit,status,created_at,started_at,finished_at FROM nblb.qa_runs WHERE ($1::timestamptz IS NULL OR (created_at,id) < ($1,$2)) ORDER BY created_at DESC,id DESC LIMIT $3",
    )
    .bind(before_at)
    .bind(before_id)
    .bind(i64::try_from(limit.clamp(1, 101)).unwrap_or(101))
    .fetch_all(pool)
    .await
    .context("list QA runs")?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let ids = rows.iter().map(|row| row.id).collect::<Vec<_>>();
    let case_rows = sqlx::query_as::<_, QaCaseRow>(
        "SELECT run_id,id,name,status,evidence,started_at,finished_at FROM nblb.qa_cases WHERE run_id = ANY($1) ORDER BY run_id,id",
    )
    .bind(&ids)
    .fetch_all(pool)
    .await
    .context("load paged QA cases")?;
    let mut cases_by_run = HashMap::<Uuid, Vec<QaCase>>::new();
    for row in case_rows {
        cases_by_run.entry(row.run_id).or_default().push(QaCase {
            id: row.id,
            name: row.name,
            status: row.status,
            evidence: row.evidence,
            started_at: row.started_at,
            finished_at: row.finished_at,
        });
    }
    Ok(rows
        .into_iter()
        .map(|row| {
            let cases = cases_by_run.remove(&row.id).unwrap_or_default();
            qa_run_from_row(row, cases)
        })
        .collect())
}

pub(crate) async fn qa_completion(
    pool: &PgPool,
    deployment_commit: &str,
    suites: &[&str],
) -> Result<Vec<super::dto::QaCompletion>> {
    let mut completion = Vec::with_capacity(suites.len());
    for suite in suites {
        let passed_id = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM nblb.qa_runs WHERE suite=$1 AND live=true AND provider_identity='nvidia_hosted' AND status='passed' AND deployment_commit=$2 ORDER BY finished_at DESC NULLS LAST,created_at DESC,id DESC LIMIT 1",
        )
        .bind(suite)
        .bind(deployment_commit)
        .fetch_optional(pool)
        .await
        .context("load current-deployment passed QA run")?;
        let latest_id = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM nblb.qa_runs WHERE suite=$1 AND live=true AND provider_identity='nvidia_hosted' AND deployment_commit=$2 ORDER BY created_at DESC,id DESC LIMIT 1",
        )
        .bind(suite)
        .bind(deployment_commit)
        .fetch_optional(pool)
        .await
        .context("load current-deployment latest QA run")?;
        let passed_run = match passed_id {
            Some(id) => qa_run(pool, id).await?,
            None => None,
        };
        let latest_run = match latest_id {
            Some(id) => qa_run(pool, id).await?,
            None => None,
        };
        completion.push(super::dto::QaCompletion {
            suite: (*suite).to_owned(),
            passed: passed_run.is_some(),
            passed_run,
            latest_run,
        });
    }
    Ok(completion)
}

#[derive(Debug)]
pub(crate) enum CreateQaRunError {
    UnsupportedSuite,
    HermesLiveRequired,
    ActiveRun(Uuid),
    Internal(anyhow::Error),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum QaProviderIdentity {
    Fake,
    NvidiaHosted,
}

impl QaProviderIdentity {
    fn as_str(self) -> &'static str {
        match self {
            Self::Fake => "fake",
            Self::NvidiaHosted => "nvidia_hosted",
        }
    }
}

impl fmt::Display for CreateQaRunError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedSuite => formatter.write_str("unsupported QA suite"),
            Self::HermesLiveRequired => formatter.write_str("Hermes E2E requires live mode"),
            Self::ActiveRun(id) => write!(formatter, "QA run {id} is already active"),
            Self::Internal(error) => write!(formatter, "{error:#}"),
        }
    }
}

impl Error for CreateQaRunError {}

fn is_active_run_conflict(error: &sqlx::Error) -> bool {
    error.as_database_error().is_some_and(|database| {
        database.code().as_deref() == Some("23505")
            && database.constraint() == Some("qa_runs_single_active_idx")
    })
}

pub(crate) async fn create_qa_run(
    pool: &PgPool,
    suite: &str,
    live: bool,
    provider_identity: QaProviderIdentity,
    request_id: Uuid,
) -> std::result::Result<(QaRun, Uuid), CreateQaRunError> {
    let cases: &[&str] = match suite {
        "smoke" => &[
            "liveness",
            "readiness",
            "models",
            "chat_non_stream",
            "chat_stream",
            "privacy",
        ],
        "distribution" => &["two_eligible_slots", "six_requests", "skew_at_most_one"],
        "failover" => &[
            "before_first_frame",
            "after_first_frame_no_replay",
            "terminal_evidence",
        ],
        "persistence" => &[
            "restart_observed",
            "encrypted_keys",
            "downstream_token",
            "profile_receipts",
            "routing_cursor",
            "owner_lease",
        ],
        "multimodal" => &[
            "phi_text_image_audio",
            "vila_text_image_video",
            "nvclip_text_image",
            "flux_text_image",
            "video_generation",
            "speech_and_transcription",
        ],
        "hermes-e2e" => &[
            "doctor",
            "exact_marker",
            "tool_task",
            "request_correlation",
            "secret_scan",
            "rollback_rehearsal",
        ],
        _ => return Err(CreateQaRunError::UnsupportedSuite),
    };
    if suite == "hermes-e2e" && !live {
        return Err(CreateQaRunError::HermesLiveRequired);
    }
    if live != (provider_identity == QaProviderIdentity::NvidiaHosted) {
        return Err(CreateQaRunError::Internal(anyhow!(
            "QA live mode and provider identity do not match"
        )));
    }
    for _attempt in 0..3 {
        let mut tx = pool
            .begin()
            .await
            .context("begin QA run")
            .map_err(CreateQaRunError::Internal)?;
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(0x4e42_4c42_5141_i64)
            .execute(&mut *tx)
            .await
            .context("serialize QA run creation")
            .map_err(CreateQaRunError::Internal)?;
        let active_id = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM nblb.qa_runs WHERE status IN ('queued','running') ORDER BY created_at DESC,id DESC LIMIT 1",
        )
        .fetch_optional(&mut *tx)
        .await
        .context("load active QA run under creation lock")
        .map_err(CreateQaRunError::Internal)?;
        if let Some(active_id) = active_id {
            tx.rollback().await.ok();
            return Err(CreateQaRunError::ActiveRun(active_id));
        }
        let inserted = sqlx::query_scalar::<_, Uuid>(
            "INSERT INTO nblb.qa_runs(suite,live,provider_identity,deployment_commit) VALUES ($1,$2,$3,$4) RETURNING id",
        )
        .bind(suite)
        .bind(live)
        .bind(provider_identity.as_str())
        .bind(crate::BUILD_COMMIT)
        .fetch_one(&mut *tx)
        .await;
        let run_id = match inserted {
            Ok(id) => id,
            Err(error) if is_active_run_conflict(&error) => {
                tx.rollback().await.ok();
                continue;
            }
            Err(error) => {
                return Err(CreateQaRunError::Internal(
                    anyhow!(error).context("insert QA run"),
                ));
            }
        };
        for name in cases {
            sqlx::query("INSERT INTO nblb.qa_cases(run_id,name) VALUES ($1,$2)")
                .bind(run_id)
                .bind(name)
                .execute(&mut *tx)
                .await
                .context("insert QA case")
                .map_err(CreateQaRunError::Internal)?;
        }
        let audit = insert_audit(
            &mut tx,
            "qa.run.create",
            "qa_run",
            Some(run_id),
            Some(request_id),
            json!({"live":live,"provider_identity":provider_identity.as_str(),"suite":suite}),
        )
        .await
        .map_err(CreateQaRunError::Internal)?;
        tx.commit()
            .await
            .context("commit QA run")
            .map_err(CreateQaRunError::Internal)?;
        let run = qa_run(pool, run_id)
            .await
            .map_err(CreateQaRunError::Internal)?
            .ok_or_else(|| CreateQaRunError::Internal(anyhow!("created QA run missing")))?;
        return Ok((run, audit));
    }
    Err(CreateQaRunError::Internal(anyhow!(
        "QA run creation conflict did not stabilize after retry"
    )))
}

#[cfg(test)]
mod qa_authority_tests {
    use super::{CreateQaRunError, QaProviderIdentity, create_qa_run, qa_completion, qa_runs};
    use chrono::{TimeZone, Utc};
    use std::collections::HashSet;
    use uuid::Uuid;

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn qa_authority_rejects_fake_hermes_and_allows_one_active_run(pool: sqlx::PgPool) {
        let fake = create_qa_run(
            &pool,
            "hermes-e2e",
            false,
            QaProviderIdentity::Fake,
            Uuid::new_v4(),
        )
        .await
        .expect_err("fake Hermes must be rejected");
        assert!(matches!(fake, CreateQaRunError::HermesLiveRequired));
        assert_eq!(
            sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.qa_runs")
                .fetch_one(&pool)
                .await
                .expect("count QA runs"),
            0
        );
        assert!(
            sqlx::query("INSERT INTO nblb.qa_runs(suite,live,deployment_commit) VALUES('hermes-e2e',false,$1)")
                .bind(crate::BUILD_COMMIT)
                .execute(&pool)
                .await
                .is_err(),
            "database constraint must reject new fake Hermes rows"
        );

        let (left, right) = tokio::join!(
            create_qa_run(
                &pool,
                "smoke",
                false,
                QaProviderIdentity::Fake,
                Uuid::new_v4()
            ),
            create_qa_run(
                &pool,
                "distribution",
                false,
                QaProviderIdentity::Fake,
                Uuid::new_v4()
            ),
        );
        let (winner, conflict) = match (left, right) {
            (Ok(winner), Err(conflict)) | (Err(conflict), Ok(winner)) => (winner, conflict),
            other => panic!("expected one winner and one conflict, got {other:?}"),
        };
        assert!(matches!(conflict, CreateQaRunError::ActiveRun(id) if id == winner.0.id));
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.qa_runs WHERE status IN ('queued','running')",
            )
            .fetch_one(&pool)
            .await
            .expect("count active QA runs"),
            1
        );
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.audit_events WHERE action='qa.run.create'",
            )
            .fetch_one(&pool)
            .await
            .expect("count QA creation audit events"),
            1
        );
        sqlx::query("UPDATE nblb.qa_runs SET status='failed',finished_at=now() WHERE id=$1")
            .bind(winner.0.id)
            .execute(&pool)
            .await
            .expect("close winning QA run");
        create_qa_run(
            &pool,
            "smoke",
            false,
            QaProviderIdentity::Fake,
            Uuid::new_v4(),
        )
        .await
        .expect("terminal run must release global single-flight");
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn qa_history_keyset_pages_more_than_one_hundred_tied_rows(pool: sqlx::PgPool) {
        let at = Utc
            .with_ymd_and_hms(2026, 7, 21, 12, 0, 0)
            .single()
            .expect("fixed timestamp");
        for index in 1_u128..=125 {
            sqlx::query(
                "INSERT INTO nblb.qa_runs(id,suite,live,deployment_commit,status,created_at,started_at,finished_at) VALUES($1,'smoke',false,$3,'failed',$2,$2,$2)",
            )
            .bind(Uuid::from_u128(index))
            .bind(at)
            .bind(crate::BUILD_COMMIT)
            .execute(&pool)
            .await
            .expect("seed terminal QA history");
        }

        let first_window = qa_runs(&pool, 51, None)
            .await
            .expect("first QA history window");
        assert_eq!(first_window.len(), 51);
        let first_page = &first_window[..50];
        let first_cursor = first_page.last().expect("first cursor row");
        let second_window = qa_runs(&pool, 51, Some((first_cursor.created_at, first_cursor.id)))
            .await
            .expect("second QA history window");
        assert_eq!(second_window.len(), 51);
        let second_page = &second_window[..50];
        let second_cursor = second_page.last().expect("second cursor row");
        let third_page = qa_runs(
            &pool,
            51,
            Some((second_cursor.created_at, second_cursor.id)),
        )
        .await
        .expect("third QA history window");
        assert_eq!(third_page.len(), 25);

        let ids = first_page
            .iter()
            .chain(second_page)
            .chain(&third_page)
            .map(|run| run.id)
            .collect::<HashSet<_>>();
        assert_eq!(ids.len(), 125);
        assert!(first_page.windows(2).all(|pair| pair[0].id > pair[1].id));
    }

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn unverified_live_history_is_not_authoritative_completion(pool: sqlx::PgPool) {
        sqlx::query(
            "INSERT INTO nblb.qa_runs(suite,live,provider_identity,deployment_commit,status,started_at,finished_at) VALUES('smoke',true,'unverified',$1,'passed',now(),now())",
        )
        .bind(crate::BUILD_COMMIT)
        .execute(&pool)
        .await
        .expect("seed pre-provenance live history");

        let completion = qa_completion(&pool, crate::BUILD_COMMIT, &["smoke"])
            .await
            .expect("load provider-authoritative completion");
        assert_eq!(completion.len(), 1);
        assert!(!completion[0].passed);
        assert!(completion[0].passed_run.is_none());
        assert!(completion[0].latest_run.is_none());
    }
}

#[cfg(test)]
mod request_query_tests {
    use super::{RequestFilters, request_aggregate, requests_page};
    use chrono::{Duration, TimeZone, Utc};
    use std::collections::HashSet;
    use uuid::Uuid;

    #[sqlx::test(migrations = "../../migrations/sqlx")]
    async fn request_filters_aggregate_and_keyset_are_consistent(pool: sqlx::PgPool) {
        let client_a = Uuid::new_v4();
        let client_b = Uuid::new_v4();
        for (id, label, digest, prefix) in [
            (client_a, "request-filter-a", "11", "filter_a"),
            (client_b, "request-filter-b", "22", "filter_b"),
        ] {
            sqlx::query(
                "INSERT INTO nblb.downstream_credentials(id,label,digest,scopes,key_prefix) VALUES($1,$2,decode(repeat($3,32),'hex'),ARRAY['chat:write'],$4)",
            )
            .bind(id)
            .bind(label)
            .bind(digest)
            .bind(prefix)
            .execute(&pool)
            .await
            .expect("seed request filter client");
        }

        let at = Utc
            .with_ymd_and_hms(2026, 7, 21, 12, 0, 0)
            .single()
            .expect("fixed request filter timestamp");
        let rows = [
            (
                Uuid::from_u128(1),
                client_a,
                "/v1/chat/completions",
                "z-ai/glm-5.2",
                "succeeded",
                Some(10_i64),
                -5_i64,
            ),
            (
                Uuid::from_u128(2),
                client_a,
                "/v1/chat/completions",
                "z-ai/glm-5.2",
                "rejected",
                Some(20),
                -4,
            ),
            (
                Uuid::from_u128(3),
                client_b,
                "/v1/embeddings",
                "nvidia/nvclip",
                "failed",
                Some(30),
                -3,
            ),
            (
                Uuid::from_u128(4),
                client_b,
                "/v1/embeddings",
                "nvidia/nvclip",
                "cancelled",
                Some(40),
                -2,
            ),
            (
                Uuid::from_u128(5),
                client_b,
                "/v1/nvidia/inference",
                "nvidia/vila",
                "abandoned_after_restart",
                Some(50),
                -1,
            ),
            (
                Uuid::from_u128(6),
                client_b,
                "/v1/nvidia/inference",
                "nvidia/vila",
                "started",
                None,
                0,
            ),
        ];
        for (request_id, client_id, endpoint, profile, outcome, duration_ms, offset) in rows {
            let started_at = at + Duration::minutes(offset);
            let finished_at = (outcome != "started").then_some(started_at + Duration::seconds(1));
            sqlx::query(
                "INSERT INTO nblb.proxy_requests(request_id,downstream_credential_id,endpoint,profile_id,stream,modality,outcome,duration_ms,started_at,finished_at) VALUES($1,$2,$3,$4,false,'text',$5,$6,$7,$8)",
            )
            .bind(request_id)
            .bind(client_id)
            .bind(endpoint)
            .bind(profile)
            .bind(outcome)
            .bind(duration_ms)
            .bind(started_at)
            .bind(finished_at)
            .execute(&pool)
            .await
            .expect("seed proxy request filter fixture");
        }

        let all = RequestFilters::default();
        let aggregate = request_aggregate(&pool, &all)
            .await
            .expect("aggregate all requests");
        assert_eq!(
            (
                aggregate.total,
                aggregate.succeeded,
                aggregate.failed,
                aggregate.cancelled,
                aggregate.active,
                aggregate.average_duration_ms
            ),
            (6, 1, 3, 1, 1, Some(30)),
        );
        let first = requests_page(&pool, None, 3, &all)
            .await
            .expect("first request page");
        assert_eq!(first.len(), 3);
        let second = requests_page(
            &pool,
            Some((first[2].started_at, first[2].request_id)),
            3,
            &all,
        )
        .await
        .expect("second request page");
        let ids = first
            .iter()
            .chain(&second)
            .map(|request| request.request_id)
            .collect::<HashSet<_>>();
        assert_eq!(ids.len(), 6);

        let cases = [
            (
                RequestFilters {
                    request_id: Some(Uuid::from_u128(2)),
                    ..RequestFilters::default()
                },
                1,
                Uuid::from_u128(2),
            ),
            (
                RequestFilters {
                    client_id: Some(client_a),
                    ..RequestFilters::default()
                },
                2,
                Uuid::from_u128(2),
            ),
            (
                RequestFilters {
                    profile: Some("nvidia/nvclip".into()),
                    ..RequestFilters::default()
                },
                2,
                Uuid::from_u128(4),
            ),
            (
                RequestFilters {
                    outcome: Some("rejected".into()),
                    ..RequestFilters::default()
                },
                1,
                Uuid::from_u128(2),
            ),
            (
                RequestFilters {
                    endpoint: Some("/v1/nvidia/inference".into()),
                    ..RequestFilters::default()
                },
                2,
                Uuid::from_u128(6),
            ),
            (
                RequestFilters {
                    since: Some(at - Duration::minutes(2)),
                    until: Some(at),
                    ..RequestFilters::default()
                },
                3,
                Uuid::from_u128(6),
            ),
            (
                RequestFilters {
                    client_id: Some(client_b),
                    profile: Some("nvidia/vila".into()),
                    outcome: Some("started".into()),
                    endpoint: Some("/v1/nvidia/inference".into()),
                    since: Some(at),
                    until: Some(at),
                    ..RequestFilters::default()
                },
                1,
                Uuid::from_u128(6),
            ),
        ];
        for (filters, expected_len, newest_id) in cases {
            let page = requests_page(&pool, None, 100, &filters)
                .await
                .expect("load filtered requests");
            assert_eq!(page.len(), expected_len);
            assert_eq!(page[0].request_id, newest_id);
            assert_eq!(
                request_aggregate(&pool, &filters)
                    .await
                    .expect("aggregate filter")
                    .total,
                u64::try_from(expected_len).expect("small fixture count"),
            );
        }
    }
}
