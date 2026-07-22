use actix_web::{
    HttpRequest, HttpResponse, Responder,
    http::{StatusCode, header},
    web,
};
use bytes::{Bytes, BytesMut};
use chrono::{Duration, Utc};
use futures_util::StreamExt;
use serde_json::{Value, json};

use super::{
    AppState, AttemptTerminal, PROFILES, RequestEvidence, RequestTerminal, STREAM_PRIME_TIMEOUT,
    STREAM_RESPONSE_HEADER_TIMEOUT, StreamAttemptGuard, UPSTREAM_REQUEST_TIMEOUT, attempt_finished,
    attempt_response_started, attempt_started, authorize_scope, chat_response_stream,
    invalid_request, mock_modality, mock_response, mock_stream_response, modality_for_path,
    normalize_modality_response, openai_error, poll_nvcf, prepare_modality_request, prime_stream,
    public_guard_response, quarantine_key, record_accepted_downstream_use, record_failure,
    record_request, request_id::request_id, select_failover_key, sse_error_frame,
    upstream_endpoint_for, valid_data_url, validate_chat_response,
};

const CHAT_RESPONSE_LIMIT: usize = 16 * 1024 * 1024;

struct RetryableAttempt<'a> {
    request_id: uuid::Uuid,
    profile: &'a str,
    attempted: &'a [uuid::Uuid],
    retry_allowed: bool,
    key_id: uuid::Uuid,
    attempt: AttemptTerminal,
    all_attempts_rate_limited: bool,
}

impl<'a> RetryableAttempt<'a> {
    fn new(
        request_id: uuid::Uuid,
        profile: &'a str,
        attempted: &'a [uuid::Uuid],
        retry_allowed: bool,
        key_id: uuid::Uuid,
        attempt: AttemptTerminal,
        all_attempts_rate_limited: bool,
    ) -> Self {
        Self {
            request_id,
            profile,
            attempted,
            retry_allowed,
            key_id,
            attempt,
            all_attempts_rate_limited,
        }
    }
}

macro_rules! finish_retryable_or_terminal {
    (
        $state:expr,
        $evidence:expr,
        $request_id:expr,
        $profile:expr,
        $attempted:expr,
        $retry_allowed:expr,
        $key_id:expr,
        $attempt:expr,
        $all_attempts_rate_limited:expr $(,)?
    ) => {
        finish_retryable_or_terminal(
            $state,
            $evidence,
            RetryableAttempt::new(
                $request_id,
                $profile,
                $attempted,
                $retry_allowed,
                $key_id,
                $attempt,
                $all_attempts_rate_limited,
            ),
        )
    };
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ResponseBodyError {
    TooLarge,
    Transport,
}

fn response_body_limit(path: &str) -> usize {
    match path {
        "/v1/videos/generations" => 128 * 1024 * 1024,
        "/v1/images/generations" | "/v1/audio/speech" => 32 * 1024 * 1024,
        "/v1/embeddings" | "/v1/audio/transcriptions" | "/v1/nvidia/inference" => 16 * 1024 * 1024,
        _ => CHAT_RESPONSE_LIMIT,
    }
}

async fn bounded_response_bytes(
    response: reqwest::Response,
    limit: usize,
) -> Result<Bytes, ResponseBodyError> {
    if response
        .content_length()
        .is_some_and(|length| length > u64::try_from(limit).unwrap_or(u64::MAX))
    {
        return Err(ResponseBodyError::TooLarge);
    }
    let mut body = BytesMut::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| ResponseBodyError::Transport)?;
        if body.len().saturating_add(chunk.len()) > limit {
            return Err(ResponseBodyError::TooLarge);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body.freeze())
}

async fn consume_live_failure_fixture(
    state: &AppState,
    downstream_credential_id: Option<uuid::Uuid>,
    request_id: uuid::Uuid,
    key_id: uuid::Uuid,
) -> Result<Option<String>, HttpResponse> {
    let (Some(pool), Some(client_id)) = (&state.vault.database, downstream_credential_id) else {
        return Ok(None);
    };
    sqlx::query_scalar::<_, String>(
        "UPDATE nblb.qa_failure_fixtures fixture SET consumed_at=now(),request_id=$2,key_id=$3 WHERE (fixture.run_id,fixture.kind)=(SELECT candidate.run_id,candidate.kind FROM nblb.qa_failure_fixtures candidate JOIN nblb.qa_runs run ON run.id=candidate.run_id WHERE candidate.downstream_credential_id=$1 AND candidate.consumed_at IS NULL AND run.status='running' AND run.live=true AND run.suite='failover' ORDER BY CASE candidate.kind WHEN 'before_first_frame' THEN 0 ELSE 1 END FOR UPDATE OF candidate SKIP LOCKED LIMIT 1) RETURNING fixture.kind",
    )
    .bind(client_id)
    .bind(request_id)
    .bind(key_id)
    .fetch_optional(pool)
    .await
    .map_err(|_| {
        openai_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "QA failure fixture state could not be persisted",
            "service_unavailable_error",
            "evidence_unavailable",
        )
    })
}

async fn injected_post_frame_failure(
    state: &web::Data<AppState>,
    request_id: uuid::Uuid,
    key_id: uuid::Uuid,
    mut evidence: RequestEvidence,
) -> HttpResponse {
    let prefix = Bytes::from_static(
        b"data: {\"id\":\"chatcmpl-qa-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n",
    );
    let error = sse_error_frame("controlled QA failure after the first frame");
    let bytes_out = prefix.len().saturating_add(error.len());
    if let Err(response) = attempt_response_started(state, request_id, key_id, 0).await {
        return response;
    }
    if let Err(response) = evidence
        .finish_with_attempt(
            key_id,
            AttemptTerminal::failed(Some(200), "qa_injected_post_frame_failure", None)
                .with_bytes_out(bytes_out),
            RequestTerminal::failed(502, "qa_injected_post_frame_failure").with_ttfb(Some(0)),
        )
        .await
    {
        return response;
    }
    let mut body = BytesMut::with_capacity(bytes_out);
    body.extend_from_slice(&prefix);
    body.extend_from_slice(&error);
    HttpResponse::Ok()
        .insert_header((header::CONTENT_TYPE, "text/event-stream"))
        .insert_header((header::CACHE_CONTROL, "no-cache"))
        .body(body.freeze())
}

pub(crate) async fn chat_completions(
    req: HttpRequest,
    state: web::Data<AppState>,
    mut payload: web::Payload,
) -> impl Responder {
    if let Some(response) = public_guard_response(&req) {
        return response;
    }
    let downstream_credential_id = match authorize_scope(&req, &state, "chat:write").await {
        Ok(id) => id,
        Err(response) => return response,
    };
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
    let request_id = request_id(&req);
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let stream = request
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let (mut evidence, first_key_id) = match RequestEvidence::admit_and_start_selected(
        state.clone(),
        request_id,
        downstream_credential_id,
        req.path(),
        profile,
        stream,
        modality_for_path(req.path()),
    )
    .await
    {
        Ok(result) => result,
        Err(response) => return response,
    };
    if let Err(response) = record_accepted_downstream_use(&state, downstream_credential_id).await {
        let _ = evidence
            .finish_with_open_attempts_silently(
                "failed",
                Some(response.status().as_u16()),
                "downstream_credential_usage_record_failed",
                None,
                0,
            )
            .await;
        return response;
    }
    let key_attempts = state.vault.list().len().max(1);
    let mut attempted = Vec::new();
    let mut next_key_id = None;
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let first_attempt = attempted.is_empty();
        let id = if first_attempt {
            Some(first_key_id)
        } else if next_key_id.is_some() {
            next_key_id.take()
        } else {
            select_failover_key(&state, profile, &attempted).await
        };
        let Some(id) = id else { break };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if !first_attempt
            && let Err(response) =
                start_evidenced_attempt(&state, &mut evidence, request_id, profile, id).await
        {
            return response;
        }
        if first_attempt {
            match consume_live_failure_fixture(&state, downstream_credential_id, request_id, id)
                .await
            {
                Ok(Some(kind)) if kind == "before_first_frame" => {
                    evidence.arm_evidence_failure(None, 0);
                    let cooldown =
                        match record_failure(&state, id, Some(Duration::seconds(2))).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                    next_key_id = match finish_retryable_or_terminal!(
                        &state,
                        &mut evidence,
                        request_id,
                        profile,
                        &attempted,
                        attempted.len() < key_attempts,
                        id,
                        AttemptTerminal::failed(None, "qa_injected_transport_failure", cooldown),
                        false,
                    )
                    .await
                    {
                        Ok(next) => Some(next),
                        Err(response) => return response,
                    };
                    continue;
                }
                Ok(Some(kind)) if kind == "after_first_frame" => {
                    if !stream {
                        if let Err(response) = evidence
                            .fail(
                                StatusCode::INTERNAL_SERVER_ERROR,
                                "qa_fixture_contract_error",
                            )
                            .await
                        {
                            return response;
                        }
                        return openai_error(
                            StatusCode::INTERNAL_SERVER_ERROR,
                            "QA post-frame fixture requires streaming",
                            "server_error",
                            "qa_fixture_contract_error",
                        );
                    }
                    return injected_post_frame_failure(&state, request_id, id, evidence).await;
                }
                Ok(Some(_)) => {
                    return openai_error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Unknown QA failure fixture",
                        "server_error",
                        "qa_fixture_contract_error",
                    );
                }
                Ok(None) => {}
                Err(response) => return response,
            }
        }
        let credential = match state.vault.credential(id) {
            Ok(value) => value,
            Err(_) => {
                all_attempts_rate_limited = false;
                evidence.arm_evidence_failure(None, 0);
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(None, "credential_unavailable", None),
                    false,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            let mock_status = first_attempt.then(|| mock_status(&request)).flatten();
            if let Some(status) = mock_status {
                let error_class = retryable_error_class(status);
                evidence.arm_evidence_failure(None, 0);
                if matches!(status, 401 | 403) {
                    all_attempts_rate_limited = false;
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    next_key_id = match finish_retryable_or_terminal!(
                        &state,
                        &mut evidence,
                        request_id,
                        profile,
                        &attempted,
                        attempted.len() < key_attempts,
                        id,
                        AttemptTerminal::failed(Some(status), error_class, None),
                        false,
                    )
                    .await
                    {
                        Ok(next) => Some(next),
                        Err(response) => return response,
                    };
                    continue;
                }
                if matches!(status, 429 | 500) {
                    rate_limited |= status == 429;
                    all_attempts_rate_limited &= status == 429;
                    let cooldown = match record_failure(
                        &state,
                        id,
                        Some(Duration::seconds(if status == 429 { 30 } else { 2 })),
                    )
                    .await
                    {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    };
                    next_key_id = match finish_retryable_or_terminal!(
                        &state,
                        &mut evidence,
                        request_id,
                        profile,
                        &attempted,
                        attempted.len() < key_attempts,
                        id,
                        AttemptTerminal::failed(Some(status), error_class, cooldown),
                        all_attempts_rate_limited,
                    )
                    .await
                    {
                        Ok(next) => Some(next),
                        Err(response) => return response,
                    };
                    continue;
                }
                let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(
                            Some(status.as_u16()),
                            "upstream_request_rejected",
                            None,
                        ),
                        RequestTerminal::rejected(status.as_u16(), "upstream_request_rejected"),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    status,
                    "mock provider rejected the request",
                    "upstream_error",
                    "upstream_request_rejected",
                );
            }
            all_attempts_rate_limited = false;
            if first_attempt && mock_response_kind(&request) == Some("invalid_json") {
                evidence.arm_evidence_failure(None, 8);
                let cooldown = match record_failure(&state, id, None).await {
                    Ok(cooldown) => cooldown,
                    Err(response) => return response,
                };
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(Some(200), "upstream_protocol_error", cooldown)
                            .with_bytes_out(8),
                        RequestTerminal::failed(502, "upstream_protocol_error"),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    StatusCode::BAD_GATEWAY,
                    "mock provider returned invalid JSON",
                    "upstream_error",
                    "upstream_protocol_error",
                );
            }
            let stream_scenario = mock_stream_scenario(&request);
            let force_first_failure = request
                .get("metadata")
                .and_then(Value::as_object)
                .is_some_and(|metadata| {
                    metadata
                        .get("force_first_upstream_failure")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                        || matches!(
                            stream_scenario,
                            Some("disconnect_before_first_frame" | "invalid_sse")
                        )
                })
                && attempted.len() == 1;
            if credential.contains("fail") || force_first_failure {
                evidence.arm_evidence_failure(None, 0);
                let cooldown = match record_failure(&state, id, Some(Duration::seconds(2))).await {
                    Ok(cooldown) => cooldown,
                    Err(response) => return response,
                };
                let error_class = if stream {
                    if stream_scenario == Some("invalid_sse") {
                        "upstream_protocol_error"
                    } else {
                        "mock_stream_prime_error"
                    }
                } else {
                    "mock_upstream_failure"
                };
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(None, error_class, cooldown),
                    false,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
            if stream {
                return mock_stream_response(&request, state.clone(), request_id, id, evidence)
                    .await;
            }
            evidence.arm_evidence_failure(None, 0);
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            if let Err(response) = evidence
                .finish_with_attempt(
                    id,
                    AttemptTerminal::succeeded(200, None),
                    RequestTerminal::succeeded(200),
                )
                .await
            {
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
        let result = if stream {
            tokio::time::timeout(STREAM_RESPONSE_HEADER_TIMEOUT, upstream_request.send())
                .await
                .map_err(|_| ())
                .and_then(|result| result.map_err(|_| ()))
        } else {
            upstream_request.send().await.map_err(|_| ())
        };
        match result.as_ref() {
            Ok(response) if response.status().as_u16() == 429 => rate_limited = true,
            _ => all_attempts_rate_limited = false,
        }
        let provider_status = result
            .as_ref()
            .ok()
            .map(|response| response.status().as_u16());
        let routing_policy = load_routing_policy(&state).await;
        let provider_failover = provider_status.is_some_and(|status| {
            matches!(status, 401 | 403) || routing_policy.retryable_statuses.contains(&status)
        });
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
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        if let Err(response) = evidence
                            .finish_with_attempt(
                                id,
                                AttemptTerminal::failed(Some(202), "upstream_poll_error", cooldown),
                                RequestTerminal::failed(
                                    StatusCode::BAD_GATEWAY.as_u16(),
                                    "upstream_poll_error",
                                ),
                            )
                            .await
                        {
                            return response;
                        }
                        return openai_error(
                            StatusCode::BAD_GATEWAY,
                            "NVIDIA accepted the request but polling did not complete",
                            "upstream_error",
                            "upstream_poll_error",
                        );
                    }
                };
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                let bytes = match bounded_response_bytes(response, CHAT_RESPONSE_LIMIT).await {
                    Ok(bytes) => bytes,
                    Err(ResponseBodyError::TooLarge) => {
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        if let Err(response) = evidence
                            .finish_with_attempt(
                                id,
                                AttemptTerminal::failed(
                                    Some(status.as_u16()),
                                    "upstream_response_too_large",
                                    cooldown,
                                ),
                                RequestTerminal::failed(
                                    StatusCode::BAD_GATEWAY.as_u16(),
                                    "upstream_response_too_large",
                                ),
                            )
                            .await
                        {
                            return response;
                        }
                        return openai_error(
                            StatusCode::BAD_GATEWAY,
                            "provider response exceeded the endpoint size limit",
                            "upstream_error",
                            "upstream_response_too_large",
                        );
                    }
                    Err(ResponseBodyError::Transport) => {
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        next_key_id = match finish_retryable_or_terminal!(
                            &state,
                            &mut evidence,
                            request_id,
                            profile,
                            &attempted,
                            attempted.len() < key_attempts,
                            id,
                            AttemptTerminal::failed(
                                Some(status.as_u16()),
                                "upstream_body_error",
                                cooldown,
                            ),
                            false,
                        )
                        .await
                        {
                            Ok(next) => Some(next),
                            Err(response) => return response,
                        };
                        continue;
                    }
                };
                if validate_chat_response(&bytes, false).is_err() {
                    evidence.arm_evidence_failure(None, bytes.len());
                    let cooldown = match record_failure(&state, id, None).await {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    };
                    if let Err(response) = evidence
                        .finish_with_attempt(
                            id,
                            AttemptTerminal::failed(
                                Some(status.as_u16()),
                                "upstream_protocol_error",
                                cooldown,
                            )
                            .with_bytes_out(bytes.len()),
                            RequestTerminal::failed(
                                StatusCode::BAD_GATEWAY.as_u16(),
                                "upstream_protocol_error",
                            ),
                        )
                        .await
                    {
                        return response;
                    }
                    return openai_error(
                        StatusCode::BAD_GATEWAY,
                        "provider returned an invalid chat response",
                        "upstream_error",
                        "upstream_protocol_error",
                    );
                }
                evidence.arm_evidence_failure(None, bytes.len());
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::succeeded(status.as_u16(), Some(bytes.len())),
                        RequestTerminal::succeeded(status.as_u16()),
                    )
                    .await
                {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", "application/json"))
                    .body(bytes);
            }
            Ok(response) if response.status().as_u16() == 202 => {
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(Some(202), "upstream_protocol_error", None),
                        RequestTerminal::failed(
                            StatusCode::BAD_GATEWAY.as_u16(),
                            "upstream_protocol_error",
                        ),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    StatusCode::BAD_GATEWAY,
                    "NVIDIA returned an unsupported asynchronous response",
                    "upstream_error",
                    "upstream_protocol_error",
                );
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
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        next_key_id = match finish_retryable_or_terminal!(
                            &state,
                            &mut evidence,
                            request_id,
                            profile,
                            &attempted,
                            attempted.len() < key_attempts,
                            id,
                            AttemptTerminal::failed(
                                Some(status.as_u16()),
                                "unexpected_stream_content_type",
                                cooldown,
                            ),
                            false,
                        )
                        .await
                        {
                            Ok(next) => Some(next),
                            Err(response) => return response,
                        };
                        continue;
                    }
                    let upstream = Box::pin(response.bytes_stream());
                    // Own the started attempt before awaiting the first SSE
                    // frame. If the client disconnects while the provider is
                    // silent, dropping this guard still closes the ledger row.
                    let mut stream_guard = StreamAttemptGuard::with_request(evidence);
                    let (upstream, validator, prefix) =
                        match tokio::time::timeout(STREAM_PRIME_TIMEOUT, prime_stream(upstream))
                            .await
                        {
                            Ok(Ok(value)) => value,
                            _ => {
                                stream_guard.arm_evidence_failure(None, 0);
                                let cooldown = match record_failure(&state, id, None).await {
                                    Ok(cooldown) => cooldown,
                                    Err(response) => return response,
                                };
                                evidence = stream_guard.release_request();
                                next_key_id = match finish_retryable_or_terminal!(
                                    &state,
                                    &mut evidence,
                                    request_id,
                                    profile,
                                    &attempted,
                                    attempted.len() < key_attempts,
                                    id,
                                    AttemptTerminal::failed(
                                        Some(status.as_u16()),
                                        "stream_prime_error",
                                        cooldown,
                                    ),
                                    false,
                                )
                                .await
                                {
                                    Ok(next) => Some(next),
                                    Err(response) => return response,
                                };
                                continue;
                            }
                        };
                    let ttfb_ms = stream_guard.request_elapsed_ms();
                    stream_guard.arm_evidence_failure(Some(ttfb_ms), 0);
                    if let Err(response) =
                        attempt_response_started(&state, request_id, id, ttfb_ms).await
                    {
                        return response;
                    }
                    stream_guard.mark_response_started(ttfb_ms);
                    stream_guard.mark_response_committed(status.as_u16());
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
                let bytes = match bounded_response_bytes(response, CHAT_RESPONSE_LIMIT).await {
                    Ok(bytes) => bytes,
                    Err(ResponseBodyError::TooLarge) => {
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        if let Err(response) = evidence
                            .finish_with_attempt(
                                id,
                                AttemptTerminal::failed(
                                    Some(status.as_u16()),
                                    "upstream_response_too_large",
                                    cooldown,
                                ),
                                RequestTerminal::failed(
                                    StatusCode::BAD_GATEWAY.as_u16(),
                                    "upstream_response_too_large",
                                ),
                            )
                            .await
                        {
                            return response;
                        }
                        return openai_error(
                            StatusCode::BAD_GATEWAY,
                            "provider response exceeded the endpoint size limit",
                            "upstream_error",
                            "upstream_response_too_large",
                        );
                    }
                    Err(ResponseBodyError::Transport) => {
                        evidence.arm_evidence_failure(None, 0);
                        let cooldown = match record_failure(&state, id, None).await {
                            Ok(cooldown) => cooldown,
                            Err(response) => return response,
                        };
                        next_key_id = match finish_retryable_or_terminal!(
                            &state,
                            &mut evidence,
                            request_id,
                            profile,
                            &attempted,
                            attempted.len() < key_attempts,
                            id,
                            AttemptTerminal::failed(
                                Some(status.as_u16()),
                                "upstream_body_error",
                                cooldown,
                            ),
                            false,
                        )
                        .await
                        {
                            Ok(next) => Some(next),
                            Err(response) => return response,
                        };
                        continue;
                    }
                };
                if validate_chat_response(&bytes, stream).is_err() {
                    evidence.arm_evidence_failure(None, bytes.len());
                    let cooldown = match record_failure(&state, id, None).await {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    };
                    if let Err(response) = evidence
                        .finish_with_attempt(
                            id,
                            AttemptTerminal::failed(
                                Some(status.as_u16()),
                                "upstream_protocol_error",
                                cooldown,
                            )
                            .with_bytes_out(bytes.len()),
                            RequestTerminal::failed(
                                StatusCode::BAD_GATEWAY.as_u16(),
                                "upstream_protocol_error",
                            ),
                        )
                        .await
                    {
                        return response;
                    }
                    return openai_error(
                        StatusCode::BAD_GATEWAY,
                        "provider returned an invalid chat response",
                        "upstream_error",
                        "upstream_protocol_error",
                    );
                }
                evidence.arm_evidence_failure(None, bytes.len());
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::succeeded(status.as_u16(), Some(bytes.len())),
                        RequestTerminal::succeeded(status.as_u16()),
                    )
                    .await
                {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response) if provider_failover => {
                // 402 means quota/credit exhaustion, not invalid custody. It
                // therefore receives the same bounded cooldown/failover path
                // as 429 instead of permanently quarantining the credential.
                let provider_status = response.status().as_u16();
                let error_class = retryable_error_class(provider_status);
                let auth_failure = matches!(provider_status, 401 | 403);
                evidence.arm_evidence_failure(None, 0);
                let cooldown = if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    None
                } else {
                    let retry =
                        retry_after_duration(&response).or(Some(routing_policy.default_cooldown));
                    match record_failure(&state, id, retry).await {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    }
                };
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(Some(provider_status), error_class, cooldown),
                    all_attempts_rate_limited,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
            Ok(response) => {
                let status = actix_web::http::StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(actix_web::http::StatusCode::BAD_GATEWAY);
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(
                            Some(status.as_u16()),
                            "upstream_request_rejected",
                            None,
                        ),
                        RequestTerminal::rejected(status.as_u16(), "upstream_request_rejected"),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    status,
                    "NVIDIA rejected the request",
                    "upstream_error",
                    "upstream_request_rejected",
                );
            }
            Err(_) => {
                evidence.arm_evidence_failure(None, 0);
                let cooldown = match record_failure(&state, id, None).await {
                    Ok(cooldown) => cooldown,
                    Err(response) => return response,
                };
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(None, "upstream_transport_error", cooldown),
                    false,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        let mut response = openai_error(
            StatusCode::TOO_MANY_REQUESTS,
            "All NVIDIA upstream keys are rate limited",
            "rate_limit_error",
            "upstream_rate_limited",
        );
        response.headers_mut().insert(
            header::RETRY_AFTER,
            header::HeaderValue::from_str(&seconds.to_string())
                .expect("cooldown seconds are a valid HTTP header value"),
        );
        if !evidence
            .finish_with_open_attempts_silently(
                "failed",
                Some(StatusCode::TOO_MANY_REQUESTS.as_u16()),
                "upstream_rate_limited",
                None,
                0,
            )
            .await
        {
            return openai_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Request evidence is temporarily unavailable.",
                "service_unavailable_error",
                "evidence_unavailable",
            );
        }
        return response;
    }
    if !evidence
        .finish_with_open_attempts_silently(
            "failed",
            Some(StatusCode::SERVICE_UNAVAILABLE.as_u16()),
            "no_eligible_upstream",
            None,
            0,
        )
        .await
    {
        return openai_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Request evidence is temporarily unavailable.",
            "service_unavailable_error",
            "evidence_unavailable",
        );
    }
    openai_error(
        StatusCode::SERVICE_UNAVAILABLE,
        "No eligible NVIDIA upstream is available.",
        "service_unavailable_error",
        "no_eligible_upstream",
    )
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
    let downstream_credential_id = match authorize_scope(&req, &state, scope).await {
        Ok(id) => id,
        Err(response) => return response,
    };
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
        return openai_error(
            StatusCode::PAYLOAD_TOO_LARGE,
            "request body exceeds the endpoint limit",
            "invalid_request_error",
            "request_too_large",
        );
    }
    let request = match parse_multimodal_request(req.path(), &body, content_type) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let upstream_request = match prepare_modality_request(req.path(), &request) {
        Ok(request) => request,
        Err(response) => return response,
    };
    let request_id = request_id(&req);
    let profile = request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or(PROFILES[0]);
    let (mut evidence, first_key_id) = match RequestEvidence::admit_and_start_selected(
        state.clone(),
        request_id,
        downstream_credential_id,
        req.path(),
        profile,
        false,
        modality_for_path(req.path()),
    )
    .await
    {
        Ok(result) => result,
        Err(response) => return response,
    };
    if let Err(response) = record_accepted_downstream_use(&state, downstream_credential_id).await {
        let _ = evidence
            .finish_with_open_attempts_silently(
                "failed",
                Some(response.status().as_u16()),
                "downstream_credential_usage_record_failed",
                None,
                0,
            )
            .await;
        return response;
    }
    let key_attempts = if matches!(
        req.path(),
        "/v1/images/generations" | "/v1/videos/generations"
    ) {
        1
    } else {
        state.vault.list().len().max(1)
    };
    let mut attempted = Vec::new();
    let mut next_key_id = None;
    let mut rate_limited = false;
    let mut all_attempts_rate_limited = true;
    for _ in 0..key_attempts {
        let first_attempt = attempted.is_empty();
        let selected = if first_attempt {
            Some(first_key_id)
        } else if next_key_id.is_some() {
            next_key_id.take()
        } else {
            select_failover_key(&state, profile, &attempted).await
        };
        let Some(id) = selected else {
            break;
        };
        if attempted.contains(&id) {
            break;
        }
        attempted.push(id);
        if !first_attempt
            && let Err(response) =
                start_evidenced_attempt(&state, &mut evidence, request_id, profile, id).await
        {
            return response;
        }
        let credential = match state.vault.credential(id).ok() {
            Some(value) => value,
            None => {
                all_attempts_rate_limited = false;
                evidence.arm_evidence_failure(None, 0);
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(None, "credential_unavailable", None),
                    false,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
        };
        if state.upstream_url.starts_with("mock://") {
            if first_attempt
                && let Some(status) = req
                    .headers()
                    .get("x-nblb-mock-status")
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<u16>().ok())
            {
                evidence.arm_evidence_failure(None, 0);
                let error_class = retryable_error_class(status);
                if matches!(status, 401 | 403) {
                    all_attempts_rate_limited = false;
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    next_key_id = match finish_retryable_or_terminal!(
                        &state,
                        &mut evidence,
                        request_id,
                        profile,
                        &attempted,
                        attempted.len() < key_attempts,
                        id,
                        AttemptTerminal::failed(Some(status), error_class, None),
                        false,
                    )
                    .await
                    {
                        Ok(next) => Some(next),
                        Err(response) => return response,
                    };
                    continue;
                }
                if matches!(status, 429 | 500) {
                    rate_limited |= status == 429;
                    all_attempts_rate_limited &= status == 429;
                    let cooldown = match record_failure(
                        &state,
                        id,
                        Some(Duration::seconds(if status == 429 { 30 } else { 2 })),
                    )
                    .await
                    {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    };
                    next_key_id = match finish_retryable_or_terminal!(
                        &state,
                        &mut evidence,
                        request_id,
                        profile,
                        &attempted,
                        attempted.len() < key_attempts,
                        id,
                        AttemptTerminal::failed(Some(status), error_class, cooldown),
                        all_attempts_rate_limited,
                    )
                    .await
                    {
                        Ok(next) => Some(next),
                        Err(response) => return response,
                    };
                    continue;
                }
                let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(
                            Some(status.as_u16()),
                            "upstream_request_rejected",
                            None,
                        ),
                        RequestTerminal::rejected(status.as_u16(), "upstream_request_rejected"),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    status,
                    "mock provider rejected the request",
                    "upstream_error",
                    "upstream_request_rejected",
                );
            }
            evidence.arm_evidence_failure(None, 0);
            if let Err(response) = record_request(&state, id).await {
                return response;
            }
            let (body, content_type) = if req.headers().contains_key("x-nblb-mock-invalid-response")
            {
                (b"not-a-provider-response".to_vec(), "application/json")
            } else {
                mock_modality(req.path(), &request)
            };
            let (body, content_type) =
                match normalize_modality_response(req.path(), &body, content_type) {
                    Ok(value) => value,
                    Err(_) => {
                        if let Err(response) = evidence
                            .finish_with_attempt(
                                id,
                                AttemptTerminal::failed(Some(502), "mock_contract_invalid", None),
                                RequestTerminal::failed(
                                    StatusCode::BAD_GATEWAY.as_u16(),
                                    "mock_contract_invalid",
                                ),
                            )
                            .await
                        {
                            return response;
                        }
                        return openai_error(
                            StatusCode::BAD_GATEWAY,
                            "mock provider fixture failed the modality contract",
                            "upstream_error",
                            "mock_contract_invalid",
                        );
                    }
                };
            if let Err(response) = evidence
                .finish_with_attempt(
                    id,
                    AttemptTerminal::succeeded(200, Some(body.len())),
                    RequestTerminal::succeeded(200),
                )
                .await
            {
                return response;
            }
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
        let provider_status = request_result
            .as_ref()
            .ok()
            .map(|response| response.status().as_u16());
        let routing_policy = load_routing_policy(&state).await;
        let provider_failover = provider_status.is_some_and(|status| {
            matches!(status, 401 | 403) || routing_policy.retryable_statuses.contains(&status)
        });
        match request_result {
            Ok(response) if response.status().is_success() => {
                // A 202 means NVIDIA accepted an asynchronous job. It is
                // already an upstream side effect, so poll the same request
                // id and never retry the POST on another key.
                let response = if response.status().as_u16() == 202 {
                    match poll_nvcf(&state.client, response, &endpoint, &credential).await {
                        Ok(response) => response,
                        Err(_) => {
                            evidence.arm_evidence_failure(None, 0);
                            let cooldown = match record_failure(&state, id, None).await {
                                Ok(cooldown) => cooldown,
                                Err(response) => return response,
                            };
                            if let Err(response) = evidence
                                .finish_with_attempt(
                                    id,
                                    AttemptTerminal::failed(
                                        Some(202),
                                        "upstream_poll_error",
                                        cooldown,
                                    ),
                                    RequestTerminal::failed(
                                        StatusCode::BAD_GATEWAY.as_u16(),
                                        "upstream_poll_error",
                                    ),
                                )
                                .await
                            {
                                return response;
                            }
                            return openai_error(
                                StatusCode::BAD_GATEWAY,
                                "NVIDIA accepted the media request but polling did not complete",
                                "upstream_error",
                                "upstream_poll_error",
                            );
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
                let bytes =
                    match bounded_response_bytes(response, response_body_limit(req.path())).await {
                        Ok(bytes) => bytes,
                        Err(ResponseBodyError::TooLarge) => {
                            evidence.arm_evidence_failure(None, 0);
                            let cooldown = match record_failure(&state, id, None).await {
                                Ok(cooldown) => cooldown,
                                Err(response) => return response,
                            };
                            if let Err(response) = evidence
                                .finish_with_attempt(
                                    id,
                                    AttemptTerminal::failed(
                                        Some(status.as_u16()),
                                        "upstream_response_too_large",
                                        cooldown,
                                    ),
                                    RequestTerminal::failed(
                                        StatusCode::BAD_GATEWAY.as_u16(),
                                        "upstream_response_too_large",
                                    ),
                                )
                                .await
                            {
                                return response;
                            }
                            return openai_error(
                                StatusCode::BAD_GATEWAY,
                                "provider response exceeded the endpoint size limit",
                                "upstream_error",
                                "upstream_response_too_large",
                            );
                        }
                        Err(ResponseBodyError::Transport) => {
                            evidence.arm_evidence_failure(None, 0);
                            let cooldown = match record_failure(&state, id, None).await {
                                Ok(cooldown) => cooldown,
                                Err(response) => return response,
                            };
                            next_key_id = match finish_retryable_or_terminal!(
                                &state,
                                &mut evidence,
                                request_id,
                                profile,
                                &attempted,
                                attempted.len() < key_attempts,
                                id,
                                AttemptTerminal::failed(
                                    Some(status.as_u16()),
                                    "upstream_body_error",
                                    cooldown,
                                ),
                                false,
                            )
                            .await
                            {
                                Ok(next) => Some(next),
                                Err(response) => return response,
                            };
                            continue;
                        }
                    };
                let (bytes, content_type) =
                    match normalize_modality_response(req.path(), &bytes, &content_type) {
                        Ok(value) => value,
                        Err(_) => {
                            evidence.arm_evidence_failure(None, bytes.len());
                            let cooldown = match record_failure(&state, id, None).await {
                                Ok(cooldown) => cooldown,
                                Err(response) => return response,
                            };
                            if let Err(response) = evidence
                                .finish_with_attempt(
                                    id,
                                    AttemptTerminal::failed(
                                        Some(status.as_u16()),
                                        "upstream_protocol_error",
                                        cooldown,
                                    )
                                    .with_bytes_out(bytes.len()),
                                    RequestTerminal::failed(
                                        StatusCode::BAD_GATEWAY.as_u16(),
                                        "upstream_protocol_error",
                                    ),
                                )
                                .await
                            {
                                return response;
                            }
                            return openai_error(
                                StatusCode::BAD_GATEWAY,
                                "provider returned an invalid modality response",
                                "upstream_error",
                                "upstream_protocol_error",
                            );
                        }
                    };
                evidence.arm_evidence_failure(None, bytes.len());
                if let Err(response) = record_request(&state, id).await {
                    return response;
                }
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::succeeded(status.as_u16(), Some(bytes.len())),
                        RequestTerminal::succeeded(status.as_u16()),
                    )
                    .await
                {
                    return response;
                }
                return HttpResponse::build(status)
                    .insert_header(("content-type", content_type))
                    .body(bytes);
            }
            Ok(response) if provider_failover => {
                // 402 is a provider entitlement/credit signal, so cooldown
                // and failover remain possible; only 401/403 quarantine.
                let provider_status = response.status().as_u16();
                let error_class = retryable_error_class(provider_status);
                let auth_failure = matches!(provider_status, 401 | 403);
                evidence.arm_evidence_failure(None, 0);
                let cooldown = if auth_failure {
                    if let Err(response) = quarantine_key(&state, id).await {
                        return response;
                    }
                    None
                } else {
                    let retry =
                        retry_after_duration(&response).or(Some(routing_policy.default_cooldown));
                    match record_failure(&state, id, retry).await {
                        Ok(cooldown) => cooldown,
                        Err(response) => return response,
                    }
                };
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(Some(provider_status), error_class, cooldown),
                    all_attempts_rate_limited,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
            Ok(response) => {
                let status = StatusCode::from_u16(response.status().as_u16())
                    .unwrap_or(StatusCode::BAD_GATEWAY);
                if let Err(response) = evidence
                    .finish_with_attempt(
                        id,
                        AttemptTerminal::failed(
                            Some(status.as_u16()),
                            "upstream_request_rejected",
                            None,
                        ),
                        RequestTerminal::rejected(status.as_u16(), "upstream_request_rejected"),
                    )
                    .await
                {
                    return response;
                }
                return openai_error(
                    status,
                    "NVIDIA rejected the request",
                    "upstream_error",
                    "upstream_request_rejected",
                );
            }
            Err(_) => {
                evidence.arm_evidence_failure(None, 0);
                let cooldown = match record_failure(&state, id, None).await {
                    Ok(cooldown) => cooldown,
                    Err(response) => return response,
                };
                next_key_id = match finish_retryable_or_terminal!(
                    &state,
                    &mut evidence,
                    request_id,
                    profile,
                    &attempted,
                    attempted.len() < key_attempts,
                    id,
                    AttemptTerminal::failed(None, "upstream_transport_error", cooldown),
                    false,
                )
                .await
                {
                    Ok(next) => Some(next),
                    Err(response) => return response,
                };
                continue;
            }
        }
    }
    if rate_limited
        && all_attempts_rate_limited
        && !attempted.is_empty()
        && let Some(seconds) = retry_after_for_keys(&state.vault.list())
    {
        let mut response = openai_error(
            StatusCode::TOO_MANY_REQUESTS,
            "All NVIDIA upstream keys are rate limited",
            "rate_limit_error",
            "upstream_rate_limited",
        );
        response.headers_mut().insert(
            header::RETRY_AFTER,
            header::HeaderValue::from_str(&seconds.to_string())
                .expect("cooldown seconds are a valid HTTP header value"),
        );
        if !evidence
            .finish_with_open_attempts_silently(
                "failed",
                Some(StatusCode::TOO_MANY_REQUESTS.as_u16()),
                "upstream_rate_limited",
                None,
                0,
            )
            .await
        {
            return openai_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Request evidence is temporarily unavailable.",
                "service_unavailable_error",
                "evidence_unavailable",
            );
        }
        return response;
    }
    if !evidence
        .finish_with_open_attempts_silently(
            "failed",
            Some(StatusCode::SERVICE_UNAVAILABLE.as_u16()),
            "no_eligible_upstream",
            None,
            0,
        )
        .await
    {
        return openai_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Request evidence is temporarily unavailable.",
            "service_unavailable_error",
            "evidence_unavailable",
        );
    }
    openai_error(
        StatusCode::SERVICE_UNAVAILABLE,
        "No eligible NVIDIA upstream is available.",
        "service_unavailable_error",
        "no_eligible_upstream",
    )
}

async fn finish_retryable_or_terminal(
    state: &web::Data<AppState>,
    evidence: &mut RequestEvidence,
    failure: RetryableAttempt<'_>,
) -> Result<uuid::Uuid, HttpResponse> {
    let RetryableAttempt {
        request_id,
        profile,
        attempted,
        retry_allowed,
        key_id,
        attempt,
        all_attempts_rate_limited,
    } = failure;
    if retry_allowed && let Some(next_key_id) = select_failover_key(state, profile, attempted).await
    {
        attempt_finished(state, request_id, key_id, attempt).await?;
        evidence.reset_drop_recovery();
        return Ok(next_key_id);
    }

    let (request, response) = if all_attempts_rate_limited {
        let mut response = openai_error(
            StatusCode::TOO_MANY_REQUESTS,
            "All NVIDIA upstream keys are rate limited",
            "rate_limit_error",
            "upstream_rate_limited",
        );
        if let Some(seconds) = retry_after_for_keys(&state.vault.list()) {
            response.headers_mut().insert(
                header::RETRY_AFTER,
                header::HeaderValue::from_str(&seconds.to_string())
                    .expect("cooldown seconds are a valid HTTP header value"),
            );
        }
        (
            RequestTerminal::failed(
                StatusCode::TOO_MANY_REQUESTS.as_u16(),
                "upstream_rate_limited",
            ),
            response,
        )
    } else {
        (
            RequestTerminal::failed(
                StatusCode::SERVICE_UNAVAILABLE.as_u16(),
                "no_eligible_upstream",
            ),
            openai_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "No eligible NVIDIA upstream is available.",
                "service_unavailable_error",
                "no_eligible_upstream",
            ),
        )
    };
    evidence
        .finish_with_attempt(key_id, attempt, request)
        .await?;
    Err(response)
}

/// Starts a provider attempt while keeping the parent request recoverable as
/// an explicit evidence failure. Both proxy entry points use this boundary so
/// an attempt-ledger INSERT failure cannot fall back to handler abandonment.
pub(crate) async fn start_evidenced_attempt(
    state: &web::Data<AppState>,
    evidence: &mut RequestEvidence,
    request_id: uuid::Uuid,
    profile: &str,
    key_id: uuid::Uuid,
) -> Result<(), HttpResponse> {
    evidence.arm_evidence_failure(None, 0);
    attempt_started(state, request_id, profile, key_id).await?;
    evidence.reset_drop_recovery();
    Ok(())
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
            return Err(openai_error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "model is not compatible with this endpoint",
                "invalid_request_error",
                "model_route_mismatch",
            ));
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
        return Err(openai_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "model is not compatible with this endpoint",
            "invalid_request_error",
            "model_route_mismatch",
        ));
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
        let chunk = chunk.map_err(|_| invalid_request("request body could not be read"))?;
        if body.len().saturating_add(chunk.len()) > limit {
            return Err(openai_error(
                StatusCode::PAYLOAD_TOO_LARGE,
                "request body exceeds the endpoint limit",
                "invalid_request_error",
                "request_too_large",
            ));
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
        "/v1/nvidia/inference" => model == "nvidia/vila",
        _ => false,
    }
}

fn retry_after_duration(response: &reqwest::Response) -> Option<Duration> {
    let value = response.headers().get("retry-after")?.to_str().ok()?.trim();
    if let Ok(seconds) = value.parse::<i64>() {
        return (seconds > 0).then(|| Duration::seconds(seconds.min(300)));
    }
    let deadline = httpdate::parse_http_date(value).ok()?;
    let remaining = deadline.duration_since(std::time::SystemTime::now()).ok()?;
    let seconds = i64::try_from(remaining.as_secs()).ok()?.clamp(1, 300);
    Some(Duration::seconds(seconds))
}

#[derive(Debug)]
struct RuntimeRoutingPolicy {
    retryable_statuses: Vec<u16>,
    default_cooldown: Duration,
}

async fn load_routing_policy(state: &AppState) -> RuntimeRoutingPolicy {
    let default = RuntimeRoutingPolicy {
        retryable_statuses: vec![402, 408, 429, 500, 502, 503, 504],
        default_cooldown: Duration::seconds(2),
    };
    let Some(pool) = &state.vault.database else {
        return default;
    };
    let document = match sqlx::query_scalar::<_, Value>(
        "SELECT document FROM nblb.routing_policies WHERE active=true ORDER BY version DESC LIMIT 1",
    )
    .fetch_optional(pool)
    .await
    {
        Ok(Some(document)) => document,
        _ => return default,
    };
    let retryable_statuses = document
        .get("retryable_statuses")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_u64)
                .filter_map(|value| u16::try_from(value).ok())
                .collect::<Vec<_>>()
        })
        .filter(|items| !items.is_empty())
        .unwrap_or(default.retryable_statuses);
    let seconds = document
        .get("default_cooldown_seconds")
        .and_then(Value::as_i64)
        .filter(|value| (1..=3600).contains(value))
        .unwrap_or(2);
    RuntimeRoutingPolicy {
        retryable_statuses,
        default_cooldown: Duration::seconds(seconds),
    }
}

fn retryable_error_class(status_code: u16) -> &'static str {
    match status_code {
        401 | 403 => "upstream_auth_error",
        402 => "upstream_quota_error",
        408 => "upstream_timeout",
        429 => "upstream_rate_limited",
        500..=599 => "upstream_server_error",
        _ => "upstream_failure",
    }
}

fn mock_status(request: &Value) -> Option<u16> {
    request
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("mock_status"))
        .and_then(Value::as_u64)
        .and_then(|status| u16::try_from(status).ok())
        .filter(|status| matches!(status, 400 | 401 | 403 | 429 | 500))
}

fn mock_response_kind(request: &Value) -> Option<&str> {
    request
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("mock_response"))
        .and_then(Value::as_str)
}

fn mock_stream_scenario(request: &Value) -> Option<&str> {
    request
        .get("metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("mock_stream_scenario"))
        .and_then(Value::as_str)
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
    openai_error(
        StatusCode::NOT_FOUND,
        "model is not advertised",
        "invalid_request_error",
        "model_not_found",
    )
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
            &[
                "model",
                "prompt",
                "image",
                "n",
                "size",
                "response_format",
                "steps",
                "cfg_scale",
                "seed",
                "user",
            ],
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
        "/v1/nvidia/inference" => (
            &["messages"],
            &[
                "model",
                "messages",
                "temperature",
                "top_p",
                "max_tokens",
                "seed",
            ],
        ),
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
            if let Some(image) = object.get("image")
                && image
                    .as_str()
                    .is_none_or(|value| !valid_data_url(value, true))
            {
                return Err(invalid_request(
                    "image must be a valid PNG or JPEG data URL",
                ));
            }
            if let Some(steps) = object.get("steps")
                && steps
                    .as_u64()
                    .is_none_or(|value| !(1..=50).contains(&value))
            {
                return Err(invalid_request("steps must be an integer from 1 to 50"));
            }
            if let Some(cfg_scale) = object.get("cfg_scale")
                && cfg_scale
                    .as_f64()
                    .is_none_or(|value| !(0.0..=20.0).contains(&value))
            {
                return Err(invalid_request("cfg_scale must be from 0 to 20"));
            }
            if let Some(seed) = object.get("seed")
                && seed.as_u64().is_none()
            {
                return Err(invalid_request("seed must be a non-negative integer"));
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
        "/v1/nvidia/inference" => validate_chat_request(&Value::Object(object.clone())),
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
        return Err(openai_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "model is not compatible with this endpoint",
            "invalid_request_error",
            "model_route_mismatch",
        ));
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

#[cfg(test)]
mod response_body_tests {
    use super::{ResponseBodyError, bounded_response_bytes};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    async fn response(raw: &'static [u8]) -> reqwest::Response {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind response fixture");
        let address = listener.local_addr().expect("fixture address");
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("accept fixture request");
            let mut request = [0_u8; 1024];
            let _ = stream
                .read(&mut request)
                .await
                .expect("read fixture request");
            stream.write_all(raw).await.expect("write fixture response");
            stream.shutdown().await.expect("close fixture response");
        });
        reqwest::get(format!("http://{address}/"))
            .await
            .expect("request response fixture")
    }

    #[tokio::test]
    async fn bounded_collector_rejects_oversized_content_length_before_body_read() {
        let response = response(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\nhello world").await;
        assert_eq!(
            bounded_response_bytes(response, 10).await,
            Err(ResponseBodyError::TooLarge)
        );
    }

    #[tokio::test]
    async fn bounded_collector_rejects_oversized_chunked_body() {
        let response = response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n6\r\nhello \r\n6\r\nworld!\r\n0\r\n\r\n",
        )
        .await;
        assert_eq!(
            bounded_response_bytes(response, 10).await,
            Err(ResponseBodyError::TooLarge)
        );
    }
}
