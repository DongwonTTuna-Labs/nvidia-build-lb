//! Server-owned request correlation for dynamic API responses.

use actix_web::body::{EitherBody, MessageBody};
use actix_web::dev::{ServiceRequest, ServiceResponse};
use actix_web::http::{
    Method,
    header::{HeaderName, HeaderValue},
};
use actix_web::middleware::Next;
use actix_web::{Error, HttpMessage, HttpRequest, HttpResponse, web};
use uuid::Uuid;

const X_REQUEST_ID: HeaderName = HeaderName::from_static("x-request-id");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct RequestId(Uuid);

impl RequestId {
    pub(crate) fn new() -> Self {
        Self(Uuid::new_v4())
    }

    pub(crate) fn value(self) -> Uuid {
        self.0
    }
}

pub(crate) fn request_id(req: &HttpRequest) -> Uuid {
    req.extensions()
        .get::<RequestId>()
        .copied()
        .unwrap_or_else(RequestId::new)
        .value()
}

pub(crate) fn is_dynamic_api_path(path: &str) -> bool {
    matches!(path, "/health" | "/health/live" | "/health/ready")
        || path.starts_with("/v1/")
        || path.starts_with("/api/public/")
        || path.starts_with("/admin/api/")
}

pub(crate) async fn assign_request_id(
    req: ServiceRequest,
    next: Next<impl MessageBody>,
) -> Result<ServiceResponse<impl MessageBody>, Error> {
    if !is_dynamic_api_path(req.path()) {
        return next.call(req).await;
    }
    let request_id = RequestId::new();
    req.extensions_mut().insert(request_id);
    let mut response = next.call(req).await?;
    response.headers_mut().insert(
        X_REQUEST_ID,
        HeaderValue::from_str(&request_id.value().to_string())
            .expect("UUID is a valid HTTP header value"),
    );
    Ok(response)
}

pub(crate) async fn enforce_admin_boundary(
    req: ServiceRequest,
    next: Next<impl MessageBody>,
) -> Result<ServiceResponse<EitherBody<impl MessageBody>>, Error> {
    let admin_path = req.path() == "/admin" || req.path().starts_with("/admin/");
    let allowed = req
        .app_data::<web::Data<super::AppState>>()
        .is_some_and(|state| super::admin_surface_allowed(req.request(), state));
    if admin_path && !allowed {
        return Ok(req
            .into_response(HttpResponse::NotFound().finish())
            .map_into_right_body());
    }
    Ok(next.call(req).await?.map_into_left_body())
}

fn failed_mutation_target(path: &str, method: &Method) -> Option<(String, String, Option<Uuid>)> {
    if !matches!(*method, Method::POST | Method::PATCH | Method::DELETE) {
        return None;
    }
    let segments = path
        .strip_prefix("/admin/api/v2/")?
        .split('/')
        .filter(|segment| !segment.is_empty())
        .collect::<Vec<_>>();
    let first = *segments.first()?;
    if first == "routing" && segments.get(1) == Some(&"simulate") {
        return None;
    }
    let resource_kind = match first {
        "upstreams" => "upstream",
        "clients" => "client",
        "routing" => "routing_policy",
        "models" => "model_catalog",
        "incidents" => "incident",
        "qa" => "qa_run",
        "settings" => "settings",
        _ => return None,
    };
    let resource_id = segments
        .iter()
        .find_map(|segment| Uuid::parse_str(segment).ok());
    let operation = match (method.clone(), segments.as_slice()) {
        (Method::POST, ["qa", "runs"]) => "create",
        (Method::POST, [_, _id, "updates"]) => "message.create",
        (Method::POST, [_, _id, suffix]) => *suffix,
        (Method::POST, [_, suffix]) if *suffix != "policy" => *suffix,
        (Method::POST, [_]) => "create",
        (Method::PATCH, _) => "update",
        (Method::DELETE, _) => "delete",
        _ => return None,
    };
    Some((
        format!("{resource_kind}.{operation}"),
        resource_kind.to_owned(),
        resource_id,
    ))
}

/// Adds a normalized best-effort audit row for every rejected or failed v2
/// mutation. The request body is deliberately unavailable here, so secrets,
/// prompts, and media cannot leak into failure evidence.
pub(crate) async fn audit_failed_admin_mutation(
    req: ServiceRequest,
    next: Next<impl MessageBody>,
) -> Result<ServiceResponse<impl MessageBody>, Error> {
    let target = failed_mutation_target(req.path(), req.method());
    let method = req.method().as_str().to_owned();
    let correlation_id = req
        .extensions()
        .get::<RequestId>()
        .copied()
        .map(RequestId::value);
    let pool = req
        .app_data::<web::Data<super::AppState>>()
        .and_then(|state| state.vault.database.clone());
    let response = next.call(req).await?;
    if !response.status().is_success()
        && let (Some((action, resource_kind, resource_id)), Some(pool)) = (target, pool)
    {
        let _ = sqlx::query(
            "INSERT INTO nblb.audit_events(action,resource_kind,resource_id,request_id,outcome,detail) VALUES ($1,$2,$3,$4,'failed',$5)",
        )
        .bind(action)
        .bind(resource_kind)
        .bind(resource_id)
        .bind(correlation_id)
        .bind(serde_json::json!({
            "http_status": response.status().as_u16(),
            "method": method,
            "best_effort": true
        }))
        .execute(&pool)
        .await;
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::failed_mutation_target;
    use actix_web::http::Method;

    #[test]
    fn failed_mutation_audit_normalizes_dynamic_paths() {
        let id = "3b0d43cb-c9d6-4aa2-a56d-5037bab6d0ca";
        assert_eq!(
            failed_mutation_target(
                &format!("/admin/api/v2/upstreams/{id}/probe-profiles"),
                &Method::POST,
            )
            .map(|value| (value.0, value.1, value.2.is_some())),
            Some(("upstream.probe-profiles".into(), "upstream".into(), true))
        );
        assert!(failed_mutation_target("/admin/api/v2/routing/simulate", &Method::POST).is_none());
    }
}
