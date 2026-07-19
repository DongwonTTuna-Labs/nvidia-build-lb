use actix_web::HttpResponse;
use serde_json::json;

pub(crate) fn invalid_request(message: &str) -> HttpResponse {
    HttpResponse::BadRequest().json(json!({
        "error": {"message": message, "type": "invalid_request"}
    }))
}
