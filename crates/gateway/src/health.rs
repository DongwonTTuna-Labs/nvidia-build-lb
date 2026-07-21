//! Process liveness and traffic-readiness HTTP contracts.

use actix_web::{HttpRequest, HttpResponse, Responder, web};
use chrono::Utc;
use nvidia_build_lb_core::{KeySummary, MAX_UPSTREAM_KEYS};
use serde::Serialize;
use std::time::Duration;

use super::{AppState, eligible_key_count, public_guard_response};

const DATABASE_PROBE_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Debug, Serialize)]
pub(crate) struct Liveness {
    status: &'static str,
    live: bool,
    version: &'static str,
    observed_at: chrono::DateTime<Utc>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub(crate) struct Readiness {
    pub(crate) status: &'static str,
    pub(crate) ready: bool,
    pub(crate) database_ready: bool,
    pub(crate) traffic_ready: bool,
    pub(crate) pair_ready: bool,
    pub(crate) eligible_keys: usize,
    pub(crate) reason_codes: Vec<&'static str>,
}

impl Readiness {
    pub(crate) fn from_state(database_ready: bool, keys: &[KeySummary]) -> Self {
        let eligible_keys = if database_ready {
            eligible_key_count(keys)
        } else {
            0
        };
        let traffic_ready = database_ready && eligible_keys > 0;
        let pair_ready = database_ready && eligible_keys == MAX_UPSTREAM_KEYS;
        let mut reason_codes = Vec::with_capacity(2);
        if !database_ready {
            reason_codes.push("database_unavailable");
        } else if eligible_keys == 0 {
            reason_codes.push("no_eligible_upstream");
        }
        if database_ready && !pair_ready {
            reason_codes.push("pair_not_ready");
        }
        Self {
            status: if traffic_ready { "ok" } else { "degraded" },
            ready: traffic_ready,
            database_ready,
            traffic_ready,
            pair_ready,
            eligible_keys,
            reason_codes,
        }
    }

    fn response(self) -> HttpResponse {
        let mut response = if self.traffic_ready {
            HttpResponse::Ok()
        } else {
            HttpResponse::ServiceUnavailable()
        };
        response
            .insert_header(("cache-control", "no-store"))
            .json(self)
    }
}

pub(crate) async fn liveness(req: HttpRequest) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    HttpResponse::Ok()
        .insert_header(("cache-control", "no-store"))
        .json(Liveness {
            status: "ok",
            live: true,
            version: env!("CARGO_PKG_VERSION"),
            observed_at: Utc::now(),
        })
}

pub(crate) async fn readiness(req: HttpRequest, state: web::Data<AppState>) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let database_ready = match &state.vault.database {
        Some(pool) => tokio::time::timeout(
            DATABASE_PROBE_TIMEOUT,
            sqlx::query_scalar::<_, i32>("SELECT 1").fetch_one(pool),
        )
        .await
        .is_ok_and(|result| result.is_ok()),
        None => true,
    };
    Readiness::from_state(database_ready, &state.vault.list()).response()
}
