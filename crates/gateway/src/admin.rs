use actix_web::{HttpRequest, HttpResponse, Responder, web};
use anyhow::{Context, Result};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use uuid::Uuid;

use super::{
    AppState, DownstreamInput, KeyInput, PROFILES, ToggleInput, UPSTREAM_REQUEST_TIMEOUT,
    admin_unauthorized, authorize_scope, authorized, eligible_key_count, public_guard_response,
    quarantine_key, upstream_endpoint_for, validate_chat_response,
};

pub(crate) async fn operator_readiness(
    req: HttpRequest,
    state: web::Data<AppState>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let database_ready = match &state.vault.database {
        Some(pool) => sqlx::query_scalar::<_, i32>("SELECT 1")
            .fetch_one(pool)
            .await
            .is_ok(),
        None => true,
    };
    if !database_ready {
        return HttpResponse::ServiceUnavailable().json(json!({
            "runtime_state": "unavailable",
            "readiness_cause": "runtime_unavailable",
            "ledger_status": "capacity_blocked",
            "capacity_blocker": "orphaned_pending"
        }));
    }
    let eligible = eligible_key_count(&state.vault.list());
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({
            "runtime_state": "operational",
            "readiness_cause": if eligible > 0 { "ready" } else { "no_eligible_upstream" },
            "ledger_status": "ok",
            "capacity_blocker": "none"
        }))
}

/// Returns the durable profile/key provider receipts. A successful key-level
/// probe alone must never make every modality appear ready.
pub(crate) async fn profile_proof_keys(state: &AppState) -> Result<HashMap<String, HashSet<Uuid>>> {
    let Some(pool) = &state.vault.database else {
        return Ok(HashMap::new());
    };
    let rows = sqlx::query_as::<_, (String, Uuid)>(
        "SELECT profile_id, key_id FROM nblb.profile_probe_receipts",
    )
    .fetch_all(pool)
    .await
    .context("load profile provider receipts")?;
    let mut proofs = HashMap::new();
    for (profile, key_id) in rows {
        proofs
            .entry(profile)
            .or_insert_with(HashSet::new)
            .insert(key_id);
    }
    Ok(proofs)
}

pub(crate) async fn models(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    if let Err(response) = authorize_scope(&req, &state, "models:read").await {
        return response;
    }
    // Keep the public OpenAI-compatible model catalog discoverable before a
    // first request succeeds. Provider proof is an operational readiness
    // signal, exposed by the admin capability/readiness endpoints below; it
    // must not create a discovery deadlock for clients that need the model ID
    // in order to make that first request.
    let data: Vec<Value> = PROFILES
        .iter()
        .map(|id| json!({"id": id, "object": "model", "owned_by": "nvidia", "created": 1784332800}))
        .collect();
    HttpResponse::Ok().json(json!({"object":"list","data":data}))
}

pub(crate) async fn list_keys(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let items = state.vault.list();
    HttpResponse::Ok().json(json!({"items": items}))
}

pub(crate) async fn overview(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let keys = state.vault.list();
    let downstream = state.vault.list_downstream();
    let eligible_keys = eligible_key_count(&keys);
    let database_ready = match &state.vault.database {
        Some(pool) => sqlx::query_scalar::<_, i32>("SELECT 1")
            .fetch_one(pool)
            .await
            .is_ok(),
        None => true,
    };
    let evidence = state
        .vault
        .evidence()
        .await
        .unwrap_or_else(|_| json!({"source_of_truth": "unavailable"}));
    let recommendation = if keys.is_empty() {
        json!({"action":"add_upstream_key","label":"첫 번째 키 구성","route":"routing","reason":"두 개의 NVIDIA 키를 저장해야 요청을 받을 수 있습니다."})
    } else if keys.iter().any(|key| !key.verified) {
        json!({"action":"probe_upstream_key","label":"제공자 검증","route":"routing","reason":"저장된 키는 제공자 검증을 통과해야 라우팅에 포함할 수 있습니다."})
    } else if eligible_keys == 0 {
        json!({"action":"inspect_routing","label":"라우팅 상태 확인","route":"routing","reason":"현재 요청 가능한 키가 없어 cooldown·중지 원인을 확인해야 합니다."})
    } else if keys.len() < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
        json!({"action":"add_upstream_key","label":"두 번째 키 구성","route":"routing","reason":"두 번째 키를 추가하면 rate-aware 분산과 장애 전환을 확인할 수 있습니다."})
    } else {
        json!({"action":"monitor","label":"현재 상태 확인","route":"evidence","reason":"두 슬롯이 구성되어 요청을 받을 수 있습니다."})
    };
    let attentions = keys
        .iter()
        .filter(|key| !key.enabled || !key.verified || key.cooldown_until.is_some())
        .map(|key| {
            json!({
                "code": if key.cooldown_until.is_some() { "upstream_cooldown" } else if !key.verified { "probe_required" } else { "upstream_disabled" },
                "resource": {"kind":"upstream_key","id":key.id},
                "label": key.label,
                "next_action": if !key.verified { "probe" } else { "inspect_routing" },
                "expires_at": key.cooldown_until,
            })
        })
        .collect::<Vec<_>>();
    let checks = keys
        .iter()
        .map(|key| {
            json!({
                "kind":"upstream_key",
                "id":key.id,
                "label":key.label,
                "status": if key.enabled && key.verified && key.cooldown_until.is_none() { "eligible" } else if key.cooldown_until.is_some() { "cooldown" } else if key.verified { "disabled" } else { "probe_required" },
                "failure_count":key.failure_count,
                "request_count":key.request_count,
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "runtime": {"status": if database_ready && eligible_keys > 0 { "ok" } else { "degraded" }, "ready": database_ready && keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS, "traffic_ready": database_ready && eligible_keys > 0, "eligible_keys": if database_ready { eligible_keys } else { 0 }},
        "upstream_keys": {"items": keys},
        "downstream_credentials": {"items": downstream},
        "models": PROFILES,
        "evidence": evidence,
        "server_recommendation": recommendation,
        "attentions": attentions,
        "checks": checks,
        "public_health": {"hostname":"nvidia-lb.dongwontuna.net","status":"not_verified","next_action":"verify_public_route"},
        "snapshot_observed_at": Utc::now(),
    }))
}

pub(crate) async fn evidence(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state.vault.evidence().await {
        Ok(value) => HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(value),
        Err(_) => HttpResponse::ServiceUnavailable()
            .json(json!({"error":{"code":"evidence_unavailable"}})),
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct PageQuery {
    pub(crate) before: Option<String>,
    pub(crate) limit: Option<u16>,
}

#[derive(Debug, Deserialize, Serialize)]
pub(crate) struct PageCursor {
    pub(crate) created_at: chrono::DateTime<Utc>,
    pub(crate) id: Uuid,
}

pub(crate) fn encode_page_cursor(cursor: &PageCursor) -> Result<String> {
    Ok(URL_SAFE_NO_PAD.encode(serde_json::to_vec(cursor)?))
}

pub(crate) fn page_before(query: &PageQuery) -> Result<Option<PageCursor>, HttpResponse> {
    query.before.as_deref().map(|value| {
        let decoded = URL_SAFE_NO_PAD.decode(value).map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"code": "invalid_page_cursor", "message": "before is not a valid page cursor"}
            }))
        })?;
        serde_json::from_slice(&decoded).map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"code": "invalid_page_cursor", "message": "before is not a valid page cursor"}
            }))
        })
    }).transpose()
}

fn admin_snapshot() -> Value {
    let observed_at = Utc::now();
    json!({"observed_at": observed_at, "generation": observed_at.to_rfc3339()})
}

pub(crate) async fn upstream_slots(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let keys = state.vault.list();
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let slots = keys
        .iter()
        .enumerate()
        .map(|(index, key)| {
            let eligible = key.enabled && key.verified && key.cooldown_until.is_none_or(|until| until <= Utc::now());
            json!({
                "slot_no": index + 1,
                "key_id": key.id,
                "label": key.label,
                "status": if eligible { "eligible" } else if key.cooldown_until.is_some() { "cooldown" } else if !key.verified { "probe_required" } else { "disabled" },
                "request_count": key.request_count,
                "failure_count": key.failure_count,
                "cooldown_until": key.cooldown_until,
                "profiles": PROFILES.iter().map(|profile| {
                    let proof = proofs.get(*profile).is_some_and(|key_ids| key_ids.contains(&key.id));
                    json!({
                        "profile_id": profile,
                        "proof_revision": if proof { "durable" } else { "missing" },
                        "eligible_now": eligible && proof,
                        "reason": if !eligible { if !key.verified { json!("missing_key_probe") } else if key.cooldown_until.is_some() { json!("cooldown") } else { json!("manual_disabled") } } else if !proof { json!("missing_profile_proof") } else { Value::Null }
                    })
                }).collect::<Vec<_>>()
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"slots":slots}))
}

pub(crate) async fn model_capabilities(
    req: HttpRequest,
    state: web::Data<AppState>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let keys = state.vault.list();
    let eligible = keys
        .iter()
        .filter(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        })
        .map(|key| key.id)
        .collect::<HashSet<_>>();
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let models = PROFILES
        .iter()
        .map(|profile| {
            let route = match *profile {
                "z-ai/glm-5.2" | "microsoft/phi-4-multimodal-instruct" | "nvidia/vila" => {
                    "/v1/chat/completions"
                }
                "nvidia/nvclip" => "/v1/embeddings",
                "black-forest-labs/flux.1-kontext-dev" => "/v1/images/generations",
                "stabilityai/stable-video-diffusion" => "/v1/videos/generations",
                "nvidia/magpie-tts-multilingual" => "/v1/audio/speech",
                "nvidia/parakeet-ctc-1.1b" => "/v1/audio/transcriptions",
                _ => "/v1/nvidia/inference",
            };
            let available_now = proofs
                .get(*profile)
                .is_some_and(|key_ids| key_ids.iter().any(|key_id| eligible.contains(key_id)));
            let proof_status = if available_now {
                "provider_proof_verified"
            } else if eligible.is_empty() {
                "pair_not_ready"
            } else {
                "provider_proof_required"
            };
            json!({
                "id":profile,
                "route":route,
                "advertised":true,
                "available_now":available_now,
                "proof_status":proof_status,
                "modalities":model_modalities(profile)
            })
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"models":models}))
}

fn model_modalities(profile: &str) -> &'static [&'static str] {
    match profile {
        "z-ai/glm-5.2" => &["text"],
        "microsoft/phi-4-multimodal-instruct" => &["text", "image", "audio"],
        "nvidia/vila" => &["text", "image", "video"],
        "nvidia/nvclip" => &["image", "embedding"],
        "black-forest-labs/flux.1-kontext-dev" => &["image_generation"],
        "stabilityai/stable-video-diffusion" => &["video_generation"],
        "nvidia/magpie-tts-multilingual" => &["audio_generation"],
        "nvidia/parakeet-ctc-1.1b" => &["audio_transcription"],
        _ => &[],
    }
}

pub(crate) async fn generation_readiness(
    req: HttpRequest,
    state: web::Data<AppState>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let keys = state.vault.list();
    let configured = keys.len();
    let verified = keys.iter().filter(|key| key.verified).count();
    let eligible = eligible_key_count(&keys);
    let proofs = match profile_proof_keys(&state).await {
        Ok(proofs) => proofs,
        Err(_) => {
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"capability_state_unavailable"}}));
        }
    };
    let eligible_ids = keys
        .iter()
        .filter(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        })
        .map(|key| key.id)
        .collect::<HashSet<_>>();
    let available_profiles = PROFILES
        .iter()
        .filter(|profile| {
            proofs
                .get(**profile)
                .is_some_and(|key_ids| key_ids.iter().any(|key_id| eligible_ids.contains(key_id)))
        })
        .count();
    let ready = configured == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && verified == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && eligible == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && available_profiles == PROFILES.len();
    let reasons = if ready {
        Vec::new()
    } else {
        let mut reasons = Vec::new();
        if configured < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("missing_upstream_slot");
        }
        if verified < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("probe_required");
        }
        if eligible < nvidia_build_lb_core::MAX_UPSTREAM_KEYS {
            reasons.push("slot_unavailable");
        }
        if available_profiles < PROFILES.len() {
            reasons.push("provider_proof_required");
        }
        reasons
    };
    HttpResponse::Ok().insert_header(("cache-control", "no-store")).json(json!({
        "snapshot":admin_snapshot(),"ready":ready,"configured_slots":configured,"verified_slots":verified,"eligible_slots":eligible,"advertised_profiles":PROFILES.len(),"available_profiles":available_profiles,"reasons":reasons
    }))
}

pub(crate) async fn operations(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<PageQuery>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let before = match page_before(&query) {
        Ok(before) => before,
        Err(response) => return response,
    };
    let limit = query.limit.unwrap_or(50).clamp(1, 100) as i64;
    let Some(pool) = &state.vault.database else {
        return HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(json!({"snapshot":admin_snapshot(),"operations":[],"next_before":Value::Null}));
    };
    let attempts = match before {
        Some(before) => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>, Option<chrono::DateTime<Utc>>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at, finished_at FROM nblb.request_attempts WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT $3"
        ).bind(before.created_at).bind(before.id).bind(limit).fetch_all(pool).await,
        None => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>, Option<chrono::DateTime<Utc>>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at, finished_at FROM nblb.request_attempts ORDER BY created_at DESC, id DESC LIMIT $1"
        ).bind(limit).fetch_all(pool).await,
    };
    let attempts = match attempts {
        Ok(attempts) => attempts,
        Err(error) => {
            eprintln!("admin operations query failed: {error:#}");
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"operations_unavailable"}}));
        }
    };
    let next_before = if attempts.len() as i64 == limit {
        attempts.last().and_then(|attempt| {
            encode_page_cursor(&PageCursor {
                created_at: attempt.5,
                id: attempt.0,
            })
            .ok()
        })
    } else {
        None
    };
    let rows = attempts
        .into_iter()
        .map(|(id, request_id, profile_id, key_id, outcome, created_at, finished_at)| {
            json!({"id":id,"request_id":request_id,"profile_id":profile_id,"key_id":key_id,"status":outcome,"created_at":created_at,"finished_at":finished_at})
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"operations":rows,"next_before":next_before}))
}

pub(crate) async fn operation_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let id = path.into_inner();
    let Some(pool) = &state.vault.database else {
        return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
    };
    let row = sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, Option<chrono::DateTime<Utc>>)>("SELECT id, request_id, profile_id, key_id, outcome, finished_at FROM nblb.request_attempts WHERE id=$1").bind(id).fetch_optional(pool).await;
    match row {
        Ok(Some((id, request_id, profile_id, key_id, outcome, finished_at))) => HttpResponse::Ok().json(json!({"snapshot":admin_snapshot(),"operation":{"id":id,"request_id":request_id,"profile_id":profile_id,"key_id":key_id,"status":outcome,"finished_at":finished_at}})),
        _ => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

pub(crate) async fn attentions(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let items = state.vault.list().into_iter().filter(|key| !key.enabled || !key.verified || key.cooldown_until.is_some()).map(|key| json!({"id":key.id,"code":if key.cooldown_until.is_some(){"upstream_cooldown"}else if !key.verified{"probe_required"}else{"upstream_disabled"},"resource":{"kind":"upstream_key","id":key.id},"label":key.label,"next_action":if !key.verified{"probe"}else{"inspect_routing"},"expires_at":key.cooldown_until})).collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"attentions":items}))
}

pub(crate) async fn events(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<PageQuery>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let before = match page_before(&query) {
        Ok(before) => before,
        Err(response) => return response,
    };
    let limit = query.limit.unwrap_or(50).clamp(1, 100) as i64;
    let Some(pool) = &state.vault.database else {
        return HttpResponse::Ok()
            .insert_header(("cache-control", "no-store"))
            .json(json!({"snapshot":admin_snapshot(),"events":[],"next_before":Value::Null}));
    };
    let attempts = match before {
        Some(before) => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at FROM nblb.request_attempts WHERE (created_at, id) < ($1, $2) ORDER BY created_at DESC, id DESC LIMIT $3"
        ).bind(before.created_at).bind(before.id).bind(limit).fetch_all(pool).await,
        None => sqlx::query_as::<_, (Uuid, Uuid, String, Uuid, String, chrono::DateTime<Utc>)>(
            "SELECT id, request_id, profile_id, key_id, outcome, created_at FROM nblb.request_attempts ORDER BY created_at DESC, id DESC LIMIT $1"
        ).bind(limit).fetch_all(pool).await,
    };
    let attempts = match attempts {
        Ok(attempts) => attempts,
        Err(error) => {
            eprintln!("admin events query failed: {error:#}");
            return HttpResponse::ServiceUnavailable()
                .json(json!({"error":{"code":"events_unavailable"}}));
        }
    };
    let next_before = if attempts.len() as i64 == limit {
        attempts.last().and_then(|attempt| {
            encode_page_cursor(&PageCursor {
                created_at: attempt.5,
                id: attempt.0,
            })
            .ok()
        })
    } else {
        None
    };
    let rows = attempts
        .into_iter()
        .map(|(id, request_id, profile_id, key_id, outcome, created_at)| {
            json!({"id":id,"kind":"request_attempt","request_id":request_id,"profile_id":profile_id,"key_id":key_id,"outcome":outcome,"created_at":created_at})
        })
        .collect::<Vec<_>>();
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(json!({"snapshot":admin_snapshot(),"events":rows,"next_before":next_before}))
}

pub(crate) async fn event_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    operation_detail(req, state, path).await
}

pub(crate) async fn evidence_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let id = path.into_inner();
    let Some(pool) = &state.vault.database else {
        return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
    };
    let row = sqlx::query_as::<_, (Uuid, Uuid, String, String)>(
        "SELECT id, request_id, profile_id, outcome FROM nblb.request_attempts WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await;
    match row {
        Ok(Some((id, request_id, profile_id, outcome))) => HttpResponse::Ok().json(json!({"snapshot":admin_snapshot(),"evidence":{"id":id,"request_id":request_id,"profile_id":profile_id,"outcome":outcome}})),
        _ => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

pub(crate) async fn add_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    payload: web::Json<KeyInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state
        .vault
        .mutate(|vault| vault.add(&payload.label, &payload.credential))
        .await
    {
        Ok(summary) => HttpResponse::Created().json(summary),
        Err(error) if is_unique_conflict(&error) => HttpResponse::Conflict().json(
            json!({"error":{"code":"resource_conflict","message":"An active upstream slot with this label or credential already exists."}}),
        ),
        Err(error) => HttpResponse::UnprocessableEntity()
            .json(json!({"error":{"code":"invalid_request","message":error.to_string()}})),
    }
}

pub(crate) async fn toggle_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
    payload: web::Json<ToggleInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state
        .vault
        .mutate(|vault| vault.set_enabled(path.into_inner(), payload.enabled))
        .await
    {
        Ok(summary) => HttpResponse::Ok().json(summary),
        Err(error) if error.to_string().contains("probe") => HttpResponse::Conflict().json(
            json!({"error":{"code":"probe_required","message":"provider probe is required before enabling this key"}}),
        ),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

pub(crate) async fn enable_key_alias(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    toggle_key(req, state, path, web::Json(ToggleInput { enabled: true })).await
}

pub(crate) async fn disable_key_alias(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    toggle_key(req, state, path, web::Json(ToggleInput { enabled: false })).await
}

/// Verifies one stored credential without changing its routing state.  The
/// response is deliberately reduced to a status class; provider bodies and
/// credential material never cross the admin boundary.
pub(crate) async fn record_probe_receipt(
    state: &web::Data<AppState>,
    key_id: Uuid,
) -> Result<(), HttpResponse> {
    state
        .vault
        .record_profile_proof("z-ai/glm-5.2", key_id)
        .await
        .map_err(|_| {
            HttpResponse::ServiceUnavailable().json(json!({
                "probe_status": "unavailable",
                "error": {"code": "probe_persistence_failed"}
            }))
        })
}

pub(crate) async fn probe_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let id = path.into_inner();
    let credential = match state.vault.credential_for_probe(id) {
        Ok(value) => value,
        Err(_) => {
            return HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}}));
        }
    };
    if state.upstream_url.starts_with("mock://") {
        if credential.contains("fail") {
            if let Err(response) = quarantine_key(&state, id).await {
                return response;
            }
            return HttpResponse::UnprocessableEntity().json(json!({
                "probe_status": "invalid_credential",
                "error": {"code": "invalid_upstream_credential"}
            }));
        }
        return match state.vault.mutate(|vault| vault.mark_verified(id)).await {
            Ok(_) => match record_probe_receipt(&state, id).await {
                Ok(()) => HttpResponse::Ok().json(json!({"probe_status":"valid","status":200})),
                Err(response) => response,
            },
            Err(_) => HttpResponse::ServiceUnavailable().json(
                json!({"probe_status":"unavailable","error":{"code":"probe_persistence_failed"}}),
            ),
        };
    }
    let endpoint =
        upstream_endpoint_for(&state.upstream_url, "/v1/chat/completions", "z-ai/glm-5.2");
    let response = state
        .client
        .post(endpoint)
        .bearer_auth(&credential)
        .header(reqwest::header::ACCEPT, "application/json")
        .json(&json!({
            "model": "z-ai/glm-5.2",
            "messages": [{"role":"user","content":"health probe"}],
            "max_tokens": 1,
            "stream": false
        }))
        .timeout(UPSTREAM_REQUEST_TIMEOUT)
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(_) => {
            return HttpResponse::ServiceUnavailable().json(json!({
                "probe_status": "unavailable",
                "error": {"code": "upstream_unavailable"}
            }));
        }
    };
    let status = response.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        if let Err(response) = quarantine_key(&state, id).await {
            return response;
        }
        return HttpResponse::UnprocessableEntity().json(json!({
            "probe_status": "invalid_credential",
            "status": status.as_u16(),
            "error": {"code": "invalid_upstream_credential"}
        }));
    }
    if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
        return HttpResponse::TooManyRequests().json(json!({
            "probe_status": "rate_limited",
            "status": status.as_u16(),
            "error": {"code": "upstream_rate_limited"}
        }));
    }
    if !status.is_success() {
        return HttpResponse::ServiceUnavailable().json(json!({
            "probe_status": "unavailable",
            "status": status.as_u16(),
            "error": {"code": "upstream_unavailable"}
        }));
    }
    let bytes = match response.bytes().await {
        Ok(bytes) => bytes,
        Err(_) => {
            return HttpResponse::BadGateway().json(json!({
                "probe_status": "invalid_response",
                "error": {"code": "upstream_protocol_error"}
            }));
        }
    };
    if validate_chat_response(&bytes, false).is_err() {
        return HttpResponse::BadGateway().json(json!({
            "probe_status": "invalid_response",
            "error": {"code": "upstream_protocol_error"}
        }));
    }
    match state.vault.mutate(|vault| vault.mark_verified(id)).await {
        Ok(_) => match record_probe_receipt(&state, id).await {
            Ok(()) => {
                HttpResponse::Ok().json(json!({"probe_status":"valid","status":status.as_u16()}))
            }
            Err(response) => response,
        },
        Err(_) => HttpResponse::ServiceUnavailable().json(
            json!({"probe_status":"unavailable","error":{"code":"probe_persistence_failed"}}),
        ),
    }
}

pub(crate) async fn delete_key(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state
        .vault
        // Request attempts retain a foreign key to the encrypted key row.
        // “Delete” is therefore a durable retirement, which preserves audit
        // evidence while removing the encrypted credential from all new
        // routing and admin read surfaces.
        .mutate(|vault| vault.delete(path.into_inner()))
        .await
    {
        Ok(()) => HttpResponse::NoContent().finish(),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

pub(crate) async fn list_downstream(
    req: HttpRequest,
    state: web::Data<AppState>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let items = state.vault.list_downstream();
    HttpResponse::Ok().json(json!({"items": items}))
}

pub(crate) async fn add_downstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    payload: web::Json<DownstreamInput>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    let keys = state.vault.list();
    let ready = keys.len() == nvidia_build_lb_core::MAX_UPSTREAM_KEYS
        && keys.iter().all(|key| {
            key.enabled
                && key.verified
                && key.cooldown_until.is_none_or(|until| until <= Utc::now())
        });
    if !ready {
        return HttpResponse::Conflict().json(json!({
            "error": {
                "code": "generation_not_ready",
                "message": "Both NVIDIA upstream slots must be probed and available before issuing a downstream credential.",
                "next_action": "probe_and_enable_all_upstream_slots"
            }
        }));
    }
    match state
        .vault
        .mutate(|vault| vault.issue_downstream(&payload.label, &payload.scopes))
        .await
    {
        Ok(issued) => {
            let mut value = serde_json::to_value(issued.summary).unwrap_or_else(|_| json!({}));
            if let Value::Object(object) = &mut value {
                object.insert("token".into(), Value::String(issued.token));
            }
            HttpResponse::Created().json(value)
        }
        Err(error)
            if error.to_string().contains("already exists") || is_unique_conflict(&error) =>
        {
            HttpResponse::Conflict()
                .json(json!({"error":{"code":"resource_conflict","message":error.to_string()}}))
        }
        Err(error) => HttpResponse::UnprocessableEntity()
            .json(json!({"error":{"code":"invalid_request","message":error.to_string()}})),
    }
}

fn is_unique_conflict(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        let text = cause.to_string();
        text.contains("duplicate key value violates unique constraint")
            || text.contains("upstream_keys_active_")
            || text.contains("downstream_credentials_active_label_idx")
            || cause
                .downcast_ref::<sqlx::Error>()
                .and_then(|error| error.as_database_error())
                .and_then(|database| database.code())
                .as_deref()
                == Some("23505")
    })
}

pub(crate) async fn revoke_downstream(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state
        .vault
        .mutate(|vault| vault.revoke_downstream(path.into_inner()))
        .await
    {
        Ok(summary) => HttpResponse::Ok().json(summary),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}

pub(crate) async fn revoke_downstream_legacy(
    req: HttpRequest,
    state: web::Data<AppState>,
    path: web::Path<Uuid>,
) -> impl Responder {
    if !authorized(&req, &state) {
        return admin_unauthorized(&req);
    }
    match state
        .vault
        .mutate(|vault| vault.revoke_downstream(path.into_inner()))
        .await
    {
        Ok(_) => HttpResponse::NoContent().finish(),
        Err(_) => HttpResponse::NotFound().json(json!({"error":{"code":"resource_not_found"}})),
    }
}
