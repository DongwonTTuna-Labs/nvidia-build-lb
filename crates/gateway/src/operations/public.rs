//! Sanitized public status handlers. These handlers never read identity DTOs.

use actix_web::{HttpRequest, HttpResponse, http::StatusCode, web};
use serde::Deserialize;
use std::collections::BTreeMap;

use super::{
    dto::{
        PublicCapacity, PublicEndpoint, PublicIncidentDetail, PublicIncidents, PublicMetrics,
        PublicModels, PublicState, PublicSummary, Snapshot,
    },
    repository,
};
use crate::{AppState, operations_error, public_guard_response};

pub(crate) fn routes(cfg: &mut web::ServiceConfig) {
    cfg.route("/api/public/v1/summary", web::get().to(summary))
        .route(
            "/api/public/v1/openapi.json",
            web::get().to(openapi_document),
        )
        .route("/api/public/v1/metrics", web::get().to(metrics))
        .route("/api/public/v1/models", web::get().to(models))
        .route("/api/public/v1/incidents", web::get().to(incidents))
        .route(
            "/api/public/v1/incidents/{slug}",
            web::get().to(incident_detail),
        );
}

async fn openapi_document(req: HttpRequest) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    HttpResponse::Ok().json(super::openapi::public_document())
}

fn public_error(req: &HttpRequest, status: StatusCode, code: &str, message: &str) -> HttpResponse {
    operations_error(req, status, code, message, status.is_server_error(), None)
}

fn pool_or_error<'a>(
    req: &HttpRequest,
    state: &'a AppState,
) -> Result<&'a sqlx::PgPool, HttpResponse> {
    state.vault.database.as_ref().ok_or_else(|| {
        public_error(
            req,
            StatusCode::SERVICE_UNAVAILABLE,
            "database_unavailable",
            "Public status data is temporarily unavailable.",
        )
    })
}

pub(crate) async fn summary(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let pool = match pool_or_error(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    let database_ready = sqlx::query_scalar::<_, i32>("SELECT 1")
        .fetch_one(pool)
        .await
        .is_ok();
    if !database_ready {
        return public_error(
            &req,
            StatusCode::SERVICE_UNAVAILABLE,
            "database_unavailable",
            "Public status data is temporarily unavailable.",
        );
    }
    let eligible_ids = match repository::eligible_key_ids(pool, "z-ai/glm-5.2").await {
        Ok(ids) => ids,
        Err(_) => {
            return public_error(
                &req,
                StatusCode::SERVICE_UNAVAILABLE,
                "capacity_unavailable",
                "Provider capacity is temporarily unavailable.",
            );
        }
    };
    let eligible = eligible_ids.len().min(2);
    let metrics_24h = match repository::metric_summary(pool, "24 hours").await {
        Ok(metric) => metric,
        Err(_) => {
            return public_error(
                &req,
                StatusCode::SERVICE_UNAVAILABLE,
                "metrics_unavailable",
                "Public metrics are temporarily unavailable.",
            );
        }
    };
    let models = match repository::public_models(pool).await {
        Ok(items) => items,
        Err(_) => {
            return public_error(
                &req,
                StatusCode::SERVICE_UNAVAILABLE,
                "models_unavailable",
                "Model status is temporarily unavailable.",
            );
        }
    };
    let mut endpoint_available: BTreeMap<&str, bool> = BTreeMap::new();
    let mut endpoint_proven: BTreeMap<&str, bool> = BTreeMap::new();
    for model in &models {
        endpoint_available
            .entry(model.endpoint)
            .and_modify(|available| *available |= model.available_now)
            .or_insert(model.available_now);
        endpoint_proven
            .entry(model.endpoint)
            .and_modify(|proven| *proven |= model.proof_status != "proof_required")
            .or_insert(model.proof_status != "proof_required");
    }
    let endpoints = [
        ("chat", "/v1/chat/completions"),
        ("embeddings", "/v1/embeddings"),
        ("images", "/v1/images/generations"),
        ("video", "/v1/videos/generations"),
        ("speech", "/v1/audio/speech"),
        ("transcription", "/v1/audio/transcriptions"),
    ]
    .into_iter()
    .map(|(kind, endpoint)| PublicEndpoint {
        kind,
        state: if endpoint_available.get(endpoint).copied().unwrap_or(false) {
            "verified"
        } else if eligible == 0 || endpoint_proven.get(endpoint).copied().unwrap_or(false) {
            "unavailable"
        } else {
            "proof_required"
        },
    })
    .collect();
    let (status, reason_code) = match eligible {
        2 => ("operational", None),
        1 => ("degraded", Some("pair_not_ready")),
        _ => ("degraded", Some("no_eligible_upstream")),
    };
    HttpResponse::Ok().json(PublicSummary {
        schema_version: "public.v1",
        snapshot: Snapshot::current(),
        state: PublicState {
            status,
            traffic_ready: eligible > 0,
            reason_code,
        },
        capacity: PublicCapacity {
            eligible: u8::try_from(eligible).unwrap_or_default(),
            target: 2,
        },
        metrics_24h,
        endpoints,
    })
}

#[derive(Debug, Deserialize)]
pub(crate) struct MetricsQuery {
    window: Option<String>,
    step: Option<String>,
}

pub(crate) async fn metrics(
    req: HttpRequest,
    state: web::Data<AppState>,
    query: web::Query<MetricsQuery>,
) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let (window_label, window) = match query.window.as_deref().unwrap_or("24h") {
        "1h" => ("1h", "1 hour"),
        "24h" => ("24h", "24 hours"),
        "7d" => ("7d", "7 days"),
        _ => {
            return public_error(
                &req,
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_window",
                "window must be one of 1h, 24h, or 7d.",
            );
        }
    };
    let (step_label, step) = match query.step.as_deref().unwrap_or("5m") {
        "1m" => ("1m", "1 minute"),
        "5m" => ("5m", "5 minutes"),
        "1h" => ("1h", "1 hour"),
        _ => {
            return public_error(
                &req,
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_step",
                "step must be one of 1m, 5m, or 1h.",
            );
        }
    };
    let pool = match pool_or_error(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::metric_points(pool, window, step).await {
        Ok(points) => HttpResponse::Ok().json(PublicMetrics {
            schema_version: "public.v1",
            snapshot: Snapshot::current(),
            window: window_label.into(),
            step: step_label.into(),
            points,
        }),
        Err(_) => public_error(
            &req,
            StatusCode::SERVICE_UNAVAILABLE,
            "metrics_unavailable",
            "Public metrics are temporarily unavailable.",
        ),
    }
}

pub(crate) async fn models(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let pool = match pool_or_error(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    match repository::public_models(pool).await {
        Ok(items) => HttpResponse::Ok().json(PublicModels {
            schema_version: "public.v1",
            snapshot: Snapshot::current(),
            items,
        }),
        Err(_) => public_error(
            &req,
            StatusCode::SERVICE_UNAVAILABLE,
            "models_unavailable",
            "Model status is temporarily unavailable.",
        ),
    }
}

pub(crate) async fn incidents(req: HttpRequest, state: web::Data<AppState>) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let pool = match pool_or_error(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if !public_incidents_enabled(pool).await {
        return HttpResponse::Ok().json(PublicIncidents {
            schema_version: "public.v1",
            snapshot: Snapshot::current(),
            items: Vec::new(),
        });
    }
    match repository::public_incidents(pool, None).await {
        Ok(items) => HttpResponse::Ok().json(PublicIncidents {
            schema_version: "public.v1",
            snapshot: Snapshot::current(),
            items,
        }),
        Err(_) => public_error(
            &req,
            StatusCode::SERVICE_UNAVAILABLE,
            "incidents_unavailable",
            "Incident history is temporarily unavailable.",
        ),
    }
}

pub(crate) async fn incident_detail(
    req: HttpRequest,
    state: web::Data<AppState>,
    slug: web::Path<String>,
) -> HttpResponse {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let slug = slug.into_inner();
    if slug.len() > 120
        || slug.is_empty()
        || slug
            .bytes()
            .any(|byte| !byte.is_ascii_lowercase() && !byte.is_ascii_digit() && byte != b'-')
    {
        return public_error(
            &req,
            StatusCode::NOT_FOUND,
            "incident_not_found",
            "Incident was not found.",
        );
    }
    let pool = match pool_or_error(&req, &state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if !public_incidents_enabled(pool).await {
        return public_error(
            &req,
            StatusCode::NOT_FOUND,
            "incident_not_found",
            "Incident was not found.",
        );
    }
    match repository::public_incidents(pool, Some(&slug)).await {
        Ok(mut items) if items.len() == 1 => HttpResponse::Ok().json(PublicIncidentDetail {
            schema_version: "public.v1",
            snapshot: Snapshot::current(),
            item: items.remove(0),
        }),
        Ok(_) => public_error(
            &req,
            StatusCode::NOT_FOUND,
            "incident_not_found",
            "Incident was not found.",
        ),
        Err(_) => public_error(
            &req,
            StatusCode::SERVICE_UNAVAILABLE,
            "incidents_unavailable",
            "Incident history is temporarily unavailable.",
        ),
    }
}

async fn public_incidents_enabled(pool: &sqlx::PgPool) -> bool {
    sqlx::query_scalar::<_, bool>(
        "SELECT public_incidents_enabled FROM nblb.operations_settings WHERE singleton=true",
    )
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
    .unwrap_or(true)
}

#[cfg(test)]
mod tests {
    use crate::operations::dto::MODEL_SPECS;

    #[test]
    fn public_model_catalog_has_unique_routes_and_no_identifiers() {
        assert_eq!(MODEL_SPECS.len(), 8);
        assert!(
            MODEL_SPECS
                .iter()
                .all(|model| model.endpoint.starts_with("/v1/"))
        );
        assert!(MODEL_SPECS.iter().all(|model| !model.id.contains("nvapi-")));
    }
}
