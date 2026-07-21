//! Server-owned request correlation for dynamic API responses.

use actix_web::body::{EitherBody, MessageBody};
use actix_web::dev::{ServiceRequest, ServiceResponse};
use actix_web::http::header::{HeaderName, HeaderValue};
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
