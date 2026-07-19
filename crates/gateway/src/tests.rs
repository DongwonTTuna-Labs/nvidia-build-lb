use super::admin::{PageCursor, PageQuery, encode_page_cursor, page_before};
use super::provider::upstream_endpoint;
use super::proxy::{parse_multimodal_request, validate_chat_request};
use super::{
    SseValidator, admin_host_allowed, bearer, eligible_key_count, format_origin_host,
    host_authority_well_formed, inline_script_bodies, percent_encode_userinfo,
    should_migrate_file_vault, upstream_endpoint_for, validate_admin_token, validate_chat_response,
};
use actix_web::http::StatusCode;
use actix_web::http::header;
use actix_web::test::TestRequest;
use chrono::{Duration, Utc};
use nvidia_build_lb_core::KeySummary;
use uuid::Uuid;

#[test]
fn page_cursor_is_explicit_and_fail_closed() {
    let id = Uuid::new_v4();
    let query = PageQuery {
        before: Some(
            encode_page_cursor(&PageCursor {
                created_at: "2026-07-19T12:34:56Z".parse().expect("timestamp"),
                id,
            })
            .expect("encode cursor"),
        ),
        limit: Some(10),
    };
    let parsed = page_before(&query)
        .expect("valid cursor")
        .expect("cursor value");
    assert_eq!(parsed.created_at.to_rfc3339(), "2026-07-19T12:34:56+00:00");
    assert_eq!(parsed.id, id);
    let invalid = PageQuery {
        before: Some("not-a-timestamp".to_owned()),
        limit: None,
    };
    assert_eq!(
        page_before(&invalid).unwrap_err().status(),
        StatusCode::BAD_REQUEST
    );
}

#[test]
fn modality_paths_replace_only_the_endpoint_suffix() {
    let base = "https://integrate.api.nvidia.com/v1/chat/completions";
    assert_eq!(
        upstream_endpoint(base, "/v1/embeddings"),
        "https://integrate.api.nvidia.com/v1/embeddings"
    );
    assert_eq!(
        upstream_endpoint(base, "/v1/audio/transcriptions"),
        "https://integrate.api.nvidia.com/v1/audio/transcriptions"
    );
    assert_eq!(
        upstream_endpoint("https://provider.example/v1", "/v1/images/generations"),
        "https://provider.example/v1/images/generations"
    );
    assert_eq!(
        upstream_endpoint_for(base, "/v1/nvidia/inference", "nvidia/vila"),
        "https://ai.api.nvidia.com/v1/vlm/nvidia/vila"
    );
    assert_eq!(
        upstream_endpoint_for(
            base,
            "/v1/images/generations",
            "black-forest-labs/flux.1-kontext-dev"
        ),
        "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-kontext-dev"
    );
    assert_eq!(
        upstream_endpoint_for(
            "https://provider.example/v1/chat/completions",
            "/v1/images/generations",
            "black-forest-labs/flux.1-kontext-dev",
        ),
        "https://provider.example/v1/images/generations"
    );
}

#[test]
fn modality_and_chat_boundaries_reject_malformed_requests() {
    assert!(parse_multimodal_request("/v1/embeddings", b"{not-json", "application/json").is_err());
    assert!(
        parse_multimodal_request(
            "/v1/embeddings",
            br#"{"model":"nvidia/nvclip","input":"hello"}"#,
            "application/json",
        )
        .is_ok()
    );
    assert!(
        validate_chat_request(&serde_json::json!({
            "model": "z-ai/glm-5.2",
            "messages": [{"role":"user","content":"hello"}]
        }))
        .is_ok()
    );
    assert!(validate_chat_request(&serde_json::json!({"model":"z-ai/glm-5.2"})).is_err());
    let png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    assert!(
        parse_multimodal_request(
            "/v1/videos/generations",
            &serde_json::to_vec(&serde_json::json!({
                "model":"stabilityai/stable-video-diffusion",
                "input_reference": png
            }))
            .expect("video request"),
            "application/json",
        )
        .is_ok()
    );
    assert!(parse_multimodal_request(
            "/v1/videos/generations",
            br#"{"model":"stabilityai/stable-video-diffusion","input_reference":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=","motion_bucket_id":126}"#,
            "application/json",
        )
        .is_err());
    assert!(parse_multimodal_request(
            "/v1/nvidia/inference",
            br#"{"model":"nvidia/vila","input":{"image":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}"#,
            "application/json",
        )
        .is_err());
}

#[test]
fn multipart_transcription_requires_model_and_file() {
    let body = b"--test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\naudio\r\n--test--\r\n";
    let parsed = parse_multimodal_request(
        "/v1/audio/transcriptions",
        body,
        "multipart/form-data; boundary=test",
    )
    .expect("valid transcription multipart");
    assert_eq!(parsed["model"], "nvidia/parakeet-ctc-1.1b");
    assert_eq!(parsed["__nblb_multipart"], true);
    assert!(
            parse_multimodal_request(
                "/v1/audio/transcriptions",
                b"--test\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--test--\r\n",
                "multipart/form-data; boundary=test",
            )
            .is_err()
        );
    assert!(
        parse_multimodal_request(
            "/v1/audio/transcriptions",
            b"--test--",
            "multipart/form-data; boundary=test",
        )
        .is_err()
    );
}

#[test]
fn chat_response_contract_rejects_provider_extras() {
    let valid = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}"#;
    assert!(validate_chat_response(valid, false).is_ok());
    let tool_call = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","system_fingerprint":"fp","service_tier":"default","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}],"refusal":null},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"completion_tokens_details":{"reasoning_tokens":0}}}"#;
    assert!(validate_chat_response(tool_call, false).is_ok());
    let leaked = br#"{"id":"chat-1","object":"chat.completion","model":"z-ai/glm-5.2","choices":[],"provider_secret":"do-not-forward"}"#;
    assert!(validate_chat_response(leaked, false).is_err());
}

#[test]
fn admin_host_boundary_handles_ipv6_and_header_confusion() {
    assert!(admin_host_allowed("[::1]:2456"));
    assert!(admin_host_allowed("::1"));
    assert_eq!(format_origin_host("::1"), "[::1]");
    assert_eq!(format_origin_host("127.0.0.1"), "127.0.0.1");
    assert!(!admin_host_allowed("127.0.0.1:2456,evil"));
    assert!(!admin_host_allowed("[::1]evil"));
    assert!(!admin_host_allowed("[::1]:not-a-port"));
    assert!(!host_authority_well_formed("[::1]evil"));
    assert!(!host_authority_well_formed(
        "[nvidia-lb.dongwontuna.net]evil"
    ));
    assert!(!host_authority_well_formed("localhost:not-a-port"));
    assert!(!admin_host_allowed("localhost:not-a-port"));
    assert!(host_authority_well_formed("localhost:2456"));
    assert!(host_authority_well_formed("::1"));
    assert!(!admin_host_allowed(""));
}

#[test]
fn bearer_rejects_duplicate_authorization_headers() {
    let request = TestRequest::default()
        .append_header((header::AUTHORIZATION, "Bearer one"))
        .append_header((header::AUTHORIZATION, "Bearer two"))
        .to_http_request();
    assert!(bearer(&request).is_none());
}

#[test]
fn chat_request_contract_is_profile_aware() {
    let phi = serde_json::json!({
        "model": "microsoft/phi-4-multimodal-instruct",
        "messages": [{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}]
    });
    assert!(validate_chat_request(&phi).is_ok());
    let phi_tools = serde_json::json!({
        "model": "microsoft/phi-4-multimodal-instruct",
        "tools": [],
        "messages": [{"role":"user","content":"hello"}]
    });
    assert!(validate_chat_request(&phi_tools).is_err());
    let glm_object = serde_json::json!({
        "model": "z-ai/glm-5.2",
        "messages": [{"role":"user","content":{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}}]
    });
    assert!(validate_chat_request(&glm_object).is_err());
    let glm_tool_follow_up = serde_json::json!({
        "model": "z-ai/glm-5.2",
        "chat_template_kwargs": {"enable_thinking": false},
        "messages": [
            {"role":"assistant","content":null,"tool_calls":[{"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]},
            {"role":"tool","content":"result"}
        ]
    });
    assert!(validate_chat_request(&glm_tool_follow_up).is_ok());
}

#[test]
fn health_eligibility_requires_verified_upstream_keys() {
    let keys = vec![
        KeySummary {
            id: Uuid::from_u128(1),
            label: "unverified".into(),
            fingerprint: "a".into(),
            enabled: true,
            verified: false,
            cooldown_until: None,
            request_count: 0,
            failure_count: 0,
        },
        KeySummary {
            id: Uuid::from_u128(2),
            label: "verified".into(),
            fingerprint: "b".into(),
            enabled: true,
            verified: true,
            cooldown_until: Some(Utc::now() + Duration::minutes(1)),
            request_count: 0,
            failure_count: 0,
        },
    ];
    assert_eq!(eligible_key_count(&keys), 0);
}

#[test]
fn csp_hash_source_parser_only_accepts_inline_bootstrap() {
    let html = r#"<script src=\"/admin/app.js\"></script><script> boot(); </script>"#;
    assert_eq!(inline_script_bodies(html), vec![" boot(); ".to_owned()]);
}

#[test]
fn provider_proof_and_boundary_helpers_fail_closed() {
    assert_eq!(percent_encode_userinfo("a@b:c/%"), "a%40b%3Ac%2F%25");
    assert!(
        validate_admin_token(
            "nblb_admin_0000000000000000000000000000000000000000000000000000000000000001",
            false,
        )
        .is_ok()
    );
    assert!(validate_admin_token("short", false).is_err());
    assert!(validate_admin_token("test-token", true).is_ok());
}

#[test]
fn seeded_routing_rows_do_not_block_file_vault_migration() {
    assert!(should_migrate_file_vault(0, 0, 2, 0));
    assert!(should_migrate_file_vault(0, 0, 0, 1));
    assert!(!should_migrate_file_vault(0, 0, 0, 0));
    assert!(!should_migrate_file_vault(1, 0, 2, 0));
}

#[test]
fn sse_validation_preserves_frames_split_across_chunks() {
    let mut validator = SseValidator::default();
    validator
            .feed(br#"data: {"id":"chat-1","object":"chat.completion.chunk","model":"z-ai/glm-5.2","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}"#)
            .expect("partial frame is buffered");
    assert!(validator.take_emitted().is_empty());
    validator
        .feed(b"\n\ndata: [DONE]\n\n")
        .expect("complete frames");
    let emitted: Vec<_> = validator.take_emitted().into_iter().collect();
    assert_eq!(emitted.len(), 1);
    assert!(std::str::from_utf8(&emitted[0]).unwrap().contains("chat-1"));
    validator.finish().expect("complete stream");
}

#[test]
fn sse_done_without_data_is_not_a_completion() {
    let mut validator = SseValidator::default();
    validator
        .feed(b"data: [DONE]\n\n")
        .expect("valid terminator");
    assert_eq!(validator.frame_count, 1);
    assert_eq!(validator.data_frame_count, 0);
    assert!(validator.finish().is_err());
}
