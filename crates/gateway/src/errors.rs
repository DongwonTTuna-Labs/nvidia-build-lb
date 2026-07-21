//! Stable error envelopes for OpenAI-compatible and operations surfaces.

use actix_web::{HttpRequest, HttpResponse, http::StatusCode};
use serde::Serialize;
use serde_json::Value;

use crate::request_id::request_id;

#[derive(Debug, Serialize)]
struct OpenAiErrorEnvelope<'a> {
    error: OpenAiError<'a>,
}

#[derive(Debug, Serialize)]
struct OpenAiError<'a> {
    message: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    param: Option<&'a str>,
    code: &'a str,
}

#[derive(Debug, Serialize)]
struct OperationsErrorEnvelope<'a> {
    error: OperationsError<'a>,
}

#[derive(Debug, Serialize)]
struct OperationsError<'a> {
    code: &'a str,
    message: &'a str,
    request_id: uuid::Uuid,
    retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<Value>,
}

pub(crate) fn openai_error(
    status: StatusCode,
    message: &str,
    kind: &str,
    code: &str,
) -> HttpResponse {
    HttpResponse::build(status).json(OpenAiErrorEnvelope {
        error: OpenAiError {
            message,
            kind,
            param: None,
            code,
        },
    })
}

pub(crate) fn invalid_request(message: &str) -> HttpResponse {
    openai_error(
        StatusCode::BAD_REQUEST,
        message,
        "invalid_request_error",
        "invalid_request",
    )
}

pub(crate) fn operations_error(
    req: &HttpRequest,
    status: StatusCode,
    code: &str,
    message: &str,
    retryable: bool,
    details: Option<Value>,
) -> HttpResponse {
    HttpResponse::build(status).json(OperationsErrorEnvelope {
        error: OperationsError {
            code,
            message,
            request_id: request_id(req),
            retryable,
            details,
        },
    })
}
