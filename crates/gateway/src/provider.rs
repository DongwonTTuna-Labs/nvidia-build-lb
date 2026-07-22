use actix_web::HttpResponse;
use serde_json::{Value, json};
use std::env;

use super::{SseValidator, invalid_request};

pub(crate) fn upstream_endpoint(configured: &str, path: &str) -> String {
    let configured = configured.trim_end_matches('/');
    let versionless_path = path.strip_prefix("/v1").unwrap_or(path);
    if let Some(prefix) = configured.strip_suffix("/chat/completions") {
        return format!("{prefix}{versionless_path}");
    }
    if configured.ends_with("/v1") {
        return format!("{configured}{versionless_path}");
    }
    format!("{configured}{path}")
}

pub(crate) fn upstream_endpoint_for(configured: &str, path: &str, model: &str) -> String {
    if configured.starts_with("mock://") {
        return configured.to_owned();
    }
    let configured = configured.trim_end_matches('/');
    let is_default_nvidia = matches!(
        configured,
        "https://integrate.api.nvidia.com/v1/chat/completions"
            | "https://integrate.api.nvidia.com/v1"
    );
    if is_default_nvidia {
        return match model {
            "nvidia/vila" => "https://ai.api.nvidia.com/v1/vlm/nvidia/vila".to_owned(),
            "black-forest-labs/flux.1-kontext-dev" => {
                "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-kontext-dev".to_owned()
            }
            "stabilityai/stable-video-diffusion" => {
                "https://ai.api.nvidia.com/v1/genai/stabilityai/stable-video-diffusion".to_owned()
            }
            "microsoft/phi-4-multimodal-instruct" => {
                "https://integrate.api.nvidia.com/v1/chat/completions".to_owned()
            }
            "nvidia/nvclip" => "https://integrate.api.nvidia.com/v1/embeddings".to_owned(),
            "nvidia/magpie-tts-multilingual" => magpie_tts_endpoint(),
            "nvidia/parakeet-ctc-1.1b" => upstream_endpoint(configured, "/v1/audio/transcriptions"),
            _ => upstream_endpoint(configured, path),
        };
    }
    upstream_endpoint(configured, path)
}

fn magpie_tts_endpoint() -> String {
    env::var("NBLB_MAGPIE_TTS_ENDPOINT").unwrap_or_else(|_| {
        "https://877104f7-e885-42b9-8de8-f6e4c6303969.invocation.api.nvcf.nvidia.com/v1/audio/synthesize".to_owned()
    })
}

pub(crate) fn prepare_modality_request(path: &str, request: &Value) -> Result<Value, HttpResponse> {
    let object = request
        .as_object()
        .ok_or_else(|| invalid_request("request body must be a JSON object"))?;
    match path {
        "/v1/images/generations" => {
            let prompt = object
                .get("prompt")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_request("prompt must be a string"))?;
            let size = object
                .get("size")
                .and_then(Value::as_str)
                .unwrap_or("1024x1024");
            let image = object
                .get("image")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let aspect_ratio = if image.is_empty() {
                match size {
                    "1024x1024" => "1:1",
                    "1792x1024" | "1536x864" => "16:9",
                    "1024x1792" | "864x1536" => "9:16",
                    _ => return Err(invalid_request("size is not supported by FLUX")),
                }
            } else {
                "match_input_image"
            };
            if object.get("n").and_then(Value::as_u64).unwrap_or(1) != 1 {
                return Err(invalid_request("FLUX supports exactly one image"));
            }
            Ok(json!({
                "prompt": prompt,
                "image": image,
                "aspect_ratio": aspect_ratio,
                "steps": object.get("steps").and_then(Value::as_u64).unwrap_or(30),
                "cfg_scale": object.get("cfg_scale").and_then(Value::as_f64).unwrap_or(3.5),
                "seed": object.get("seed").and_then(Value::as_u64).unwrap_or(0)
            }))
        }
        "/v1/videos/generations" => {
            let input = object
                .get("input_reference")
                .and_then(Value::as_str)
                .filter(|value| {
                    value.starts_with("data:image/png;base64,")
                        || value.starts_with("data:image/jpeg;base64,")
                })
                .ok_or_else(|| invalid_request("input_reference must be a PNG or JPEG data URL"))?;
            let mut result = serde_json::Map::new();
            result.insert("image".to_owned(), Value::String(input.to_owned()));
            result.insert(
                "seed".to_owned(),
                object.get("seed").cloned().unwrap_or_else(|| json!(0)),
            );
            result.insert(
                "cfg_scale".to_owned(),
                object
                    .get("cfg_scale")
                    .cloned()
                    .unwrap_or_else(|| json!(1.8)),
            );
            result.insert(
                "motion_bucket_id".to_owned(),
                object
                    .get("motion_bucket_id")
                    .cloned()
                    .unwrap_or_else(|| json!(127)),
            );
            Ok(Value::Object(result))
        }
        "/v1/nvidia/inference" => {
            // The hosted VILA endpoint is an endpoint-specific OpenAI chat
            // contract. The public alias carries `model` for routing, while
            // the provider endpoint already identifies the model.
            let mut result = object.clone();
            result.remove("model");
            Ok(Value::Object(result))
        }
        _ => Ok(request.clone()),
    }
}

pub(crate) fn validate_chat_response(body: &[u8], stream: bool) -> Result<(), ()> {
    if stream {
        let mut validator = SseValidator::default();
        validator.feed(body)?;
        return validator.finish();
    }
    let value: Value = serde_json::from_slice(body).map_err(|_| ())?;
    let object = value.as_object().ok_or(())?;
    if object.keys().any(|key| {
        !matches!(
            key.as_str(),
            "id" | "object"
                | "created"
                | "model"
                | "choices"
                | "usage"
                | "system_fingerprint"
                | "service_tier"
                | "prompt_filter_results"
                | "reasoning"
                | "reasoning_content"
        )
    }) || object.get("object").and_then(Value::as_str) != Some("chat.completion")
        || object
            .get("id")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        || object
            .get("model")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return Err(());
    }
    if let Some(created) = object.get("created")
        && created.as_i64().is_none_or(|value| value < 0)
    {
        return Err(());
    }
    let choices = object.get("choices").and_then(Value::as_array).ok_or(())?;
    if choices.is_empty() {
        return Err(());
    }
    for choice in choices {
        let choice = choice.as_object().ok_or(())?;
        if choice.keys().any(|key| {
            !matches!(
                key.as_str(),
                "index" | "message" | "finish_reason" | "logprobs" | "content_filter_results"
            )
        }) || choice.get("index").and_then(Value::as_u64).is_none()
            || choice
                .get("finish_reason")
                .is_none_or(|value| !value.is_null() && !value.is_string())
        {
            return Err(());
        }
        let message = choice.get("message").and_then(Value::as_object).ok_or(())?;
        if message.keys().any(|key| {
            !matches!(
                key.as_str(),
                "role"
                    | "content"
                    | "tool_calls"
                    | "function_call"
                    | "refusal"
                    | "audio"
                    | "annotations"
            )
        }) || message
            .get("role")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
            || message
                .get("content")
                .is_some_and(|value| !value.is_string() && !value.is_null())
            || message
                .get("refusal")
                .is_some_and(|value| !value.is_string() && !value.is_null())
            || message
                .get("tool_calls")
                .is_some_and(|value| !value.is_array())
            || message
                .get("annotations")
                .is_some_and(|value| !value.is_array())
        {
            return Err(());
        }
    }
    if let Some(usage) = object.get("usage") {
        let usage = usage.as_object().ok_or(())?;
        if usage.keys().any(|key| {
            !matches!(
                key.as_str(),
                "prompt_tokens"
                    | "completion_tokens"
                    | "total_tokens"
                    | "prompt_tokens_details"
                    | "completion_tokens_details"
            )
        }) || usage.iter().any(|(key, value)| {
            if matches!(
                key.as_str(),
                "prompt_tokens" | "completion_tokens" | "total_tokens"
            ) {
                value.as_u64().is_none()
            } else {
                !value.is_object() && !value.is_null()
            }
        }) {
            return Err(());
        }
    }
    Ok(())
}
