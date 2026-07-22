//! Loopback-only typed administrator API.

use actix_web::{HttpRequest, HttpResponse, http::StatusCode, web};
use base64::Engine;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Deserializer};
use serde_json::json;
use uuid::Uuid;

use super::repository as public_repository;
use super::{
    admin_repository as repository,
    dto::{
        AdminOverview, Attention, AttentionAction, AuditMutation, MODEL_SPECS, OverviewCapacity,
        OverviewClients, OverviewProfiles, OverviewQa, OverviewRecent, OverviewRuntime, Page,
        PrimaryAction, QaRunsPage, Snapshot,
    },
};
use crate::{
    AppState, ClientPolicyMutation, PROFILES, ProbeAuditMutation, VaultAuditMutation,
    admin_surface_allowed, authorized, operations_error, request_id::request_id,
};

pub(crate) fn routes(cfg: &mut web::ServiceConfig) {
    cfg.service(
        web::scope("/admin/api/v2")
            .route("/openapi.json", web::get().to(openapi_document))
            .route("/overview", web::get().to(overview))
            .route("/attentions", web::get().to(attentions))
            .route("/upstreams", web::get().to(list_upstreams))
            .route("/upstreams", web::post().to(create_upstream))
            .route("/upstreams/{id}", web::get().to(get_upstream))
            .route("/upstreams/{id}/probe", web::post().to(probe_upstream))
            .route(
                "/upstreams/{id}/probe-profiles",
                web::post().to(probe_upstream_profiles),
            )
            .route("/upstreams/{id}/enable", web::post().to(enable_upstream))
            .route("/upstreams/{id}/disable", web::post().to(disable_upstream))
            .route("/upstreams/{id}/retire", web::post().to(retire_upstream))
            .route("/clients", web::get().to(list_clients))
            .route("/clients", web::post().to(create_client))
            .route("/clients/{id}", web::get().to(get_client))
            .route("/clients/{id}", web::patch().to(update_client))
            .route("/clients/{id}/rotate", web::post().to(rotate_client))
            .route("/clients/{id}/revoke", web::post().to(revoke_client))
            .route("/routing/policy", web::get().to(get_routing_policy))
            .route("/routing/policy", web::patch().to(update_routing_policy))
            .route("/routing/simulate", web::post().to(simulate_routing))
            .route("/models", web::get().to(list_models))
            .route("/models/sync", web::post().to(sync_models))
            .route("/models/probe", web::post().to(probe_model))
            .route("/requests", web::get().to(list_requests))
            .route("/requests/{id}", web::get().to(get_request))
            .route("/probes", web::get().to(list_probes))
            .route("/probes/{id}", web::get().to(get_probe))
            .route("/incidents", web::get().to(list_incidents))
            .route("/incidents", web::post().to(create_incident))
            .route("/incidents/{id}", web::patch().to(update_incident))
            .route(
                "/incidents/{id}/updates",
                web::post().to(create_incident_update),
            )
            .route("/audit", web::get().to(list_audit))
            .route("/qa/runs", web::get().to(list_qa_runs))
            .route("/qa/runs", web::post().to(create_qa_run))
            .route("/qa/secret-scan", web::get().to(get_secret_scan))
            .route("/qa/runs/{id}", web::get().to(get_qa_run))
            .route(
                "/qa/runs/{id}/hermes-completion",
                web::post().to(complete_hermes_qa_run),
            )
            .route("/settings", web::get().to(get_settings))
            .route("/settings", web::patch().to(update_settings)),
    );
}

async fn openapi_document(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    no_store(HttpResponse::Ok().json(super::openapi::admin_document()))
}

fn guard(req: &HttpRequest, state: &AppState) -> Result<(), HttpResponse> {
    if !admin_surface_allowed(req, state) {
        return Err(HttpResponse::NotFound().finish());
    }
    if !authorized(req, state) {
        let mut response = operations_error(
            req,
            StatusCode::UNAUTHORIZED,
            "invalid_admin_token",
            "Invalid admin token.",
            false,
            None,
        );
        response.headers_mut().insert(
            actix_web::http::header::WWW_AUTHENTICATE,
            actix_web::http::header::HeaderValue::from_static(
                "Bearer realm=\"nvidia-build-lb-admin\"",
            ),
        );
        return Err(response);
    }
    Ok(())
}

fn pool<'a>(req: &HttpRequest, state: &'a AppState) -> Result<&'a sqlx::PgPool, HttpResponse> {
    state.vault.database.as_ref().ok_or_else(|| {
        operations_error(
            req,
            StatusCode::SERVICE_UNAVAILABLE,
            "database_unavailable",
            "The operations database is unavailable.",
            true,
            None,
        )
    })
}

fn failure(req: &HttpRequest, code: &str, message: &str) -> HttpResponse {
    operations_error(
        req,
        StatusCode::SERVICE_UNAVAILABLE,
        code,
        message,
        true,
        None,
    )
}

fn not_found(req: &HttpRequest, kind: &str) -> HttpResponse {
    operations_error(
        req,
        StatusCode::NOT_FOUND,
        "resource_not_found",
        "The requested resource was not found.",
        false,
        Some(json!({"resource_kind":kind})),
    )
}

fn no_store(mut response: HttpResponse) -> HttpResponse {
    response.headers_mut().insert(
        actix_web::http::header::CACHE_CONTROL,
        actix_web::http::header::HeaderValue::from_static("no-store"),
    );
    response
}

fn owner_lease_attention() -> Attention {
    Attention {
        severity: "critical",
        code: "owner_lease_stale",
        title: "게이트웨이 lease 확인 필요",
        reason: "요청 정리와 소유권 heartbeat가 최신이 아닙니다.",
        action: AttentionAction {
            label: "lease 다시 확인".into(),
            // This action is an in-place runtime recheck, not navigation. An
            // empty href prevents API consumers from presenting the current
            // command center as a no-op recovery destination.
            href: String::new(),
        },
    }
}

async fn current_attentions(
    pool: &sqlx::PgPool,
    state: &AppState,
) -> Result<Vec<Attention>, anyhow::Error> {
    let upstreams = repository::upstreams(pool).await?;
    let clients = repository::clients(pool).await?;
    let models = repository::admin_models(pool).await?;
    let owner_ready = if let Some(owner_id) = state.vault.owner_id {
        sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS(SELECT 1 FROM nblb.gateway_instances WHERE id=$1 AND last_seen_at >= now()-interval '30 seconds')",
        )
        .bind(owner_id)
        .fetch_one(pool)
        .await
        .unwrap_or(false)
    } else {
        false
    };
    let eligible = upstreams.iter().filter(|item| item.eligible_now).count();
    let mut items = Vec::new();
    if !owner_ready {
        items.push(owner_lease_attention());
    }
    let mut add = |severity: &'static str,
                   code: &'static str,
                   title: &'static str,
                   reason: &'static str,
                   label: &'static str,
                   href: &'static str| {
        items.push(Attention {
            severity,
            code,
            title,
            reason,
            action: AttentionAction {
                label: label.into(),
                href: href.into(),
            },
        });
    };
    if eligible == 0 {
        add(
            "critical",
            "no_eligible_upstream",
            "요청 가능한 upstream 없음",
            "검증되고 활성화된 slot이 없습니다.",
            "upstream 설정",
            "/admin/upstreams",
        );
    }
    if upstreams.iter().any(|item| !item.retired && !item.verified) {
        add(
            "warning",
            "probe_required",
            "provider probe 필요",
            "저장된 credential의 실제 호출 증거가 없습니다.",
            "probe 실행",
            "/admin/upstreams",
        );
    }
    if upstreams.iter().filter(|item| !item.retired).count() < 2 {
        add(
            "warning",
            "second_slot_missing",
            "두 번째 slot 필요",
            "장애 전환과 분산을 위한 slot이 비어 있습니다.",
            "slot 추가",
            "/admin/upstreams",
        );
    }
    if models
        .iter()
        .any(|model| model.proof_status == "proof_required")
    {
        add(
            "warning",
            "profile_proof_missing",
            "model proof 필요",
            "광고된 profile 중 실제 provider 증거가 없는 항목이 있습니다.",
            "model 검증",
            "/admin/models",
        );
    }
    if !clients.iter().any(|client| client.active) {
        add(
            "warning",
            "downstream_client_missing",
            "client credential 필요",
            "호출에 사용할 active downstream client가 없습니다.",
            "client 발급",
            "/admin/clients",
        );
    }
    let required_qa_passed = sqlx::query_scalar::<_, i64>(
        "SELECT count(DISTINCT suite) FROM nblb.qa_runs WHERE live=true AND provider_identity='nvidia_hosted' AND status='passed' AND deployment_commit=$1",
    )
    .bind(crate::BUILD_COMMIT)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if required_qa_passed < 6 {
        add(
            "warning",
            "qa_incomplete",
            "현재 배포 Live QA 미완료",
            "필수 여섯 suite 중 아직 통과하지 않은 검증이 있습니다.",
            "QA 완료",
            "/admin/qa",
        );
    }
    let hermes_verified = sqlx::query_scalar::<_, bool>(
        "SELECT EXISTS(SELECT 1 FROM nblb.qa_runs WHERE suite='hermes-e2e' AND live=true AND provider_identity='nvidia_hosted' AND status='passed' AND deployment_commit=$1)",
    )
    .bind(crate::BUILD_COMMIT)
    .fetch_one(pool)
    .await
    .unwrap_or(false);
    if !hermes_verified {
        add(
            "info",
            "hermes_unverified",
            "Hermes E2E 필요",
            "현재 배포에서 실제 agent 작업 증거가 없습니다.",
            "QA 실행",
            "/admin/qa",
        );
    }
    if models
        .iter()
        .flat_map(|model| &model.proofs)
        .any(|proof| proof.stale)
    {
        add(
            "warning",
            "proof_stale",
            "오래된 proof 갱신 필요",
            "provider 검증 유효기간을 넘긴 profile이 있습니다.",
            "probe 갱신",
            "/admin/probes",
        );
    }
    let (sample_count, failover_count, rate_limited_count) =
        sqlx::query_as::<_, (i64, i64, i64)>(
            "SELECT count(*),COALESCE(sum(request.failover_count),0)::bigint,count(*) FILTER (WHERE EXISTS(SELECT 1 FROM nblb.request_attempts attempt WHERE attempt.proxy_request_id=request.id AND attempt.status_code=429)) FROM nblb.proxy_requests request WHERE request.started_at>=now()-interval '24 hours' AND request.finished_at IS NOT NULL",
        )
        .fetch_one(pool)
        .await
        .unwrap_or((0, 0, 0));
    if sample_count >= 20 && (failover_count as f64 / sample_count as f64) >= 0.10 {
        add(
            "warning",
            "elevated_failover",
            "failover 증가 확인",
            "최근 failover 비율이 설정된 임계값을 넘었습니다.",
            "라우팅 확인",
            "/admin/routing",
        );
    }
    if sample_count >= 20 && (rate_limited_count as f64 / sample_count as f64) >= 0.10 {
        add(
            "warning",
            "elevated_rate_limit",
            "rate limit 증가 확인",
            "최근 NVIDIA 429 비율이 설정된 임계값을 넘었습니다.",
            "cooldown 확인",
            "/admin/routing",
        );
    }
    Ok(items)
}

async fn overview(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let upstreams = match repository::upstreams(pool).await {
        Ok(items) => items,
        Err(_) => return failure(&req, "overview_unavailable", "Overview is unavailable."),
    };
    let clients = match repository::clients(pool).await {
        Ok(items) => items,
        Err(_) => return failure(&req, "overview_unavailable", "Overview is unavailable."),
    };
    let models = match repository::admin_models(pool).await {
        Ok(items) => items,
        Err(_) => return failure(&req, "overview_unavailable", "Overview is unavailable."),
    };
    let items = match current_attentions(pool, &state).await {
        Ok(items) => items,
        Err(_) => return failure(&req, "overview_unavailable", "Overview is unavailable."),
    };
    let metrics_24h = public_repository::metric_summary(pool, "24 hours")
        .await
        .unwrap_or_default();
    let owner_lease_ready = if let Some(owner_id) = state.vault.owner_id {
        sqlx::query_scalar::<_, bool>(
            "SELECT EXISTS(SELECT 1 FROM nblb.gateway_instances WHERE id=$1 AND last_seen_at >= now()-interval '30 seconds')",
        )
        .bind(owner_id)
        .fetch_one(pool)
        .await
        .unwrap_or(false)
    } else {
        false
    };
    let eligible_slots = upstreams.iter().filter(|item| item.eligible_now).count();
    let last_nvidia_success_at = sqlx::query_scalar::<_, Option<DateTime<Utc>>>(
        "SELECT max(finished_at) FROM nblb.proxy_requests WHERE outcome='succeeded'",
    )
    .fetch_one(pool)
    .await
    .unwrap_or(None);
    let last_hermes_e2e_at = sqlx::query_scalar::<_, Option<DateTime<Utc>>>(
        "SELECT max(finished_at) FROM nblb.qa_runs WHERE suite='hermes-e2e' AND live=true AND provider_identity='nvidia_hosted' AND status='passed' AND deployment_commit=$1",
    )
    .bind(crate::BUILD_COMMIT)
    .fetch_one(pool)
    .await
    .unwrap_or(None);
    let qa_passed = sqlx::query_scalar::<_, i64>(
        "SELECT count(DISTINCT suite) FROM nblb.qa_runs WHERE live=true AND provider_identity='nvidia_hosted' AND status='passed' AND deployment_commit=$1",
    )
    .bind(crate::BUILD_COMMIT)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    let primary_action = items.first().cloned().map(PrimaryAction::from);
    no_store(
        HttpResponse::Ok().json(AdminOverview {
            snapshot: Snapshot::current(),
            runtime: OverviewRuntime {
                live: true,
                database_ready: true,
                owner_lease_ready,
                traffic_ready: eligible_slots > 0,
            },
            capacity: OverviewCapacity {
                configured_slots: u8::try_from(
                    upstreams.iter().filter(|item| !item.retired).count(),
                )
                .unwrap_or_default(),
                verified_slots: u8::try_from(
                    upstreams
                        .iter()
                        .filter(|item| !item.retired && item.verified)
                        .count(),
                )
                .unwrap_or_default(),
                eligible_slots: u8::try_from(eligible_slots).unwrap_or_default(),
                pair_ready: eligible_slots == 2,
            },
            profiles: OverviewProfiles {
                catalogued: u8::try_from(PROFILES.len()).unwrap_or_default(),
                advertised: u8::try_from(models.iter().filter(|item| item.advertised).count())
                    .unwrap_or_default(),
                proven: u8::try_from(
                    models
                        .iter()
                        .filter(|item| item.verified_key_count > 0)
                        .count(),
                )
                .unwrap_or_default(),
                available: u8::try_from(models.iter().filter(|item| item.available_now).count())
                    .unwrap_or_default(),
            },
            clients: OverviewClients {
                active_count: u64::try_from(clients.iter().filter(|item| item.active).count())
                    .unwrap_or_default(),
            },
            qa: OverviewQa {
                required: 6,
                passed: u8::try_from(qa_passed).unwrap_or_default(),
                complete: qa_passed == 6,
            },
            recent: OverviewRecent {
                last_nvidia_success_at,
                last_hermes_e2e_at,
            },
            metrics_24h,
            primary_action,
            attention_count: u64::try_from(items.len()).unwrap_or_default(),
        }),
    )
}

async fn attentions(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match current_attentions(pool, &state).await {
        Ok(items) => no_store(HttpResponse::Ok().json(json!({
            "snapshot": Snapshot::current(),
            "items": items.into_iter().skip(1).collect::<Vec<_>>(),
            "next_before": null,
        }))),
        Err(_) => failure(
            &req,
            "attentions_unavailable",
            "Attentions are unavailable.",
        ),
    }
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    limit: Option<u16>,
    before: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct RequestListQuery {
    limit: Option<u16>,
    before: Option<String>,
    request_id: Option<String>,
    client_id: Option<String>,
    profile: Option<String>,
    outcome: Option<String>,
    endpoint: Option<String>,
    since: Option<String>,
    until: Option<String>,
}

fn request_filters(
    req: &HttpRequest,
    query: &RequestListQuery,
) -> Result<repository::RequestFilters, HttpResponse> {
    let invalid = |message: String| {
        operations_error(
            req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_request_filter",
            &message,
            false,
            None,
        )
    };
    let parse_uuid = |value: &Option<String>, label: &str| {
        value
            .as_deref()
            .map(Uuid::parse_str)
            .transpose()
            .map_err(|_| invalid(format!("{label} must be a UUID.")))
    };
    let parse_time = |value: &Option<String>, label: &str| {
        value
            .as_deref()
            .map(DateTime::parse_from_rfc3339)
            .transpose()
            .map(|value| value.map(|time| time.with_timezone(&Utc)))
            .map_err(|_| invalid(format!("{label} must be RFC 3339.")))
    };
    let outcome = query.outcome.clone().filter(|value| !value.is_empty());
    if outcome.as_deref().is_some_and(|value| {
        !matches!(
            value,
            "started"
                | "succeeded"
                | "failed"
                | "rejected"
                | "cancelled"
                | "abandoned_after_restart"
        )
    }) {
        return Err(invalid("outcome is not supported.".to_owned()));
    }
    let profile = query.profile.clone().filter(|value| !value.is_empty());
    if profile.as_deref().is_some_and(|value| value.len() > 160) {
        return Err(invalid("profile is too long.".to_owned()));
    }
    let endpoint = query.endpoint.clone().filter(|value| !value.is_empty());
    if endpoint.as_deref().is_some_and(|value| {
        value.len() > 128 || !MODEL_SPECS.iter().any(|spec| spec.endpoint == value)
    }) {
        return Err(invalid(
            "endpoint must be a supported /v1/ path.".to_owned(),
        ));
    }
    let since = parse_time(&query.since, "since")?;
    let until = parse_time(&query.until, "until")?;
    if since.zip(until).is_some_and(|(start, end)| start > end) {
        return Err(invalid("since must not be later than until.".to_owned()));
    }
    Ok(repository::RequestFilters {
        request_id: parse_uuid(&query.request_id, "request_id")?,
        client_id: parse_uuid(&query.client_id, "client_id")?,
        profile,
        outcome,
        endpoint,
        since,
        until,
    })
}

#[derive(Clone, Copy)]
struct PageWindow {
    limit: usize,
    offset: usize,
}

fn list_window(req: &HttpRequest, query: &ListQuery) -> Result<PageWindow, HttpResponse> {
    let limit = match query.limit.unwrap_or(50) {
        1..=100 => usize::from(query.limit.unwrap_or(50)),
        _ => Err(operations_error(
            req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_limit",
            "limit must be between 1 and 100.",
            false,
            None,
        ))?,
    };
    let offset = match &query.before {
        None => 0,
        Some(cursor) => {
            let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD
                .decode(cursor)
                .ok()
                .and_then(|bytes| String::from_utf8(bytes).ok())
                .and_then(|value| value.strip_prefix("nblb-page-v1:").map(str::to_owned))
                .and_then(|value| value.parse::<usize>().ok())
                .filter(|value| *value <= 10_000_000);
            match raw {
                Some(offset) => offset,
                None => {
                    return Err(operations_error(
                        req,
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "invalid_page_cursor",
                        "This cursor is not valid for the current snapshot.",
                        false,
                        None,
                    ));
                }
            }
        }
    };
    Ok(PageWindow { limit, offset })
}

fn paginate<T>(mut items: Vec<T>, window: PageWindow) -> (Vec<T>, Option<String>) {
    let total = items.len();
    let items = if window.offset >= total {
        Vec::new()
    } else {
        items.drain(window.offset..).take(window.limit).collect()
    };
    let next_offset = window.offset.saturating_add(items.len());
    let next = (next_offset < total).then(|| {
        base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(format!("nblb-page-v1:{next_offset}"))
    });
    (items, next)
}

#[derive(Debug, Deserialize)]
struct KeysetCursor {
    version: u8,
    kind: String,
    at: DateTime<Utc>,
    id: Uuid,
}

type PageKey = (DateTime<Utc>, Uuid);
type KeysetWindow = (usize, Option<PageKey>);

fn decode_keyset_cursor(encoded: &str, kind: &str) -> Option<PageKey> {
    base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(encoded)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<KeysetCursor>(&bytes).ok())
        .filter(|cursor| cursor.version == 1 && cursor.kind == kind)
        .map(|cursor| (cursor.at, cursor.id))
}

fn keyset_window(
    req: &HttpRequest,
    query: &ListQuery,
    kind: &str,
) -> Result<KeysetWindow, HttpResponse> {
    let limit = match query.limit.unwrap_or(50) {
        1..=100 => usize::from(query.limit.unwrap_or(50)),
        _ => Err(operations_error(
            req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_limit",
            "limit must be between 1 and 100.",
            false,
            None,
        ))?,
    };
    let before = match query.before.as_deref() {
        None => None,
        Some(encoded) => match decode_keyset_cursor(encoded, kind) {
            Some(cursor) => Some(cursor),
            None => {
                return Err(operations_error(
                    req,
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "invalid_page_cursor",
                    "This cursor is not valid for this list.",
                    false,
                    None,
                ));
            }
        },
    };
    Ok((limit, before))
}

fn keyset_page<T>(
    mut items: Vec<T>,
    limit: usize,
    kind: &str,
    key: impl Fn(&T) -> (DateTime<Utc>, Uuid),
) -> (Vec<T>, Option<String>) {
    let has_more = items.len() > limit;
    items.truncate(limit);
    let next = has_more.then(|| {
        let (at, id) = key(items
            .last()
            .expect("a keyset page with a continuation is non-empty"));
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&json!({"version":1,"kind":kind,"at":at,"id":id}))
                .expect("keyset cursor serialization cannot fail"),
        )
    });
    (items, next)
}

macro_rules! keyset_list_handler {
    ($name:ident, $repo:ident, $kind:literal, $at:ident, $id:ident, $code:literal, $message:literal) => {
        async fn $name(
            req: HttpRequest,
            state: web::Data<AppState>,
            query: web::Query<ListQuery>,
        ) -> HttpResponse {
            if let Err(response) = guard(&req, &state) {
                return response;
            }
            let pool = match pool(&req, &state) {
                Ok(pool) => pool,
                Err(response) => return response,
            };
            let (limit, before) = match keyset_window(&req, &query, $kind) {
                Ok(window) => window,
                Err(response) => return response,
            };
            let fetch_limit = limit.saturating_add(1);
            match repository::$repo(pool, before, i64::try_from(fetch_limit).unwrap_or(101)).await {
                Ok(items) => {
                    let (items, next_before) =
                        keyset_page(items, limit, $kind, |item| (item.$at, item.$id));
                    no_store(HttpResponse::Ok().json(Page {
                        snapshot: Snapshot::current(),
                        items,
                        next_before,
                    }))
                }
                Err(_) => failure(&req, $code, $message),
            }
        }
    };
}

async fn list_upstreams(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<ListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let window = match list_window(&req, &query) {
        Ok(window) => window,
        Err(response) => return response,
    };
    match repository::upstreams(pool).await {
        Ok(items) => {
            let (items, next_before) = paginate(items, window);
            no_store(HttpResponse::Ok().json(Page {
                snapshot: Snapshot::current(),
                items,
                next_before,
            }))
        }
        Err(_) => failure(&req, "upstreams_unavailable", "Upstreams are unavailable."),
    }
}

async fn get_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::upstream(pool, id.into_inner()).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(item)),
        Ok(None) => not_found(&req, "upstream"),
        Err(_) => failure(&req, "upstreams_unavailable", "Upstream is unavailable."),
    }
}

async fn list_clients(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<ListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let (limit, before) = match keyset_window(&req, &query, "clients") {
        Ok(window) => window,
        Err(response) => return response,
    };
    match repository::clients_page(
        pool,
        before,
        i64::try_from(limit.saturating_add(1)).unwrap_or(101),
    )
    .await
    {
        Ok(items) => {
            let (items, next_before) =
                keyset_page(items, limit, "clients", |item| (item.created_at, item.id));
            no_store(HttpResponse::Ok().json(Page {
                snapshot: Snapshot::current(),
                items,
                next_before,
            }))
        }
        Err(_) => failure(&req, "clients_unavailable", "Clients are unavailable."),
    }
}

async fn get_client(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::client(pool, id.into_inner()).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(item)),
        Ok(None) => not_found(&req, "client"),
        Err(_) => failure(&req, "clients_unavailable", "Client is unavailable."),
    }
}

async fn list_requests(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<RequestListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let list_query = ListQuery {
        limit: query.limit,
        before: query.before.clone(),
    };
    let (limit, before) = match keyset_window(&req, &list_query, "requests") {
        Ok(window) => window,
        Err(response) => return response,
    };
    let filters = match request_filters(&req, &query) {
        Ok(filters) => filters,
        Err(response) => return response,
    };
    match tokio::try_join!(
        repository::requests_page(
            pool,
            before,
            i64::try_from(limit.saturating_add(1)).unwrap_or(101),
            &filters,
        ),
        repository::request_aggregate(pool, &filters),
    ) {
        Ok((items, aggregate)) => {
            let (items, next_before) = keyset_page(items, limit, "requests", |item| {
                (item.started_at, item.request_id)
            });
            no_store(HttpResponse::Ok().json(json!({
                "snapshot":Snapshot::current(),
                "items":items,
                "next_before":next_before,
                "aggregate":aggregate,
            })))
        }
        Err(_) => failure(&req, "requests_unavailable", "Requests are unavailable."),
    }
}
keyset_list_handler!(
    list_probes,
    probes_page,
    "probes",
    created_at,
    id,
    "probes_unavailable",
    "Probe runs are unavailable."
);
keyset_list_handler!(
    list_audit,
    audit_events_page,
    "audit",
    created_at,
    id,
    "audit_unavailable",
    "Audit events are unavailable."
);

async fn get_request(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::request(pool, id.into_inner()).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(item)),
        Ok(None) => not_found(&req, "request"),
        Err(_) => failure(&req, "requests_unavailable", "Request is unavailable."),
    }
}

async fn get_probe(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::probe(pool, id.into_inner()).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(item)),
        Ok(None) => not_found(&req, "probe"),
        Err(_) => failure(&req, "probes_unavailable", "Probe is unavailable."),
    }
}

async fn list_models(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<ListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let window = match list_window(&req, &query) {
        Ok(window) => window,
        Err(response) => return response,
    };
    match repository::admin_models(pool).await {
        Ok(items) => {
            let (items, next_before) = paginate(items, window);
            no_store(HttpResponse::Ok().json(Page {
                snapshot: Snapshot::current(),
                items,
                next_before,
            }))
        }
        Err(_) => failure(&req, "models_unavailable", "Models are unavailable."),
    }
}

async fn list_incidents(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<ListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let (limit, before) = match keyset_window(&req, &query, "incidents") {
        Ok(window) => window,
        Err(response) => return response,
    };
    match repository::incidents_page(
        pool,
        before,
        i64::try_from(limit.saturating_add(1)).unwrap_or(101),
    )
    .await
    {
        Ok(items) => {
            let (items, next_before) =
                keyset_page(items, limit, "incidents", |item| (item.started_at, item.id));
            no_store(HttpResponse::Ok().json(Page {
                snapshot: Snapshot::current(),
                items,
                next_before,
            }))
        }
        Err(_) => failure(&req, "incidents_unavailable", "Incidents are unavailable."),
    }
}

async fn get_routing_policy(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::routing_policy(pool).await {
        Ok(item) => no_store(HttpResponse::Ok().json(item)),
        Err(_) => failure(
            &req,
            "routing_policy_unavailable",
            "Routing policy is unavailable.",
        ),
    }
}

async fn get_settings(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::settings(pool).await {
        Ok(item) => no_store(HttpResponse::Ok().json(item)),
        Err(_) => failure(&req, "settings_unavailable", "Settings are unavailable."),
    }
}

async fn get_qa_run(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::qa_run(pool, id.into_inner()).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(item)),
        Ok(None) => not_found(&req, "qa_run"),
        Err(_) => failure(&req, "qa_unavailable", "QA run is unavailable."),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct QaRunInput {
    suite: String,
    live: bool,
    confirm_billable: bool,
}

async fn list_qa_runs(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<ListQuery>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let (limit, before) = match keyset_window(&req, &query, "qa-runs") {
        Ok(window) => window,
        Err(response) => return response,
    };
    match (
        repository::qa_runs(pool, limit.saturating_add(1), before).await,
        repository::qa_completion(pool, crate::BUILD_COMMIT, &super::qa::REQUIRED_SUITES).await,
    ) {
        (Ok(items), Ok(completion)) => {
            let (items, next_before) =
                keyset_page(items, limit, "qa-runs", |item| (item.created_at, item.id));
            no_store(HttpResponse::Ok().json(QaRunsPage {
                snapshot: Snapshot::current(),
                deployment_commit: crate::BUILD_COMMIT.to_owned(),
                items,
                completion,
                next_before,
            }))
        }
        _ => failure(&req, "qa_unavailable", "QA runs are unavailable."),
    }
}

async fn create_qa_run(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<QaRunInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let provider_identity = if input.live {
        if !crate::provider::live_qa_provider_is_canonical(&state.upstream_url) {
            return operations_error(
                &req,
                StatusCode::UNPROCESSABLE_ENTITY,
                "live_nvidia_provider_required",
                "Live QA requires canonical NVIDIA hosted API endpoints.",
                false,
                None,
            );
        }
        repository::QaProviderIdentity::NvidiaHosted
    } else {
        repository::QaProviderIdentity::Fake
    };
    if input.live && matches!(input.suite.as_str(), "multimodal") && !input.confirm_billable {
        return operations_error(
            &req,
            StatusCode::CONFLICT,
            "billable_confirmation_required",
            "Live multimodal QA requires explicit billable confirmation.",
            false,
            None,
        );
    }
    if !input.live && input.suite == "hermes-e2e" {
        return operations_error(
            &req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "hermes_live_required",
            "Hermes E2E QA requires live mode.",
            false,
            None,
        );
    }
    match repository::create_qa_run(
        pool,
        &input.suite,
        input.live,
        provider_identity,
        request_id(&req),
    )
    .await
    {
        Ok((item, audit_event_id)) => {
            super::qa::spawn(state.clone(), item.id);
            no_store(HttpResponse::Accepted().json(AuditMutation {
                item,
                audit_event_id,
            }))
        }
        Err(repository::CreateQaRunError::UnsupportedSuite) => operations_error(
            &req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_qa_suite",
            "The requested QA suite is not supported.",
            false,
            None,
        ),
        Err(repository::CreateQaRunError::HermesLiveRequired) => operations_error(
            &req,
            StatusCode::UNPROCESSABLE_ENTITY,
            "hermes_live_required",
            "Hermes E2E QA requires live mode.",
            false,
            None,
        ),
        Err(repository::CreateQaRunError::ActiveRun(active_run_id)) => operations_error(
            &req,
            StatusCode::CONFLICT,
            "qa_run_active",
            "Another QA run is already queued or running.",
            true,
            Some(json!({"active_run_id": active_run_id})),
        ),
        Err(repository::CreateQaRunError::Internal(_)) => {
            failure(&req, "qa_unavailable", "QA run could not be created.")
        }
    }
}

async fn complete_hermes_qa_run(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
    input: web::Json<super::qa::HermesCompletion>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match super::qa::complete_hermes(pool, id.into_inner(), &input, request_id(&req)).await {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Ok().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(error)
            if error.to_string().contains("not found")
                || error.to_string().contains("not a Hermes") =>
        {
            not_found(&req, "qa_run")
        }
        Err(error)
            if error.to_string().contains("not running")
                || error.to_string().contains("requires")
                || error.to_string().contains("outside")
                || error.to_string().contains("disagrees")
                || error.to_string().contains("duplicate")
                || error.to_string().contains("unreported") =>
        {
            operations_error(
                &req,
                StatusCode::CONFLICT,
                "hermes_completion_rejected",
                "Hermes completion evidence did not satisfy the armed QA contract.",
                false,
                None,
            )
        }
        Err(_) => failure(
            &req,
            "qa_unavailable",
            "Hermes QA completion could not be stored.",
        ),
    }
}

async fn get_secret_scan(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match super::qa::database_secret_matches(pool).await {
        Ok(database_matches) => no_store(HttpResponse::Ok().json(json!({
            "schema_version":"nblb.secret-scan.v1",
            "database_matches":database_matches
        }))),
        Err(_) => failure(
            &req,
            "qa_unavailable",
            "Persisted secret evidence could not be scanned.",
        ),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct UpstreamInput {
    label: String,
    credential: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileProbeInput {
    profile_ids: Vec<String>,
    confirm_billable: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ClientInput {
    label: String,
    scopes: Vec<String>,
    expires_at: Option<DateTime<Utc>>,
    model_allowlist: Option<Vec<String>>,
    rpm_limit: Option<u32>,
    max_concurrency: Option<u32>,
    request_limit_day: Option<u32>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ClientPatch {
    scopes: Option<Vec<String>>,
    #[serde(default)]
    expires_at: NullablePatch<DateTime<Utc>>,
    #[serde(default)]
    model_allowlist: NullablePatch<Vec<String>>,
    #[serde(default)]
    rpm_limit: NullablePatch<u32>,
    #[serde(default)]
    max_concurrency: NullablePatch<u32>,
    #[serde(default)]
    request_limit_day: NullablePatch<u32>,
}

#[derive(Debug, Default)]
enum NullablePatch<T> {
    #[default]
    Missing,
    Null,
    Value(T),
}

impl<'de, T> Deserialize<'de> for NullablePatch<T>
where
    T: Deserialize<'de>,
{
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Ok(match Option::<T>::deserialize(deserializer)? {
            Some(value) => Self::Value(value),
            None => Self::Null,
        })
    }
}

impl<T: Clone> NullablePatch<T> {
    fn apply(&self, current: Option<T>) -> Option<T> {
        match self {
            Self::Missing => current,
            Self::Null => None,
            Self::Value(value) => Some(value.clone()),
        }
    }

    fn is_missing(&self) -> bool {
        matches!(self, Self::Missing)
    }

    fn value(&self) -> Option<&T> {
        match self {
            Self::Value(value) => Some(value),
            Self::Missing | Self::Null => None,
        }
    }
}

fn invalid_allowlist(value: Option<&Vec<String>>) -> bool {
    value.is_some_and(|items| {
        items.is_empty()
            || items.len() > 64
            || items
                .iter()
                .any(|item| item.is_empty() || !PROFILES.contains(&item.as_str()))
    })
}

fn invalid_client_limits(
    expires_at: Option<&DateTime<Utc>>,
    model_allowlist: Option<&Vec<String>>,
    rpm_limit: Option<u32>,
    max_concurrency: Option<u32>,
    request_limit_day: Option<u32>,
) -> bool {
    expires_at.is_some_and(|value| *value <= Utc::now())
        || invalid_allowlist(model_allowlist)
        || rpm_limit.is_some_and(|value| !(1..=100_000).contains(&value))
        || max_concurrency.is_some_and(|value| !(1..=64).contains(&value))
        || request_limit_day.is_some_and(|value| !(1..=10_000_000).contains(&value))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RoutingPolicyPatch {
    retryable_statuses: Vec<u16>,
    default_cooldown_seconds: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RoutingSimulationInput {
    profile_id: String,
    endpoint: String,
    stream: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelProbeInput {
    model_id: String,
    upstream_ids: Vec<Uuid>,
    confirm_billable: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct IncidentInput {
    slug: String,
    title: String,
    status: String,
    severity: String,
    public: bool,
    public_message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct IncidentPatch {
    title: Option<String>,
    status: Option<String>,
    severity: Option<String>,
    public: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct IncidentUpdateInput {
    status: String,
    public_message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SettingsPatch {
    proof_freshness_seconds: Option<u32>,
    request_retention_days: Option<u32>,
    metric_retention_days: Option<u32>,
    public_incidents_enabled: Option<bool>,
}

fn invalid(req: &HttpRequest, code: &str, message: &str) -> HttpResponse {
    operations_error(
        req,
        StatusCode::UNPROCESSABLE_ENTITY,
        code,
        message,
        false,
        None,
    )
}

async fn execute_profile_probe_inner(
    state: &AppState,
    upstream_id: Uuid,
    spec: &super::dto::ModelSpec,
) -> Result<u16, (Option<u16>, &'static str)> {
    let credential = state
        .vault
        .credential_for_probe(upstream_id)
        .map_err(|_| (None, "credential_unavailable"))?;
    if state.upstream_url.starts_with("mock://") {
        return if credential.contains("fail") {
            Err((Some(401), "upstream_auth_error"))
        } else {
            Ok(200)
        };
    }
    let endpoint = crate::upstream_endpoint_for(&state.upstream_url, spec.endpoint, spec.id);
    let tiny_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    let builder = state
        .client
        .post(&endpoint)
        .bearer_auth(&credential)
        .header(reqwest::header::ACCEPT, "application/json");
    let builder = match spec.endpoint {
        "/v1/chat/completions" if spec.id == "microsoft/phi-4-multimodal-instruct" => builder.json(&json!({
            "model":spec.id,"messages":[{"role":"user","content":[{"type":"text","text":"Reply OK"},{"type":"image_url","image_url":{"url":tiny_png}}]}],"max_tokens":2,"stream":false
        })),
        "/v1/chat/completions" => builder.json(&json!({"model":spec.id,"messages":[{"role":"user","content":"Reply OK"}],"max_tokens":2,"stream":false})),
        "/v1/embeddings" => builder.json(&json!({"model":spec.id,"input":["probe"],"encoding_format":"float"})),
        "/v1/images/generations" => builder.json(&json!({"prompt":"a green square","image":null,"aspect_ratio":"1:1","samples":1})),
        "/v1/videos/generations" => builder.json(&json!({"image":tiny_png,"seed":0,"cfg_scale":1.8,"motion_bucket_id":127})),
        "/v1/audio/speech" => builder.json(&json!({"model":spec.id,"input":"OK","voice":"Magpie-Multilingual.EN-US.Aria","response_format":"wav"})),
        "/v1/audio/transcriptions" => {
            let wav = vec![
                b'R',b'I',b'F',b'F',36,0,0,0,b'W',b'A',b'V',b'E',b'f',b'm',b't',b' ',16,0,0,0,1,0,1,0,64,31,0,0,128,62,0,0,2,0,16,0,b'd',b'a',b't',b'a',0,0,0,0,
            ];
            let part = reqwest::multipart::Part::bytes(wav).file_name("probe.wav").mime_str("audio/wav").map_err(|_|(None,"probe_fixture_invalid"))?;
            builder.multipart(reqwest::multipart::Form::new().text("model",spec.id.to_owned()).part("file",part))
        }
        "/v1/nvidia/inference" => builder.json(&json!({"messages":[{"role":"user","content":[{"type":"text","text":"Describe in one word"},{"type":"image_url","image_url":{"url":tiny_png}}]}],"max_tokens":2})),
        _ => return Err((None,"unsupported_probe_profile")),
    };
    let response = builder
        .timeout(crate::UPSTREAM_REQUEST_TIMEOUT)
        .send()
        .await
        .map_err(|_| (None, "upstream_unavailable"))?;
    let response = if response.status().as_u16() == 202 {
        crate::poll_nvcf(&state.client, response, &endpoint, &credential)
            .await
            .map_err(|_| (Some(202), "upstream_poll_error"))?
    } else {
        response
    };
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        let class = match status {
            401 | 403 => "upstream_auth_error",
            429 => "upstream_rate_limited",
            500..=599 => "upstream_server_error",
            _ => "upstream_probe_rejected",
        };
        return Err((Some(status), class));
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    let body = response
        .bytes()
        .await
        .map_err(|_| (Some(status), "upstream_body_error"))?;
    let valid = if spec.endpoint == "/v1/chat/completions" {
        content_type
            .split(';')
            .next()
            .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"))
            && crate::validate_chat_response(&body, false).is_ok()
    } else {
        crate::normalize_modality_response(spec.endpoint, &body, &content_type).is_ok()
    };
    if !valid {
        return Err((Some(status), "upstream_protocol_error"));
    }
    Ok(status)
}

#[derive(Clone, Copy, Debug)]
struct ProbeExecution {
    status: Option<u16>,
    error: Option<&'static str>,
    latency_ms: u64,
}

async fn execute_profile_probe(
    state: &AppState,
    upstream_id: Uuid,
    spec: &super::dto::ModelSpec,
) -> ProbeExecution {
    let started = std::time::Instant::now();
    let result = execute_profile_probe_inner(state, upstream_id, spec).await;
    let latency_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
    match result {
        Ok(status) => ProbeExecution {
            status: Some(status),
            error: None,
            latency_ms,
        },
        Err((status, error)) => ProbeExecution {
            status,
            error: Some(error),
            latency_ms,
        },
    }
}

async fn create_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<UpstreamInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let (summary, audit_event_id) = match state
        .vault
        .mutate_with_audit(
            |vault| vault.add(&input.label, &input.credential),
            |summary| VaultAuditMutation {
                action: "upstream.create",
                resource_kind: "upstream",
                resource_id: Some(summary.id),
                request_id: request_id(&req),
                detail: json!({"changed":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
    {
        Ok(result) => result,
        Err(error)
            if error.to_string().contains("already exists")
                || error.to_string().contains("at most two") =>
        {
            return operations_error(
                &req,
                StatusCode::CONFLICT,
                "resource_conflict",
                "No empty upstream slot is available or this credential already exists.",
                false,
                None,
            );
        }
        Err(_) => {
            return invalid(
                &req,
                "invalid_upstream",
                "The upstream label or credential is invalid.",
            );
        }
    };
    let item = match repository::upstream(pool, summary.id).await {
        Ok(Some(item)) => item,
        _ => {
            return failure(
                &req,
                "upstream_unavailable",
                "The created upstream could not be loaded.",
            );
        }
    };
    no_store(HttpResponse::Created().json(AuditMutation {
        item,
        audit_event_id,
    }))
}

async fn probe_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let upstream_id = id.into_inner();
    let spec = &super::dto::MODEL_SPECS[0];
    let result = execute_profile_probe(&state, upstream_id, spec).await;
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let status = result.status;
    let error = result.error;
    let latency_ms = result.latency_ms;
    if error.is_none() || matches!(status, Some(401 | 403)) {
        let probe_id = Uuid::new_v4();
        let probe_result = ProbeAuditMutation {
            id: probe_id,
            kind: "credential",
            upstream_id,
            profile_id: None,
            receipt_profile: Some(spec.id),
            billable: false,
            status_code: status,
            error_class: error,
            latency_ms,
        };
        let request_id = request_id(&req);
        let mutation = |_: &()| VaultAuditMutation {
            action: "probe.complete",
            resource_kind: "probe",
            resource_id: Some(probe_id),
            request_id,
            detail: json!({"billable":false,"kind":"credential","passed":error.is_none()}),
            client_policy: None,
            probe_result: Some(probe_result.clone()),
        };
        let persisted = if error.is_none() {
            state
                .vault
                .mutate_with_audit(
                    |vault| vault.mark_verified(upstream_id).map(|_| ()),
                    mutation,
                )
                .await
        } else {
            state
                .vault
                .mutate_with_audit(|vault| vault.quarantine(upstream_id).map(|_| ()), mutation)
                .await
        };
        let (_, audit_event_id) = match persisted {
            Ok(value) => value,
            Err(_) => {
                return failure(
                    &req,
                    "probe_persistence_failed",
                    "Credential verification and evidence could not be stored atomically.",
                );
            }
        };
        let item = match repository::probe(pool, probe_id).await {
            Ok(Some(item)) => item,
            _ => {
                return failure(
                    &req,
                    "probe_persistence_failed",
                    "Credential probe evidence could not be loaded.",
                );
            }
        };
        return no_store(if error.is_none() {
            HttpResponse::Ok().json(AuditMutation {
                item,
                audit_event_id,
            })
        } else {
            HttpResponse::UnprocessableEntity().json(AuditMutation {
                item,
                audit_event_id,
            })
        });
    }
    match repository::record_probe_result(
        pool,
        repository::ProbeResultInput {
            kind: "credential",
            upstream_id,
            profile_id: None,
            receipt_profile: Some(spec.id),
            billable: false,
            status_code: status,
            error_class: error,
            latency_ms,
            request_id: request_id(&req),
        },
    )
    .await
    {
        Ok((item, audit_event_id)) if error.is_none() => {
            no_store(HttpResponse::Ok().json(AuditMutation {
                item,
                audit_event_id,
            }))
        }
        Ok((item, audit_event_id)) => {
            no_store(HttpResponse::UnprocessableEntity().json(AuditMutation {
                item,
                audit_event_id,
            }))
        }
        Err(_) => failure(
            &req,
            "probe_persistence_failed",
            "Credential probe evidence could not be stored.",
        ),
    }
}

async fn probe_upstream_profiles(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
    input: web::Json<ProfileProbeInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    if input.profile_ids.is_empty()
        || input
            .profile_ids
            .iter()
            .any(|profile| !PROFILES.contains(&profile.as_str()))
    {
        return invalid(
            &req,
            "invalid_profile",
            "profiles must contain supported model IDs.",
        );
    }
    let billable = input.profile_ids.iter().any(|profile| {
        super::dto::MODEL_SPECS
            .iter()
            .any(|spec| spec.id == profile && spec.billable_probe)
    });
    if billable && !input.confirm_billable {
        return operations_error(
            &req,
            StatusCode::CONFLICT,
            "billable_confirmation_required",
            "Billable profile probes require explicit confirmation.",
            false,
            None,
        );
    }
    let upstream_id = id.into_inner();
    if state.vault.credential_for_probe(upstream_id).is_err() {
        return not_found(&req, "upstream");
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let mut runs = Vec::new();
    let mut audit_event_id = None;
    for profile in &input.profile_ids {
        let Some(spec) = super::dto::MODEL_SPECS
            .iter()
            .find(|spec| spec.id == profile)
        else {
            return invalid(&req, "invalid_profile", "The profile is not supported.");
        };
        let result = execute_profile_probe(&state, upstream_id, spec).await;
        let status = result.status;
        let error = result.error;
        let (run, run_audit_id) = match repository::record_probe_result(
            pool,
            repository::ProbeResultInput {
                kind: "profile",
                upstream_id,
                profile_id: Some(spec.id),
                receipt_profile: Some(spec.id),
                billable: spec.billable_probe,
                status_code: status,
                error_class: error,
                latency_ms: result.latency_ms,
                request_id: request_id(&req),
            },
        )
        .await
        {
            Ok(result) => result,
            Err(_) => {
                return failure(
                    &req,
                    "probe_persistence_failed",
                    "Profile probe evidence could not be stored.",
                );
            }
        };
        audit_event_id = Some(run_audit_id);
        runs.push(run);
        if error.is_some() {
            return no_store(
                HttpResponse::UnprocessableEntity()
                    .json(json!({"items":runs,"failed_profile":spec.id})),
            );
        }
    }
    let Some(audit_event_id) = audit_event_id else {
        return invalid(&req, "invalid_profile", "At least one profile is required.");
    };
    let item = match repository::upstream(pool, upstream_id).await {
        Ok(Some(item)) => item,
        _ => return not_found(&req, "upstream"),
    };
    no_store(
        HttpResponse::Ok().json(json!({"item":item,"runs":runs,"audit_event_id":audit_event_id})),
    )
}

async fn set_upstream_state(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: Uuid,
    enabled: bool,
) -> HttpResponse {
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let action = if enabled {
        "upstream.enable"
    } else {
        "upstream.disable"
    };
    let audit_event_id = match state
        .vault
        .mutate_with_audit(
            |vault| vault.set_enabled(id, enabled),
            |_| VaultAuditMutation {
                action,
                resource_kind: "upstream",
                resource_id: Some(id),
                request_id: request_id(&req),
                detail: json!({"changed":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
    {
        Ok((_, audit_event_id)) => audit_event_id,
        Err(error) if error.to_string().contains("probe") => {
            return operations_error(
                &req,
                StatusCode::CONFLICT,
                "probe_required",
                "Provider probe is required before enabling this upstream.",
                false,
                None,
            );
        }
        Err(_) => return not_found(&req, "upstream"),
    };
    let item = match repository::upstream(pool, id).await {
        Ok(Some(item)) => item,
        _ => return not_found(&req, "upstream"),
    };
    no_store(HttpResponse::Ok().json(AuditMutation {
        item,
        audit_event_id,
    }))
}

async fn enable_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    set_upstream_state(req, state, id.into_inner(), true).await
}
async fn disable_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    set_upstream_state(req, state, id.into_inner(), false).await
}
async fn retire_upstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let id = id.into_inner();
    let audit_event_id = match state
        .vault
        .mutate_with_audit(
            |vault| vault.delete(id),
            |_| VaultAuditMutation {
                action: "upstream.retire",
                resource_kind: "upstream",
                resource_id: Some(id),
                request_id: request_id(&req),
                detail: json!({"changed":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
    {
        Ok((_, audit_event_id)) => audit_event_id,
        Err(_) => return not_found(&req, "upstream"),
    };
    no_store(
        HttpResponse::Ok()
            .json(json!({"item":{"id":id,"retired":true},"audit_event_id":audit_event_id})),
    )
}

async fn create_client(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<ClientInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if invalid_client_limits(
        input.expires_at.as_ref(),
        input.model_allowlist.as_ref(),
        input.rpm_limit,
        input.max_concurrency,
        input.request_limit_day,
    ) {
        return invalid(
            &req,
            "invalid_client_policy",
            "One or more client policy fields are outside the supported bounds.",
        );
    }
    let (issued, audit_event_id) = match state
        .vault
        .mutate_with_audit(
            |vault| vault.issue_downstream(&input.label, &input.scopes),
            |issued| VaultAuditMutation {
                action: "client.create",
                resource_kind: "client",
                resource_id: Some(issued.summary.id),
                request_id: request_id(&req),
                detail: json!({"policy_updated":true}),
                client_policy: Some(ClientPolicyMutation {
                    id: issued.summary.id,
                    expires_at: input.expires_at,
                    expires_at_changed: true,
                    model_allowlist: input.model_allowlist.clone(),
                    model_allowlist_changed: true,
                    rpm_limit: input.rpm_limit,
                    rpm_limit_changed: true,
                    max_concurrency: input.max_concurrency,
                    max_concurrency_changed: true,
                    request_limit_day: input.request_limit_day,
                    request_limit_day_changed: true,
                }),
                probe_result: None,
            },
        )
        .await
    {
        Ok(result) => result,
        Err(error) if error.to_string().contains("already exists") => {
            return operations_error(
                &req,
                StatusCode::CONFLICT,
                "resource_conflict",
                "An active client with this label already exists.",
                false,
                None,
            );
        }
        Err(_) => {
            return invalid(
                &req,
                "invalid_client",
                "The client label or scopes are invalid.",
            );
        }
    };
    let item = match repository::client(pool, issued.summary.id).await {
        Ok(Some(item)) => item,
        _ => return failure(&req, "client_unavailable", "Client could not be loaded."),
    };
    no_store(
        HttpResponse::Created()
            .json(json!({"item":item,"token":issued.token,"audit_event_id":audit_event_id})),
    )
}

async fn update_client(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
    input: web::Json<ClientPatch>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let id = id.into_inner();
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let current = match repository::client(pool, id).await {
        Ok(Some(item)) => item,
        _ => return not_found(&req, "client"),
    };
    if input.scopes.is_none()
        && input.expires_at.is_missing()
        && input.model_allowlist.is_missing()
        && input.rpm_limit.is_missing()
        && input.max_concurrency.is_missing()
        && input.request_limit_day.is_missing()
    {
        return invalid(
            &req,
            "empty_patch",
            "At least one client policy field is required.",
        );
    }
    if invalid_client_limits(
        input.expires_at.value(),
        input.model_allowlist.value(),
        input.rpm_limit.value().copied(),
        input.max_concurrency.value().copied(),
        input.request_limit_day.value().copied(),
    ) {
        return invalid(
            &req,
            "invalid_client_policy",
            "One or more client policy fields are outside the supported bounds.",
        );
    }
    let scopes = input
        .scopes
        .clone()
        .unwrap_or_else(|| current.scopes.clone());
    let policy = ClientPolicyMutation {
        id,
        expires_at: input.expires_at.apply(current.expires_at),
        expires_at_changed: !input.expires_at.is_missing(),
        model_allowlist: input.model_allowlist.apply(current.model_allowlist),
        model_allowlist_changed: !input.model_allowlist.is_missing(),
        rpm_limit: input.rpm_limit.apply(current.rpm_limit),
        rpm_limit_changed: !input.rpm_limit.is_missing(),
        max_concurrency: input.max_concurrency.apply(current.max_concurrency),
        max_concurrency_changed: !input.max_concurrency.is_missing(),
        request_limit_day: input.request_limit_day.apply(current.request_limit_day),
        request_limit_day_changed: !input.request_limit_day.is_missing(),
    };
    let audit_event_id = match state
        .vault
        .mutate_with_audit(
            |vault| vault.update_downstream_scopes(id, &scopes),
            |_| VaultAuditMutation {
                action: "client.update",
                resource_kind: "client",
                resource_id: Some(id),
                request_id: request_id(&req),
                detail: json!({"policy_updated":true}),
                client_policy: Some(policy),
                probe_result: None,
            },
        )
        .await
    {
        Ok((_, audit_event_id)) => audit_event_id,
        Err(error) if error.to_string().contains("scope") => {
            return invalid(&req, "invalid_scopes", "Client scopes are invalid.");
        }
        Err(_) => return failure(&req, "client_unavailable", "Client could not be updated."),
    };
    match repository::client(pool, id).await {
        Ok(Some(item)) => no_store(HttpResponse::Ok().json(AuditMutation {
            item,
            audit_event_id,
        })),
        _ => failure(&req, "client_unavailable", "Client could not be loaded."),
    }
}

async fn rotate_client(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let id = id.into_inner();
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let (issued, audit_event_id) = match state
        .vault
        .mutate_with_audit(
            |vault| vault.rotate_downstream(id),
            |_| VaultAuditMutation {
                action: "client.rotate",
                resource_kind: "client",
                resource_id: Some(id),
                request_id: request_id(&req),
                detail: json!({"changed":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
    {
        Ok(result) => result,
        Err(_) => return not_found(&req, "client"),
    };
    let item = match repository::client(pool, id).await {
        Ok(Some(item)) => item,
        _ => return not_found(&req, "client"),
    };
    no_store(
        HttpResponse::Ok()
            .json(json!({"item":item,"token":issued.token,"audit_event_id":audit_event_id})),
    )
}

async fn revoke_client(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let id = id.into_inner();
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let audit_event_id = match state
        .vault
        .mutate_with_audit(
            |vault| vault.revoke_downstream(id),
            |_| VaultAuditMutation {
                action: "client.revoke",
                resource_kind: "client",
                resource_id: Some(id),
                request_id: request_id(&req),
                detail: json!({"changed":true}),
                client_policy: None,
                probe_result: None,
            },
        )
        .await
    {
        Ok((_, audit_event_id)) => audit_event_id,
        Err(_) => return not_found(&req, "client"),
    };
    let item = match repository::client(pool, id).await {
        Ok(Some(item)) => item,
        _ => return not_found(&req, "client"),
    };
    no_store(HttpResponse::Ok().json(AuditMutation {
        item,
        audit_event_id,
    }))
}

async fn update_routing_policy(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<RoutingPolicyPatch>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    if input.default_cooldown_seconds == 0
        || input.default_cooldown_seconds > 3600
        || input.retryable_statuses.is_empty()
    {
        return invalid(
            &req,
            "invalid_routing_policy",
            "The routing policy is outside the supported bounds.",
        );
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::update_routing_policy(
        pool,
        &input.retryable_statuses,
        input.default_cooldown_seconds,
        request_id(&req),
    )
    .await
    {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Ok().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(_) => failure(
            &req,
            "routing_policy_unavailable",
            "Routing policy could not be updated.",
        ),
    }
}

async fn simulate_routing(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<RoutingSimulationInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    if !PROFILES.contains(&input.profile_id.as_str()) {
        return invalid(&req, "invalid_profile", "The profile is not supported.");
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let items = match repository::upstreams(pool).await {
        Ok(items) => items,
        Err(_) => {
            return failure(
                &req,
                "routing_unavailable",
                "Routing simulation is unavailable.",
            );
        }
    };
    let profile_eligible = match public_repository::eligible_key_ids(pool, &input.profile_id).await
    {
        Ok(ids) => ids,
        Err(_) => {
            return failure(
                &req,
                "routing_unavailable",
                "Routing simulation is unavailable.",
            );
        }
    };
    let next_slot = sqlx::query_scalar::<_, i16>(
        "SELECT next_slot FROM nblb.routing_state WHERE profile_id=$1",
    )
    .bind(&input.profile_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
    .unwrap_or(1);
    let mut eligible = items
        .into_iter()
        .filter(|item| profile_eligible.contains(&item.id))
        .collect::<Vec<_>>();
    eligible.sort_by_key(|item| {
        (
            item.slot_no != u8::try_from(next_slot).unwrap_or(1),
            item.slot_no,
        )
    });
    let selected_slot = eligible.first().map(|item| item.slot_no);
    let eligible_order = eligible.iter().map(|item| item.id).collect::<Vec<_>>();
    let retry_allowed = !matches!(
        input.endpoint.as_str(),
        "/v1/images/generations" | "/v1/videos/generations"
    );
    let mut reason_codes = Vec::new();
    if eligible_order.is_empty() {
        reason_codes.push("no_eligible_upstream");
    }
    if input.stream {
        reason_codes.push("first_frame_boundary_applies");
    }
    no_store(HttpResponse::Ok().json(json!({
        "selected_slot":selected_slot,
        "eligible_order":eligible_order,
        "retry_allowed":retry_allowed,
        "reason_codes":reason_codes,
        "would_advance_generation":selected_slot.is_some(),
        "would_persist":false
    })))
}

async fn sync_models(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::sync_model_catalog(pool, request_id(&req)).await {
        Ok((items, audit_event_id)) => no_store(
            HttpResponse::Ok().json(json!({"items":items,"audit_event_id":audit_event_id})),
        ),
        Err(_) => failure(
            &req,
            "models_unavailable",
            "Model catalog could not be synchronized.",
        ),
    }
}

async fn probe_model(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<ModelProbeInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let Some(spec) = super::dto::MODEL_SPECS
        .iter()
        .find(|spec| spec.id == input.model_id)
    else {
        return invalid(&req, "invalid_model", "The model is not supported.");
    };
    if input.upstream_ids.is_empty()
        || input.upstream_ids.len() > 2
        || input.upstream_ids.len() == 2 && input.upstream_ids[0] == input.upstream_ids[1]
    {
        return invalid(
            &req,
            "invalid_upstream_ids",
            "One or two distinct upstream IDs are required.",
        );
    }
    if spec.billable_probe && !input.confirm_billable {
        return operations_error(
            &req,
            StatusCode::CONFLICT,
            "billable_confirmation_required",
            "This model probe may be billable.",
            false,
            None,
        );
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let mut probe_runs = Vec::new();
    let mut audit_event_id = None;
    let mut failed = false;
    for upstream_id in &input.upstream_ids {
        let result = execute_profile_probe(&state, *upstream_id, spec).await;
        let status = result.status;
        let error = result.error;
        match repository::record_probe_result(
            pool,
            repository::ProbeResultInput {
                kind: "profile",
                upstream_id: *upstream_id,
                profile_id: Some(spec.id),
                receipt_profile: Some(spec.id),
                billable: spec.billable_probe,
                status_code: status,
                error_class: error,
                latency_ms: result.latency_ms,
                request_id: request_id(&req),
            },
        )
        .await
        {
            Ok((run, audit)) => {
                probe_runs.push(run);
                audit_event_id = Some(audit);
                failed |= error.is_some();
            }
            Err(_) => {
                return failure(
                    &req,
                    "probe_persistence_failed",
                    "Model probe evidence could not be stored.",
                );
            }
        }
    }
    let models = match repository::admin_models(pool).await {
        Ok(models) => models,
        Err(_) => return failure(&req, "models_unavailable", "Model state is unavailable."),
    };
    let Some(model) = models.into_iter().find(|model| model.id == input.model_id) else {
        return failure(&req, "models_unavailable", "Model state is unavailable.");
    };
    let body = json!({
        "model":model,
        "probe_runs":probe_runs,
        "audit_event_id":audit_event_id
    });
    if failed {
        no_store(HttpResponse::UnprocessableEntity().json(body))
    } else {
        no_store(HttpResponse::Accepted().json(body))
    }
}

async fn create_incident(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<IncidentInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::create_incident(
        pool,
        repository::CreateIncident {
            slug: &input.slug,
            title: &input.title,
            status: &input.status,
            severity: &input.severity,
            public: input.public,
            public_message: &input.public_message,
            request_id: request_id(&req),
        },
    )
    .await
    {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Created().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(_) => invalid(
            &req,
            "invalid_incident",
            "The incident fields are invalid or the slug already exists.",
        ),
    }
}
async fn update_incident(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
    input: web::Json<IncidentPatch>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let id = id.into_inner();
    if input.title.is_none()
        && input.status.is_none()
        && input.severity.is_none()
        && input.public.is_none()
    {
        return invalid(
            &req,
            "empty_patch",
            "At least one incident field is required.",
        );
    }
    match repository::update_incident(
        pool,
        id,
        input.title.as_deref(),
        input.status.as_deref(),
        input.severity.as_deref(),
        input.public,
        request_id(&req),
    )
    .await
    {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Ok().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(_) => invalid(
            &req,
            "invalid_incident",
            "The incident could not be updated.",
        ),
    }
}
async fn create_incident_update(
    req: HttpRequest,
    state: web::Data<AppState>,
    id: web::Path<Uuid>,
    input: web::Json<IncidentUpdateInput>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::create_incident_update(
        pool,
        id.into_inner(),
        &input.status,
        &input.public_message,
        request_id(&req),
    )
    .await
    {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Created().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(_) => invalid(
            &req,
            "invalid_incident_update",
            "The incident update is invalid.",
        ),
    }
}
async fn update_settings(
    req: HttpRequest,
    state: web::Data<AppState>,
    input: web::Json<SettingsPatch>,
) -> HttpResponse {
    if let Err(response) = guard(&req, &state) {
        return response;
    }
    let patch = input.into_inner();
    if patch.proof_freshness_seconds.is_none()
        && patch.request_retention_days.is_none()
        && patch.metric_retention_days.is_none()
        && patch.public_incidents_enabled.is_none()
    {
        return invalid(
            &req,
            "invalid_settings",
            "At least one setting is required.",
        );
    }
    let pool = match pool(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if patch
        .proof_freshness_seconds
        .is_some_and(|value| !(300..=2_592_000).contains(&value))
        || patch
            .request_retention_days
            .is_some_and(|value| !(7..=90).contains(&value))
        || patch
            .metric_retention_days
            .is_some_and(|value| !(7..=365).contains(&value))
    {
        return invalid(
            &req,
            "invalid_settings",
            "One or more settings are outside the supported range.",
        );
    }
    match repository::update_settings(
        pool,
        patch.proof_freshness_seconds,
        patch.request_retention_days,
        patch.metric_retention_days,
        patch.public_incidents_enabled,
        request_id(&req),
    )
    .await
    {
        Ok((item, audit_event_id)) => no_store(HttpResponse::Ok().json(AuditMutation {
            item,
            audit_event_id,
        })),
        Err(_) => failure(
            &req,
            "settings_unavailable",
            "Settings could not be updated.",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        ClientPatch, ListQuery, NullablePatch, QaRunInput, RequestListQuery, decode_keyset_cursor,
        keyset_page, owner_lease_attention, request_filters,
    };
    use actix_web::{http::StatusCode, test::TestRequest};
    use base64::Engine;
    use chrono::{TimeZone, Utc};
    use serde_json::json;
    use uuid::Uuid;

    #[test]
    fn exact_write_dtos_reject_unknown_fields() {
        assert!(
            serde_json::from_str::<QaRunInput>(
                r#"{"suite":"smoke","live":false,"confirm_billable":false,"prompt":"forbidden"}"#
            )
            .is_err()
        );
        let query = ListQuery {
            limit: Some(50),
            before: None,
        };
        assert_eq!(query.limit, Some(50));
    }

    #[test]
    fn owner_lease_recovery_is_an_in_place_recheck_not_a_noop_link() {
        let attention = owner_lease_attention();
        assert_eq!(attention.code, "owner_lease_stale");
        assert_eq!(attention.action.label, "lease 다시 확인");
        assert!(attention.action.href.is_empty());
        assert_ne!(attention.action.href, "/admin");
    }

    #[test]
    fn client_patch_distinguishes_missing_null_and_value() {
        let missing: ClientPatch =
            serde_json::from_str(r#"{"scopes":["chat:write"]}"#).expect("missing nullable fields");
        assert!(missing.rpm_limit.is_missing());
        assert_eq!(missing.rpm_limit.apply(Some(50)), Some(50));

        let cleared: ClientPatch = serde_json::from_str(
            r#"{"expires_at":null,"model_allowlist":null,"rpm_limit":null,"max_concurrency":null,"request_limit_day":null}"#,
        )
        .expect("explicit null fields");
        assert!(matches!(cleared.rpm_limit, NullablePatch::Null));
        assert_eq!(cleared.rpm_limit.apply(Some(50)), None);

        let value: ClientPatch = serde_json::from_str(
            r#"{"model_allowlist":["z-ai/glm-5.2"],"rpm_limit":100,"max_concurrency":4,"request_limit_day":1000}"#,
        )
        .expect("explicit policy values");
        assert_eq!(value.rpm_limit.apply(None), Some(100));
        assert_eq!(value.max_concurrency.apply(None), Some(4));
        assert_eq!(value.request_limit_day.apply(None), Some(1000));
    }

    #[test]
    fn keyset_cursor_rejects_invalid_version_kind_and_encoding() {
        let at = Utc
            .with_ymd_and_hms(2026, 7, 21, 12, 0, 0)
            .single()
            .expect("fixed timestamp");
        let id = Uuid::from_u128(42);
        let encode = |version: u8, kind: &str| {
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(
                serde_json::to_vec(&json!({
                    "version": version,
                    "kind": kind,
                    "at": at,
                    "id": id,
                }))
                .expect("serialize cursor"),
            )
        };

        assert_eq!(
            decode_keyset_cursor(&encode(1, "incidents"), "incidents"),
            Some((at, id))
        );
        assert_eq!(
            decode_keyset_cursor(&encode(2, "incidents"), "incidents"),
            None
        );
        assert_eq!(decode_keyset_cursor(&encode(1, "audit"), "incidents"), None);
        assert_eq!(decode_keyset_cursor("not-base64", "incidents"), None);
    }

    #[test]
    fn keyset_cursor_uses_timestamp_and_uuid_of_the_last_visible_item() {
        let at = Utc
            .with_ymd_and_hms(2026, 7, 21, 12, 0, 0)
            .single()
            .expect("fixed timestamp");
        let items = vec![
            (at, Uuid::from_u128(3)),
            (at, Uuid::from_u128(2)),
            (at, Uuid::from_u128(1)),
        ];
        let (visible, cursor) = keyset_page(items, 2, "incidents", |item| *item);
        assert_eq!(visible.len(), 2);
        assert_eq!(
            decode_keyset_cursor(cursor.as_deref().expect("continuation cursor"), "incidents"),
            Some((at, Uuid::from_u128(2)))
        );
    }

    #[test]
    fn request_filters_accept_canonical_values_and_reject_invalid_contracts() {
        let req = TestRequest::default().to_http_request();
        let accepted = request_filters(
            &req,
            &RequestListQuery {
                outcome: Some("rejected".into()),
                endpoint: Some("/v1/nvidia/inference".into()),
                ..RequestListQuery::default()
            },
        )
        .expect("canonical rejected request filter");
        assert_eq!(accepted.outcome.as_deref(), Some("rejected"));
        assert_eq!(accepted.endpoint.as_deref(), Some("/v1/nvidia/inference"));

        for query in [
            RequestListQuery {
                request_id: Some("not-a-uuid".into()),
                ..RequestListQuery::default()
            },
            RequestListQuery {
                client_id: Some("not-a-uuid".into()),
                ..RequestListQuery::default()
            },
            RequestListQuery {
                outcome: Some("unknown".into()),
                ..RequestListQuery::default()
            },
            RequestListQuery {
                endpoint: Some("/v1/unknown".into()),
                ..RequestListQuery::default()
            },
            RequestListQuery {
                since: Some("yesterday".into()),
                ..RequestListQuery::default()
            },
            RequestListQuery {
                since: Some("2026-07-21T13:00:00Z".into()),
                until: Some("2026-07-21T12:00:00Z".into()),
                ..RequestListQuery::default()
            },
        ] {
            let response = request_filters(&req, &query).expect_err("invalid filter must fail");
            assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
        }
    }
}
