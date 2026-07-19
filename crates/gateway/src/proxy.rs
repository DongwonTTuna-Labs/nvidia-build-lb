use actix_web::{HttpRequest, HttpResponse, Responder, http::header, web};
use bytes::{Bytes, BytesMut};
use chrono::{Duration, Utc};
use futures_util::StreamExt;
use serde_json::{Value, json};
use uuid::Uuid;

use super::{
    AppState, PROFILES, STREAM_PRIME_TIMEOUT, StreamAttemptGuard, UPSTREAM_REQUEST_TIMEOUT,
    attempt_finished, attempt_started, authorize_scope, chat_response_stream, invalid_request,
    mock_modality, mock_response, mock_stream_response, normalize_modality_response, poll_nvcf,
    prepare_modality_request, prime_stream, public_guard_response, quarantine_key, record_failure,
    record_request, select_key, upstream_endpoint_for, valid_data_url, validate_chat_response,
};

pub(crate) async fn chat_completions(
    req: HttpRequest,
    state: web::Data<AppState>,
    mut payload: web::Payload,
) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    if let Err(response) = authorize_scope(&req, &state, "chat:write").await {
        return response;
    }
    let body = match read_request_body(&mut payload, 64 * 1024 * 1024).await {
        Ok(body) => body,
        Err(response) => return response,
    };
    let request: Value = match serde_json::from_slice(&body) {
        Ok(request) => request,
        Err(_) => return invalid_request("request body must be valid JSON"),
    };
    if let Err(response) = validate_chat_request(&request) {
        return response;
    }
    let request_id = Uuid::new_v4();
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let stream = request
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let key_attempts = state.vault.list().len().max(1);
    let mut attempted = Vec::new();
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let id = select_key(&state, profile).await;
        let Some(id) = id else { break };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if let Err(response) = attempt_started(&state, request_id, profile, id).await {
            return response;
        }
        let credential = match state.vault.credential(id) {
            Ok(value) => value,
            Err(_) => {
                all_attempts_rate_limited = false;
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            all_attempts_rate_limited = false;
            let force_first_failure = request
                .get("metadata")
                .and_then(Value::as_object)
                .and_then(|metadata| metadata.get("force_first_upstream_failure"))
                .and_then(Value::as_bool)
                .unwrap_or(false)
                && attempted.len() == 1;
            if credential.contains("fail") || force_first_failure {
                if let Err(response) = record_failure(&state, id, Some(Duration::seconds(2))).await
                {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
            if stream {
                return mock_stream_response(&request, state.clone(), request_id, id);
            }
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                return response;
            }
            return mock_response(&request, stream);
        }
        let mut upstream_request = state
            .client
            .post(upstream_endpoint_for(
                &state.upstream_url,
                "/v1/chat/completions",
                profile,
            ))
            .bearer_auth(&credential)
            .json(&request);
        if !stream {
            upstream_request = upstream_request.timeout(UPSTREAM_REQUEST_TIMEOUT);
        }
        let result = upstream_request.send().await;
        match result.as_ref() {
            Ok(response) if response.status().as_u16() == 429 => rate_limited = true,
            _ => all_attempts_rate_limited = false,
        }
        match result {
            Ok(response)
                if response.status().as_u16() == 202
                    && !stream
                    && matches!(
                        profile,
                        "microsoft/phi-4-multimodal-instruct" | "nvidia/vila"
                    ) =>
            {
                let chat_endpoint =
                    upstream_endpoint_for(&state.upstream_url, "/v1/chat/completions", profile);
                let response = match poll_nvcf(&state.client, response, &chat_endpoint, &credential)
                    .await
                {
                    Ok(response) => response,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "NVIDIA accepted the request but polling did not complete", "type": "upstream_poll_error"}
                            }));
                    }
                };
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                if validate_chat_response(&bytes, false).is_err() {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "provider returned an invalid chat response", "type": "upstream_protocol_error"}
                    }));
                }
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", "application/json"))
                    .body(bytes);
            }
            Ok(response) if response.status().as_u16() == 202 => {
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::BadGateway().json(json!({"error":{"message":"NVIDIA returned an unsupported asynchronous response","type":"upstream_protocol_error"}}));
            }
            Ok(response) if response.status().is_success() => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let content_type = response
                    .headers()
                    .get("content-type")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("application/json")
                    .to_owned();
                if stream {
                    if !content_type
                        .split(';')
                        .next()
                        .is_some_and(|value| value.trim().eq_ignore_ascii_case("text/event-stream"))
                    {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                    let upstream = Box::pin(response.bytes_stream());
                    // Own the started attempt before awaiting the first SSE
                    // frame. If the client disconnects while the provider is
                    // silent, dropping this guard still closes the ledger row.
                    let stream_guard = StreamAttemptGuard::new(state.clone(), request_id, id);
                    let (upstream, validator, prefix) =
                        match tokio::time::timeout(STREAM_PRIME_TIMEOUT, prime_stream(upstream))
                            .await
                        {
                            Ok(Ok(value)) => value,
                            _ => {
                                stream_guard.mark_terminal();
                                if let Err(response) = record_failure(&state, id, None).await {
                                    return response;
                                }
                                if let Err(response) =
                                    attempt_finished(&state, request_id, id, "failed").await
                                {
                                    return response;
                                }
                                continue;
                            }
                        };
                    let downstream = chat_response_stream(
                        upstream,
                        validator,
                        prefix,
                        state.clone(),
                        request_id,
                        id,
                        stream_guard,
                    );
                    return HttpResponse::build(status)
                        .insert_header(("content-type", content_type))
                        .insert_header(("cache-control", "no-cache"))
                        .streaming(downstream);
                }
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                if validate_chat_response(&bytes, stream).is_err() {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    if let Err(response) = attempt_finished(&state, request_id, id, "failed").await
                    {
                        return response;
                    }
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "provider returned an invalid chat response", "type": "upstream_protocol_error"}
                    }));
                }
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response)
                if response.status().as_u16() == 408
                    || response.status().as_u16() == 401
                    || response.status().as_u16() == 402
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                // 402 means quota/credit exhaustion, not invalid custody. It
                // therefore receives the same bounded cooldown/failover path
                // as 429 instead of permanently quarantining the credential.
                let auth_failure = matches!(response.status().as_u16(), 401 | 403);
                if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                } else {
                    let retry = retry_after_duration(&response);
                    if let Err(response) = record_failure(&state, id, retry).await {
                        return response;
                    }
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
            Ok(response) => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::build(status).json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => {
                if let Err(response) = record_failure(&state, id, None).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        return HttpResponse::TooManyRequests()
            .insert_header(("retry-after", seconds.to_string()))
            .json(json!({"error":{"message":"All NVIDIA upstream keys are rate limited","type":"upstream_rate_limited"}}));
    }
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
}

/// Handles the non-chat OpenAI/NVIDIA representations through the same
/// authenticated router. The mock path deliberately returns shape-valid
/// fixtures for every advertised modality, while production forwards the
/// original JSON body to the configured NVIDIA endpoint.
pub(crate) async fn multimodal(
    req: HttpRequest,
    state: web::Data<AppState>,
    mut payload: web::Payload,
) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let scope = match req.path() {
        "/v1/embeddings" => "embeddings:write",
        "/v1/images/generations" => "images:write",
        "/v1/audio/speech" | "/v1/audio/transcriptions" => "audio:write",
        _ => "media:write",
    };
    if let Err(response) = authorize_scope(&req, &state, scope).await {
        return response;
    }
    let body = match read_request_body(&mut payload, 64 * 1024 * 1024).await {
        Ok(body) => body,
        Err(response) => return response,
    };
    let content_type = req
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("application/json");
    if body.len() > modality_body_limit(req.path()) {
        return HttpResponse::PayloadTooLarge().json(json!({
            "error": {"message": "request body exceeds the endpoint limit", "type": "request_too_large"}
        }));
    }
    let request = match parse_multimodal_request(req.path(), &body, content_type) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let upstream_request = match prepare_modality_request(req.path(), &request) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let request_id = Uuid::new_v4();
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let key_attempts = state.vault.list().len().max(1);
    let mut attempted = Vec::new();
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let Some(id) = select_key(&state, profile).await else {
            break;
        };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if let Err(response) = attempt_started(&state, request_id, profile, id).await {
            return response;
        }
        let credential = match state.vault.credential(id).ok() {
            Some(value) => value,
            None => {
                all_attempts_rate_limited = false;
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                return response;
            }
            let (body, content_type) = mock_modality(req.path(), &request);
            let (body, content_type) = match normalize_modality_response(
                req.path(),
                &body,
                content_type,
            ) {
                Ok(value) => value,
                Err(_) => {
                    return HttpResponse::BadGateway().json(json!({
                        "error": {"message": "mock provider fixture failed the modality contract", "type": "mock_contract_invalid"}
                    }));
                }
            };
            return HttpResponse::Ok()
                .insert_header(("content-type", content_type))
                .body(body);
        }
        let endpoint = upstream_endpoint_for(&state.upstream_url, req.path(), profile);
        let multipart_transcription = req.path() == "/v1/audio/transcriptions"
            && content_type.starts_with("multipart/form-data")
            && request.get("__nblb_multipart").and_then(Value::as_bool) == Some(true);
        let request_result = if multipart_transcription {
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .header(reqwest::header::CONTENT_TYPE, content_type)
                .body(body.clone())
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        } else if req.path() == "/v1/audio/speech" {
            let input = request
                .get("input")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let voice = request
                .get("voice")
                .and_then(Value::as_str)
                .unwrap_or("English-US.Female-1");
            let language = if voice.to_ascii_lowercase().starts_with("ko") {
                "ko-KR"
            } else {
                "en-US"
            };
            let form = reqwest::multipart::Form::new()
                .text("text", input.to_owned())
                .text("language", language.to_owned())
                .text("voice", voice.to_owned())
                .text("encoding", "LINEAR_PCM".to_owned())
                .text("sample_rate_hz", "44100".to_owned());
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .multipart(form)
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        } else {
            state
                .client
                .post(&endpoint)
                .bearer_auth(&credential)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .json(&upstream_request)
                .timeout(UPSTREAM_REQUEST_TIMEOUT)
                .send()
                .await
        };
        match request_result.as_ref() {
            Ok(response) if response.status().as_u16() == 429 => rate_limited = true,
            _ => all_attempts_rate_limited = false,
        }
        match request_result {
            Ok(response) if response.status().is_success() => {
                // A 202 means NVIDIA accepted an asynchronous job. It is
                // already an upstream side effect, so poll the same request
                // id and never retry the POST on another key.
                let response = if response.status().as_u16() == 202 {
                    match poll_nvcf(&state.client, response, &endpoint, &credential).await {
                        Ok(response) => response,
                        Err(_) => {
                            if let Err(response) = record_failure(&state, id, None).await {
                                return response;
                            }
                            if let Err(response) =
                                attempt_finished(&state, request_id, id, "failed").await
                            {
                                return response;
                            }
                            return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "NVIDIA accepted the media request but polling did not complete", "type": "upstream_poll_error"}
                            }));
                        }
                    }
                } else {
                    response
                };
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let content_type = response
                    .headers()
                    .get("content-type")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or("application/json")
                    .to_owned();
                let bytes = match response.bytes().await {
                    Ok(bytes) => bytes,
                    Err(_) => {
                        if let Err(response) = record_failure(&state, id, None).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        continue;
                    }
                };
                let (bytes, content_type) = match normalize_modality_response(
                    req.path(),
                    &bytes,
                    &content_type,
                ) {
                    Ok(value) => value,
                    Err(_) => {
                        if let Err(response) = quarantine_key(&state, id).await {
                            return response;
                        }
                        if let Err(response) =
                            attempt_finished(&state, request_id, id, "failed").await
                        {
                            return response;
                        }
                        return HttpResponse::BadGateway().json(json!({
                                "error": {"message": "provider returned an invalid modality response", "type": "upstream_protocol_error"}
                            }));
                    }
                };
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "succeeded").await {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response)
                if response.status().as_u16() == 408
                    || response.status().as_u16() == 401
                    || response.status().as_u16() == 402
                    || response.status().as_u16() == 403
                    || response.status().as_u16() == 429
                    || response.status().is_server_error() =>
            {
                // 402 is a provider entitlement/credit signal, so cooldown
                // and failover remain possible; only 401/403 quarantine.
                let auth_failure = matches!(response.status().as_u16(), 401 | 403);
                if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                } else {
                    let retry = retry_after_duration(&response);
                    if let Err(response) = record_failure(&state, id, retry).await {
                        return response;
                    }
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
            Ok(response) => {
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
                return HttpResponse::build(actix_web::http::StatusCode::from_u16(response.status().as_u16()).unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY))
                    .json(json!({"error":{"message":"NVIDIA rejected the request","type":"upstream_request_rejected"}}));
            }
            Err(_) => {
                if let Err(response) = record_failure(&state, id, None).await {
                    return response;
                }
                if let Err(response) = attempt_finished(&state, request_id, id, "failed").await {
                    return response;
                }
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        return HttpResponse::TooManyRequests()
            .insert_header(("retry-after", seconds.to_string()))
            .json(json!({"error":{"message":"All NVIDIA upstream keys are rate limited","type":"upstream_rate_limited"}}));
    }
    HttpResponse::ServiceUnavailable().json(json!({"error":{"message":"No eligible NVIDIA upstream key","type":"upstream_unavailable"}}))
}

pub(crate) fn parse_multimodal_request(
    path: &str,
    body: &[u8],
    content_type: &str,
) -> Result<Value, HttpResponse> {
    if path == "/v1/audio/transcriptions" && content_type.starts_with("multipart/form-data") {
        let boundary = content_type
            .split(';')
            .map(str::trim)
            .find_map(|part| part.strip_prefix("boundary="))
            .map(|value| value.trim_matches('"'))
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_request("multipart boundary is required"))?;
        let marker = format!("--{boundary}").into_bytes();
        let has_marker = body.windows(marker.len()).any(|window| window == marker);
        let has_model = body
            .windows(b"name=\"model\"".len())
            .any(|window| window == b"name=\"model\"");
        let has_file = body
            .windows(b"name=\"file\"".len())
            .any(|window| window == b"name=\"file\"");
        if !has_marker || !has_model || !has_file {
            return Err(invalid_request(
                "multipart transcription requires model and file fields",
            ));
        }
        if body.len() > 64 * 1024 * 1024 {
            return Err(invalid_request("multipart body exceeds 64 MiB"));
        }
        let has_audio_part = body
            .windows(b"Content-Type: audio/".len())
            .any(|window| window.eq_ignore_ascii_case(b"Content-Type: audio/"));
        if !has_audio_part {
            return Err(invalid_request(
                "multipart transcription file must declare an audio MIME type",
            ));
        }
        let text = String::from_utf8_lossy(body);
        let model = text
            .split("name=\"model\"")
            .nth(1)
            .and_then(|part| part.split("\r\n\r\n").nth(1))
            .and_then(|value| value.split("\r\n--").next())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_request("multipart model field is empty"))?;
        if !PROFILES.contains(&model) {
            return Err(model_not_found());
        }
        if !profile_supports_path(path, model) {
            return Err(HttpResponse::UnprocessableEntity().json(json!({
                "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
            })));
        }
        return Ok(json!({"model": model, "__nblb_multipart": true}));
    }

    if !content_type.starts_with("application/json") {
        return Err(invalid_request(
            "JSON content-type is required for this modality",
        ));
    }
    let request = serde_json::from_slice::<Value>(body)
        .map_err(|_| invalid_request("request body must be valid JSON"))?;
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| invalid_request("model is required"))?;
    if !PROFILES.contains(&model) {
        return Err(model_not_found());
    }
    if !profile_supports_path(path, model) {
        return Err(HttpResponse::UnprocessableEntity().json(json!({
            "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
        })));
    }
    validate_modality_fields(path, object)?;
    let required_field = match path {
        "/v1/images/generations" => Some("prompt"),
        "/v1/videos/generations" => Some("input_reference"),
        "/v1/embeddings" => Some("input"),
        "/v1/audio/speech" => Some("input"),
        _ => None,
    };
    if let Some(field) = required_field
        && !object.contains_key(field)
    {
        return Err(invalid_request(&format!("{field} is required")));
    }
    Ok(request)
}

/// Reads a request body only after the route has passed host and bearer
/// authorization.  Using the raw Actix payload here prevents an unauthenticated
/// caller from forcing JSON extraction and validation work before rejection.
async fn read_request_body(
    payload: &mut web::Payload,
    limit: usize,
) -> Result<Bytes, HttpResponse> {
    let mut body = BytesMut::new();
    while let Some(chunk) = payload.next().await {
        let chunk = chunk.map_err(|_| {
            HttpResponse::BadRequest().json(json!({
                "error": {"message": "request body could not be read", "type": "invalid_request"}
            }))
        })?;
        if body.len().saturating_add(chunk.len()) > limit {
            return Err(HttpResponse::PayloadTooLarge().json(json!({
                "error": {"message": "request body exceeds the endpoint limit", "type": "request_too_large"}
            })));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body.freeze())
}

fn modality_body_limit(path: &str) -> usize {
    match path {
        "/v1/images/generations" => 256 * 1024,
        "/v1/audio/speech" => 64 * 1024,
        "/v1/nvidia/inference" => 1024 * 1024,
        "/v1/embeddings" | "/v1/audio/transcriptions" => 32 * 1024 * 1024,
        "/v1/videos/generations" => 32 * 1024 * 1024,
        _ => 64 * 1024 * 1024,
    }
}

fn profile_supports_path(path: &str, model: &str) -> bool {
    match path {
        "/v1/chat/completions" => matches!(
            model,
            "z-ai/glm-5.2" | "microsoft/phi-4-multimodal-instruct" | "nvidia/vila"
        ),
        "/v1/embeddings" => model == "nvidia/nvclip",
        "/v1/images/generations" => model == "black-forest-labs/flux.1-kontext-dev",
        "/v1/videos/generations" => model == "stabilityai/stable-video-diffusion",
        "/v1/audio/speech" => model == "nvidia/magpie-tts-multilingual",
        "/v1/audio/transcriptions" => model == "nvidia/parakeet-ctc-1.1b",
        "/v1/nvidia/inference" => model == "stabilityai/stable-video-diffusion",
        _ => false,
    }
}

fn retry_after_duration(response: &reqwest::Response) -> Option<Duration> {
    let value = response.headers().get("retry-after")?.to_str().ok()?.trim();
    if let Ok(seconds) = value.parse::<i64>() {
        return (1..=300)
            .contains(&seconds)
            .then(|| Duration::seconds(seconds));
    }
    let deadline = httpdate::parse_http_date(value).ok()?;
    let remaining = deadline.duration_since(std::time::SystemTime::now()).ok()?;
    let seconds = i64::try_from(remaining.as_secs()).ok()?.clamp(1, 300);
    Some(Duration::seconds(seconds))
}

fn retry_after_for_keys(keys: &[nvidia_build_lb_core::KeySummary]) -> Option<u64> {
    let now = Utc::now();
    if keys.is_empty()
        || keys.iter().any(|key| {
            key.enabled && key.verified && key.cooldown_until.is_none_or(|until| until <= now)
        })
    {
        return None;
    }
    keys.iter()
        .filter_map(|key| key.cooldown_until)
        .filter_map(|until| (until - now).num_seconds().try_into().ok())
        .min()
        .map(|seconds: u64| seconds.clamp(1, 300))
}

fn model_not_found() -> HttpResponse {
    HttpResponse::NotFound().json(json!({
        "error": {"message": "model is not advertised", "type": "model_not_found"}
    }))
}

fn validate_modality_fields(
    path: &str,
    object: &serde_json::Map<String, Value>,
) -> Result<(), HttpResponse> {
    let (required, allowed): (&[&str], &[&str]) = match path {
        "/v1/embeddings" => (
            &["input"],
            &["model", "input", "encoding_format", "dimensions", "user"],
        ),
        "/v1/images/generations" => (
            &["prompt"],
            &["model", "prompt", "n", "size", "response_format", "user"],
        ),
        "/v1/audio/speech" => (
            &["input"],
            &["model", "input", "voice", "response_format", "speed"],
        ),
        "/v1/videos/generations" => (
            &["input_reference"],
            &[
                "model",
                "input_reference",
                "seed",
                "cfg_scale",
                "motion_bucket_id",
            ],
        ),
        "/v1/nvidia/inference" => (&["input"], &["model", "input"]),
        _ => (&[], &["model"]),
    };
    for field in required {
        if !object.contains_key(*field) {
            return Err(invalid_request(&format!("{field} is required")));
        }
    }
    if let Some(unknown) = object
        .keys()
        .find(|field| !allowed.contains(&field.as_str()))
    {
        return Err(invalid_request(&format!("unsupported field: {unknown}")));
    }
    match path {
        "/v1/embeddings" => {
            let valid = object["input"]
                .as_str()
                .is_some_and(|value| !value.is_empty())
                || object["input"].as_array().is_some_and(|items| {
                    !items.is_empty()
                        && items.len() <= 64
                        && items
                            .iter()
                            .all(|item| item.as_str().is_some_and(|value| !value.is_empty()))
                });
            if valid {
                Ok(())
            } else {
                Err(invalid_request(
                    "input must be a non-empty string or an array of at most 64 strings",
                ))
            }
        }
        "/v1/images/generations" => {
            if !object["prompt"].is_string() {
                return Err(invalid_request("prompt must be a string"));
            }
            if let Some(size) = object.get("size")
                && !matches!(
                    size.as_str(),
                    Some("1024x1024" | "1792x1024" | "1536x864" | "1024x1792" | "864x1536")
                )
            {
                return Err(invalid_request(
                    "size must be one of 1024x1024, 1792x1024, 1536x864, 1024x1792, 864x1536",
                ));
            }
            if object.get("n").and_then(Value::as_u64).unwrap_or(1) != 1 {
                return Err(invalid_request("n must be 1"));
            }
            if let Some(format) = object.get("response_format")
                && format.as_str() != Some("b64_json")
            {
                return Err(invalid_request("response_format must be b64_json"));
            }
            Ok(())
        }
        "/v1/videos/generations" => {
            if object
                .get("input_reference")
                .and_then(Value::as_str)
                .is_none_or(|value| !valid_data_url(value, true))
            {
                return Err(invalid_request(
                    "input_reference must be a valid PNG or JPEG data URL",
                ));
            }
            validate_video_options(object)
        }
        "/v1/audio/speech" => {
            if !object["input"].is_string() {
                return Err(invalid_request("input must be a string"));
            }
            if let Some(format) = object.get("response_format")
                && format.as_str() != Some("wav")
            {
                return Err(invalid_request("response_format must be wav"));
            }
            if let Some(speed) = object.get("speed")
                && speed.as_f64() != Some(1.0)
            {
                return Err(invalid_request("speed must be 1.0"));
            }
            if let Some(voice) = object.get("voice")
                && voice.as_str().is_none_or(|value| {
                    value.trim().is_empty() || value.chars().any(char::is_control)
                })
            {
                return Err(invalid_request("voice must be a non-empty safe string"));
            }
            Ok(())
        }
        "/v1/nvidia/inference" => {
            let input = object
                .get("input")
                .ok_or_else(|| invalid_request("input is required"))?;
            let Some(input) = input.as_object() else {
                return Err(invalid_request("input must be an object"));
            };
            if let Some(unknown) = input.keys().find(|field| {
                !["image", "seed", "cfg_scale", "motion_bucket_id"].contains(&field.as_str())
            }) {
                return Err(invalid_request(&format!(
                    "unsupported input field: {unknown}"
                )));
            }
            if input
                .get("image")
                .and_then(Value::as_str)
                .is_none_or(|value| !valid_data_url(value, true))
            {
                return Err(invalid_request(
                    "input.image must be a valid PNG or JPEG data URL",
                ));
            }
            if let Some(seed) = input.get("seed")
                && seed
                    .as_i64()
                    .is_none_or(|value| !(0..=u32::MAX as i64).contains(&value))
            {
                return Err(invalid_request(
                    "seed must be an integer between 0 and 4294967295",
                ));
            }
            if let Some(cfg) = input.get("cfg_scale")
                && cfg.as_f64().is_none_or(|value| {
                    value.is_nan() || !(1.0..=9.0).contains(&value) || value == 1.0
                })
            {
                return Err(invalid_request(
                    "cfg_scale must be greater than 1 and at most 9",
                ));
            }
            if let Some(bucket) = input.get("motion_bucket_id")
                && bucket.as_i64() != Some(127)
            {
                return Err(invalid_request("motion_bucket_id must equal 127"));
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn validate_video_options(object: &serde_json::Map<String, Value>) -> Result<(), HttpResponse> {
    if let Some(seed) = object.get("seed")
        && seed
            .as_i64()
            .is_none_or(|value| !(0..=u32::MAX as i64).contains(&value))
    {
        return Err(invalid_request(
            "seed must be an integer between 0 and 4294967295",
        ));
    }
    if let Some(cfg) = object.get("cfg_scale")
        && cfg
            .as_f64()
            .is_none_or(|value| value.is_nan() || !(1.0..=9.0).contains(&value) || value == 1.0)
    {
        return Err(invalid_request(
            "cfg_scale must be greater than 1 and at most 9",
        ));
    }
    if let Some(bucket) = object.get("motion_bucket_id")
        && bucket.as_i64() != Some(127)
    {
        return Err(invalid_request("motion_bucket_id must equal 127"));
    }
    Ok(())
}

pub(crate) fn validate_chat_request(request: &Value) -> Result<(), HttpResponse> {
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| invalid_request("model is required"))?;
    if !PROFILES.contains(&model) {
        return Err(model_not_found());
    }
    if !profile_supports_path("/v1/chat/completions", model) {
        return Err(HttpResponse::UnprocessableEntity().json(json!({
            "error": {"message": "model is not compatible with this endpoint", "type": "model_route_mismatch"}
        })));
    }
    const GLM_ALLOWED: &[&str] = &[
        "model",
        "messages",
        "stream",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "tools",
        "tool_choice",
        "response_format",
        "user",
        "n",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "metadata",
        "chat_template_kwargs",
    ];
    let allowed = if model == "z-ai/glm-5.2" {
        GLM_ALLOWED
    } else {
        &[
            "model",
            "messages",
            "stream",
            "temperature",
            "top_p",
            "max_tokens",
            "seed",
        ]
    };
    if let Some(unknown) = object
        .keys()
        .find(|field| !allowed.contains(&field.as_str()))
    {
        return Err(invalid_request(&format!("unsupported field: {unknown}")));
    }
    let messages = object
        .get("messages")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_request("messages must be an array"))?;
    if messages.is_empty() {
        return Err(invalid_request(
            "messages must contain valid role/content entries",
        ));
    }
    let allowed_content = match model {
        "microsoft/phi-4-multimodal-instruct" => &["text", "image_url", "audio_url"][..],
        "nvidia/vila" => &["text", "image_url", "video_url"][..],
        _ => &["text"][..],
    };
    for message in messages {
        let Some(message) = message.as_object() else {
            return Err(invalid_request(
                "messages must contain valid role/content entries",
            ));
        };
        if !matches!(
            message.get("role").and_then(Value::as_str),
            Some("system" | "user" | "assistant" | "tool")
        ) {
            return Err(invalid_request("message role is not supported"));
        }
        let Some(content) = message.get("content") else {
            return Err(invalid_request("message content is required"));
        };
        if content.is_null()
            && message.get("role").and_then(Value::as_str) == Some("assistant")
            && (message.get("tool_calls").is_some_and(Value::is_array)
                || message.get("function_call").is_some_and(Value::is_object))
        {
            continue;
        }
        if !validate_chat_content(content, allowed_content) {
            return Err(invalid_request(
                "message content does not match the selected model",
            ));
        }
    }
    Ok(())
}

fn validate_chat_content(content: &Value, allowed_types: &[&str]) -> bool {
    if let Some(text) = content.as_str() {
        return !text.is_empty();
    }
    let Some(items) = content.as_array() else {
        return false;
    };
    !items.is_empty()
        && items.iter().all(|item| {
            let Some(item) = item.as_object() else {
                return false;
            };
            let Some(kind) = item.get("type").and_then(Value::as_str) else {
                return false;
            };
            if !allowed_types.contains(&kind) {
                return false;
            }
            match kind {
                "text" => {
                    item.get("text")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.is_empty())
                        && item
                            .keys()
                            .all(|key| matches!(key.as_str(), "type" | "text"))
                }
                "image_url" | "audio_url" | "video_url" => {
                    item.keys().all(|key| {
                        matches!(
                            key.as_str(),
                            "type" | "image_url" | "audio_url" | "video_url"
                        )
                    }) && item
                        .get(kind)
                        .is_some_and(|value| value.is_string() || value.is_object())
                }
                _ => false,
            }
        })
}
