use actix_web::web;
use base64::Engine;
use bytes::Bytes;
use chrono::Utc;
use futures_util::{Stream, StreamExt, stream};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use uuid::Uuid;

use super::{AppState, record_failure, record_request};

pub(crate) fn sse_error_frame(message: &str) -> Bytes {
    // Once an SSE response has started, an HTTP status cannot be changed. A
    // bounded error event followed by the normal terminator prevents clients
    // from waiting forever without reflecting upstream headers or secrets.
    Bytes::from(format!(
        "event: error\ndata: {}\n\ndata: [DONE]\n\n",
        json!({"error":{"message":message,"type":"upstream_stream_error"}})
    ))
}

pub(crate) type UpstreamByteStream =
    Pin<Box<dyn Stream<Item = Result<Bytes, reqwest::Error>> + Send>>;

pub(crate) async fn prime_stream(
    mut upstream: UpstreamByteStream,
) -> Result<(UpstreamByteStream, SseValidator, VecDeque<Bytes>), ()> {
    let mut validator = SseValidator::default();
    let mut prefix = VecDeque::new();
    loop {
        match upstream.next().await {
            Some(Ok(chunk)) => {
                validator.feed(&chunk)?;
                prefix.extend(validator.take_emitted());
                // A bare [DONE] is not a usable completion.  Do not hand the
                // response to Actix until at least one validated data chunk
                // exists, otherwise failover is lost after a premature
                // provider terminator.
                if validator.data_frame_count > 0 {
                    return Ok((upstream, validator, prefix));
                }
                if prefix.len() > 256 {
                    return Err(());
                }
            }
            Some(Err(_)) | None => return Err(()),
        }
    }
}

pub(crate) struct StreamAttemptGuard {
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    terminal: Arc<AtomicBool>,
}

impl StreamAttemptGuard {
    pub(crate) fn new(state: web::Data<AppState>, request_id: Uuid, key_id: Uuid) -> Self {
        Self {
            state,
            request_id,
            key_id,
            terminal: Arc::new(AtomicBool::new(false)),
        }
    }

    pub(crate) fn mark_terminal(&self) {
        self.terminal.store(true, Ordering::Release);
    }
}

impl Drop for StreamAttemptGuard {
    fn drop(&mut self) {
        if self.terminal.swap(true, Ordering::AcqRel) {
            return;
        }
        let state = self.state.clone();
        let request_id = self.request_id;
        let key_id = self.key_id;
        // Actix drops the body stream on a client disconnect. The durable
        // terminal update is therefore scheduled from Drop so a started row
        // cannot survive an abandoned downstream connection.
        tokio::spawn(async move {
            if let Err(error) = state
                .vault
                .attempt_finished(request_id, key_id, "cancelled")
                .await
            {
                eprintln!("stream cancellation ledger update failed: {error:#}");
            }
        });
    }
}

pub(crate) async fn finish_stream_failure(
    state: &web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
) {
    if record_failure(state, key_id, None).await.is_err() {
        eprintln!("stream failure health update failed");
    }
    if let Err(error) = state
        .vault
        .attempt_finished(request_id, key_id, "failed")
        .await
    {
        eprintln!("stream failure ledger update failed: {error:#}");
    }
}

#[derive(Default)]
pub(crate) struct SseValidator {
    buffer: Vec<u8>,
    emitted: VecDeque<Bytes>,
    done: bool,
    pub(crate) frame_count: usize,
    pub(crate) data_frame_count: usize,
    message_id: Option<String>,
    model: Option<String>,
}

impl SseValidator {
    pub(crate) fn take_emitted(&mut self) -> VecDeque<Bytes> {
        std::mem::take(&mut self.emitted)
    }

    pub(crate) fn feed(&mut self, bytes: &[u8]) -> Result<(), ()> {
        if self.buffer.len().saturating_add(bytes.len()) > 256 * 1024 * 1024 {
            return Err(());
        }
        self.buffer.extend_from_slice(bytes);
        loop {
            let lf = self.buffer.windows(2).position(|pair| pair == b"\n\n");
            let crlf = self.buffer.windows(4).position(|pair| pair == b"\r\n\r\n");
            let (index, delimiter_len) = match (lf, crlf) {
                (Some(lf), Some(crlf)) if crlf < lf => (crlf, 4),
                (Some(lf), _) => (lf, 2),
                (None, Some(crlf)) => (crlf, 4),
                (None, None) => break,
            };
            let frame = self
                .buffer
                .drain(..index + delimiter_len)
                .collect::<Vec<_>>();
            self.validate_frame(&frame[..frame.len() - delimiter_len])?;
        }
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> Result<(), ()> {
        if !self.buffer.iter().all(u8::is_ascii_whitespace) {
            return Err(());
        }
        if self.done && self.data_frame_count >= 1 {
            Ok(())
        } else {
            Err(())
        }
    }

    fn validate_frame(&mut self, frame: &[u8]) -> Result<(), ()> {
        if frame.len() > 1024 * 1024 {
            return Err(());
        }
        let frame = std::str::from_utf8(frame).map_err(|_| ())?;
        if frame.is_empty() {
            return Ok(());
        }
        if self.done {
            return Err(());
        }
        let mut data_lines = Vec::new();
        for line in frame.lines() {
            let line = line.strip_suffix('\r').unwrap_or(line);
            if line.is_empty() || line.starts_with(':') {
                continue;
            }
            if let Some(value) = line.strip_prefix("data:") {
                data_lines.push(value.strip_prefix(' ').unwrap_or(value));
                continue;
            }
            if let Some(value) = line.strip_prefix("event:") {
                if value.trim().len() > 64
                    || !value
                        .trim()
                        .bytes()
                        .all(|byte| (0x20..=0x7e).contains(&byte))
                {
                    return Err(());
                }
                continue;
            }
            if let Some(value) = line.strip_prefix("id:") {
                if value.trim().len() > 128 || value.contains('\0') {
                    return Err(());
                }
                continue;
            }
            if let Some(value) = line.strip_prefix("retry:") {
                if value.trim().is_empty()
                    || value.trim().len() > 6
                    || !value.trim().bytes().all(|byte| byte.is_ascii_digit())
                {
                    return Err(());
                }
                continue;
            }
            return Err(());
        }
        if data_lines.is_empty() {
            return Ok(());
        }
        let data = data_lines.join("\n");
        self.frame_count = self.frame_count.saturating_add(1);
        if data.trim() == "[DONE]" {
            self.done = true;
            return Ok(());
        }
        self.data_frame_count = self.data_frame_count.saturating_add(1);
        let value: Value = serde_json::from_str(&data).map_err(|_| ())?;
        let object = value.as_object().ok_or(())?;
        if object.keys().any(|key| {
            !matches!(
                key.as_str(),
                "id" | "object"
                    | "model"
                    | "choices"
                    | "created"
                    | "system_fingerprint"
                    | "service_tier"
                    | "usage"
                    | "reasoning"
                    | "reasoning_content"
            )
        }) {
            return Err(());
        }
        let id = value.get("id").and_then(Value::as_str).ok_or(())?;
        let model = value.get("model").and_then(Value::as_str).ok_or(())?;
        if value.get("object").and_then(Value::as_str) != Some("chat.completion.chunk")
            || id.is_empty()
            || model.is_empty()
            || value.get("choices").and_then(Value::as_array).is_none()
        {
            return Err(());
        }
        if self
            .message_id
            .as_deref()
            .is_some_and(|previous| previous != id)
            || self
                .model
                .as_deref()
                .is_some_and(|previous| previous != model)
        {
            return Err(());
        }
        self.message_id = Some(id.to_owned());
        self.model = Some(model.to_owned());
        let choices = value.get("choices").and_then(Value::as_array).ok_or(())?;
        if choices.is_empty() {
            return Err(());
        }
        if choices.iter().any(|choice| {
            !choice.is_object()
                || choice.as_object().is_some_and(|choice| {
                    choice.keys().any(|key| {
                        !matches!(
                            key.as_str(),
                            "index"
                                | "delta"
                                | "finish_reason"
                                | "logprobs"
                                | "content_filter_results"
                        )
                    })
                })
                || choice.get("index").and_then(Value::as_u64).is_none()
                || choice
                    .get("delta")
                    .and_then(Value::as_object)
                    .is_none_or(|delta| {
                        delta.keys().any(|key| {
                            !matches!(
                                key.as_str(),
                                "role"
                                    | "content"
                                    | "tool_calls"
                                    | "function_call"
                                    | "refusal"
                                    | "audio"
                            )
                        })
                    })
                || !choice
                    .get("finish_reason")
                    .is_some_and(|reason| reason.is_null() || reason.is_string())
        }) {
            return Err(());
        }
        self.emitted
            .push_back(Bytes::from(format!("data: {data}\n\n")));
        Ok(())
    }
}

pub(crate) fn chat_response_stream(
    upstream: UpstreamByteStream,
    validator: SseValidator,
    prefix: VecDeque<Bytes>,
    state: web::Data<AppState>,
    request_id: Uuid,
    key_id: Uuid,
    guard: StreamAttemptGuard,
) -> impl Stream<Item = Result<Bytes, actix_web::Error>> {
    stream::unfold(
        (
            upstream, validator, prefix, state, request_id, key_id, guard, false,
        ),
        |(
            mut upstream,
            mut validator,
            mut prefix,
            state,
            request_id,
            key_id,
            guard,
            mut terminal,
        )| async move {
            if terminal {
                return None;
            }
            loop {
                if let Some(chunk) = prefix.pop_front() {
                    return Some((
                        Ok(chunk),
                        (
                            upstream, validator, prefix, state, request_id, key_id, guard, terminal,
                        ),
                    ));
                }
                match upstream.next().await {
                    Some(Ok(chunk)) => {
                        if validator.feed(&chunk).is_err() {
                            finish_stream_failure(&state, request_id, key_id).await;
                            terminal = true;
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("invalid upstream stream")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    terminal,
                                ),
                            ));
                        }
                        prefix.extend(validator.take_emitted());
                        continue;
                    }
                    Some(Err(_)) => {
                        finish_stream_failure(&state, request_id, key_id).await;
                        terminal = true;
                        guard.terminal.store(true, Ordering::Release);
                        return Some((
                            Ok(sse_error_frame("upstream stream failed")),
                            (
                                upstream, validator, prefix, state, request_id, key_id, guard,
                                terminal,
                            ),
                        ));
                    }
                    None => {
                        if validator.finish().is_err() {
                            finish_stream_failure(&state, request_id, key_id).await;
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("incomplete upstream stream")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        if record_request(&state, key_id).await.is_err() {
                            if let Err(error) = state
                                .vault
                                .attempt_finished(request_id, key_id, "failed")
                                .await
                            {
                                eprintln!("stream accounting ledger update failed: {error:#}");
                            }
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("request accounting unavailable")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        if let Err(error) = state
                            .vault
                            .attempt_finished(request_id, key_id, "succeeded")
                            .await
                        {
                            eprintln!("stream success ledger update failed: {error:#}");
                            guard.terminal.store(true, Ordering::Release);
                            return Some((
                                Ok(sse_error_frame("request ledger unavailable")),
                                (
                                    upstream, validator, prefix, state, request_id, key_id, guard,
                                    true,
                                ),
                            ));
                        }
                        guard.terminal.store(true, Ordering::Release);
                        // Do not expose the provider's success terminator until
                        // the durable request and attempt ledger commits have
                        // succeeded.  A client must never observe `[DONE]` for
                        // a request the gateway recorded as failed.
                        return Some((
                            Ok(Bytes::from_static(b"data: [DONE]\n\n")),
                            (
                                upstream, validator, prefix, state, request_id, key_id, guard, true,
                            ),
                        ));
                    }
                }
            }
        },
    )
}

pub(crate) fn decode_base64(value: &str) -> Option<Vec<u8>> {
    if value.is_empty() || value.bytes().any(|byte| byte.is_ascii_whitespace()) {
        return None;
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value.as_bytes())
        .ok()?;
    (base64::engine::general_purpose::STANDARD.encode(&bytes) == value).then_some(bytes)
}

pub(crate) fn valid_jpeg(bytes: &[u8]) -> bool {
    if bytes.len() < 16 || !bytes.starts_with(&[0xff, 0xd8]) || !bytes.ends_with(&[0xff, 0xd9]) {
        return false;
    }
    let mut index = 2;
    let mut has_frame = false;
    while index + 3 < bytes.len().saturating_sub(2) {
        if bytes[index] != 0xff {
            index += 1;
            continue;
        }
        while index < bytes.len() && bytes[index] == 0xff {
            index += 1;
        }
        if index >= bytes.len() {
            break;
        }
        let marker = bytes[index];
        index += 1;
        if marker == 0xd9 || marker == 0xda {
            break;
        }
        if marker == 0xd8 || marker == 0x01 || (0xd0..=0xd7).contains(&marker) {
            continue;
        }
        if index + 2 > bytes.len() {
            return false;
        }
        let segment_len = u16::from_be_bytes([bytes[index], bytes[index + 1]]) as usize;
        if segment_len < 2 || index + segment_len > bytes.len() {
            return false;
        }
        if (0xc0..=0xc3).contains(&marker) {
            if segment_len < 7 {
                return false;
            }
            let height = u16::from_be_bytes([bytes[index + 3], bytes[index + 4]]);
            let width = u16::from_be_bytes([bytes[index + 5], bytes[index + 6]]);
            has_frame = width > 0 && height > 0;
        }
        index += segment_len;
    }
    has_frame
}

pub(crate) fn valid_image(bytes: &[u8]) -> bool {
    valid_jpeg(bytes) || valid_png(bytes)
}

pub(crate) fn valid_png(bytes: &[u8]) -> bool {
    bytes.len() >= 24
        && bytes.starts_with(b"\x89PNG\r\n\x1a\n")
        && bytes.windows(4).any(|chunk| chunk == b"IEND")
}

pub(crate) fn valid_mp4(bytes: &[u8]) -> bool {
    if bytes.len() < 16 || &bytes[4..8] != b"ftyp" {
        return false;
    }
    let size = u32::from_be_bytes(bytes[0..4].try_into().unwrap_or_default()) as usize;
    size >= 16
        && size <= bytes.len()
        && bytes[8..size].windows(4).any(|brand| brand == b"isom")
        && bytes.windows(4).any(|atom| atom == b"moov")
}

pub(crate) fn valid_wav(bytes: &[u8]) -> bool {
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return false;
    }
    let fmt_size = u32::from_le_bytes(bytes[16..20].try_into().unwrap_or_default()) as usize;
    let channels = u16::from_le_bytes(bytes[22..24].try_into().unwrap_or_default());
    let sample_rate = u32::from_le_bytes(bytes[24..28].try_into().unwrap_or_default());
    let bits = u16::from_le_bytes(bytes[34..36].try_into().unwrap_or_default());
    let riff_size = u32::from_le_bytes(bytes[4..8].try_into().unwrap_or_default()) as usize;
    let Some(data_offset) = bytes.windows(4).position(|chunk| chunk == b"data") else {
        return false;
    };
    if data_offset + 8 > bytes.len() {
        return false;
    }
    let data_size = u32::from_le_bytes(
        bytes[data_offset + 4..data_offset + 8]
            .try_into()
            .unwrap_or_default(),
    ) as usize;
    fmt_size >= 16
        && channels == 1
        && sample_rate == 44_100
        && bits == 16
        && riff_size + 8 == bytes.len()
        && data_size > 0
        && data_offset + 8 + data_size == bytes.len()
}

pub(crate) fn valid_data_url(value: &str, image_only: bool) -> bool {
    let Some((prefix, encoded)) = value.split_once(",") else {
        return false;
    };
    let Some(bytes) = decode_base64(encoded) else {
        return false;
    };
    match (prefix, image_only) {
        ("data:image/png;base64", true) => valid_png(&bytes),
        ("data:image/jpeg;base64", true) => valid_jpeg(&bytes),
        _ => false,
    }
}

fn validate_modality_response(path: &str, body: &[u8], content_type: &str) -> Result<(), ()> {
    let media_type = content_type.split(';').next().unwrap_or_default().trim();
    if path == "/v1/audio/speech" && matches!(media_type, "audio/wav" | "audio/x-wav") {
        return valid_wav(body).then_some(()).ok_or(());
    }
    let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
    let valid = match path {
        "/v1/embeddings" => {
            value.as_object().is_some_and(|object| {
                object
                    .keys()
                    .all(|key| matches!(key.as_str(), "object" | "data" | "model" | "usage"))
            }) && value.get("object").and_then(Value::as_str) == Some("list")
                && value.get("model").and_then(Value::as_str) == Some("nvidia/nvclip")
                && value
                    .get("data")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().enumerate().all(|(index, item)| {
                                item.get("object").and_then(Value::as_str) == Some("embedding")
                                    && item.get("index").and_then(Value::as_u64)
                                        == Some(index as u64)
                                    && item.get("embedding").and_then(Value::as_array).is_some_and(
                                        |vector| {
                                            vector.len() == 1024
                                                && vector.iter().all(|value| {
                                                    value.as_f64().is_some_and(f64::is_finite)
                                                })
                                        },
                                    )
                            })
                    })
                && value
                    .get("usage")
                    .and_then(Value::as_object)
                    .is_some_and(|usage| {
                        usage.contains_key("prompt_tokens")
                            && usage.contains_key("total_tokens")
                            && usage
                                .keys()
                                .all(|key| matches!(key.as_str(), "prompt_tokens" | "total_tokens"))
                            && usage.values().all(|value| value.as_u64().is_some())
                    })
        }
        "/v1/images/generations" => {
            value
                .get("data")
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    !items.is_empty()
                        && items.iter().all(|item| {
                            item.get("b64_json")
                                .and_then(Value::as_str)
                                .and_then(decode_base64)
                                .is_some_and(|bytes| valid_image(&bytes))
                        })
                })
                || value
                    .get("artifacts")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().all(|item| {
                                item.get("finishReason").and_then(Value::as_str) == Some("SUCCESS")
                                    && item.get("seed").and_then(Value::as_i64).is_some()
                                    && item
                                        .get("base64")
                                        .and_then(Value::as_str)
                                        .and_then(decode_base64)
                                        .is_some_and(|bytes| valid_image(&bytes))
                            })
                    })
        }
        "/v1/audio/speech" => value
            .get("audio")
            .or_else(|| value.get("data"))
            .and_then(Value::as_str)
            .and_then(decode_base64)
            .is_some_and(|bytes| valid_wav(&bytes)),
        "/v1/audio/transcriptions" => value
            .get("text")
            .and_then(Value::as_str)
            .is_some_and(|text| !text.trim().is_empty()),
        "/v1/videos/generations" => {
            (value
                .get("video")
                .and_then(Value::as_str)
                .and_then(decode_base64)
                .is_some_and(|bytes| valid_mp4(&bytes))
                && value.get("finish_reason").and_then(Value::as_str) == Some("SUCCESS")
                && value.get("seed").and_then(Value::as_i64).is_some())
                || value
                    .get("data")
                    .and_then(Value::as_array)
                    .is_some_and(|items| {
                        !items.is_empty()
                            && items.iter().all(|item| {
                                item.get("b64_json")
                                    .and_then(Value::as_str)
                                    .and_then(decode_base64)
                                    .is_some_and(|bytes| valid_mp4(&bytes))
                            })
                    })
        }
        "/v1/nvidia/inference" => {
            value
                .get("video")
                .and_then(Value::as_str)
                .and_then(decode_base64)
                .is_some_and(|bytes| valid_mp4(&bytes))
                && value.get("finish_reason").and_then(Value::as_str) == Some("SUCCESS")
                && value.get("seed").and_then(Value::as_i64).is_some()
        }
        _ => value.is_object(),
    };
    valid.then_some(()).ok_or(())
}

pub(crate) fn normalize_modality_response(
    path: &str,
    body: &[u8],
    content_type: &str,
) -> Result<(Vec<u8>, String), ()> {
    validate_modality_response(path, body, content_type)?;
    if path == "/v1/images/generations" {
        let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
        if value.get("data").is_some() {
            return Ok((body.to_vec(), "application/json".to_owned()));
        }
        let artifacts = value.get("artifacts").and_then(Value::as_array).ok_or(())?;
        let data = artifacts
            .iter()
            .map(|item| {
                let encoded = item.get("base64").and_then(Value::as_str).ok_or(())?;
                if encoded.is_empty() {
                    return Err(());
                }
                Ok(json!({"b64_json": encoded}))
            })
            .collect::<Result<Vec<_>, ()>>()?;
        let normalized = json!({"created": Utc::now().timestamp(), "data": data});
        return serde_json::to_vec(&normalized)
            .map(|bytes| (bytes, "application/json".to_owned()))
            .map_err(|_| ());
    }
    if path == "/v1/videos/generations" {
        let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
        if value.get("data").is_some() {
            return Ok((body.to_vec(), "application/json".to_owned()));
        }
        if let Some(video) = value.get("video").and_then(Value::as_str) {
            let normalized = json!({"data":[{"b64_json":video}],"model":value.get("model")});
            return serde_json::to_vec(&normalized)
                .map(|bytes| (bytes, "application/json".to_owned()))
                .map_err(|_| ());
        }
    }
    Ok((body.to_vec(), content_type.to_owned()))
}
