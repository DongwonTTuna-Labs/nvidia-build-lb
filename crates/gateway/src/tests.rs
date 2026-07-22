use super::admin::{PageCursor, PageQuery, encode_page_cursor, page_before};
use super::provider::{live_qa_provider_is_canonical, upstream_endpoint};
use super::proxy::{parse_multimodal_request, validate_chat_request};
use super::request_id::{assign_request_id, enforce_admin_boundary};
use super::{
    AppState, PROFILES, SseValidator, VaultStore, admin_host_allowed,
    admin_surface_allowed_for_port, bearer, eligible_key_count, format_origin_host,
    host_authority_well_formed, inline_script_bodies, mock_mp4, mock_wav, openai_error,
    operations_error, percent_encode_userinfo, poll_nvcf, routes, select_initial_key,
    should_migrate_file_vault, upstream_endpoint_for, validate_admin_token, validate_chat_response,
};
use actix_web::http::{Method, StatusCode, header};
use actix_web::middleware::from_fn;
use actix_web::test::{self, TestRequest};
use actix_web::{App, HttpRequest, HttpResponse, HttpServer, web};
use base64::Engine as _;
use chrono::{DateTime, Duration, Utc};
use nvidia_build_lb_core::{KeySummary, Router, Vault};
use std::{collections::HashMap, os::unix::fs::PermissionsExt, sync::Mutex};
use uuid::Uuid;

async fn pr1_operations_error(req: HttpRequest) -> HttpResponse {
    operations_error(
        &req,
        StatusCode::CONFLICT,
        "probe_required",
        "Provider probe is required.",
        false,
        None,
    )
}

async fn pr1_openai_error() -> HttpResponse {
    openai_error(
        StatusCode::SERVICE_UNAVAILABLE,
        "No eligible NVIDIA upstream is available.",
        "service_unavailable_error",
        "no_eligible_upstream",
    )
}

#[test]
fn embedded_mock_media_fixtures_have_consistent_container_structure() {
    let wav = mock_wav();
    assert_eq!(&wav[0..4], b"RIFF");
    assert_eq!(&wav[8..12], b"WAVE");
    assert_eq!(&wav[12..16], b"fmt ");
    assert_eq!(&wav[36..40], b"data");
    assert_eq!(
        u32::from_le_bytes(wav[4..8].try_into().expect("WAV RIFF size")) as usize,
        wav.len() - 8,
    );
    assert_eq!(
        u32::from_le_bytes(wav[40..44].try_into().expect("WAV data size")) as usize,
        wav.len() - 44,
    );
    assert_eq!(
        u32::from_le_bytes(wav[28..32].try_into().expect("WAV byte rate")),
        88_200,
    );
    assert_eq!(
        u16::from_le_bytes(wav[32..34].try_into().expect("WAV block alignment")),
        2,
    );

    let mp4 = mock_mp4();
    let mut offset = 0_usize;
    let mut boxes = Vec::new();
    while offset < mp4.len() {
        assert!(offset + 8 <= mp4.len(), "truncated MP4 box header");
        let size =
            u32::from_be_bytes(mp4[offset..offset + 4].try_into().expect("MP4 box size")) as usize;
        assert!(
            size >= 8 && offset + size <= mp4.len(),
            "invalid MP4 box size"
        );
        boxes.push(&mp4[offset + 4..offset + 8]);
        offset += size;
    }
    assert_eq!(offset, mp4.len());
    assert_eq!(boxes, vec![b"ftyp".as_slice(), b"mdat", b"moov"]);
}

async fn pr1_legacy_test_state(path: &std::path::Path) -> AppState {
    let vault = VaultStore::open(path, [7; 32], None)
        .await
        .expect("open isolated compatibility vault");
    AppState {
        vault,
        router: Mutex::new(
            PROFILES
                .iter()
                .map(|profile| ((*profile).to_owned(), Router::default()))
                .collect::<HashMap<_, _>>(),
        ),
        selection_lock: tokio::sync::Mutex::new(()),
        client: reqwest::Client::new(),
        admin_token: "test-token".to_owned(),
        upstream_url: "mock://provider".to_owned(),
        require_downstream_token: true,
        public_port: 2456,
        csp_hashes: Vec::new(),
    }
}

async fn database_test_state(
    pool: &sqlx::PgPool,
    path: &std::path::Path,
    owner_id: Uuid,
) -> AppState {
    sqlx::query("INSERT INTO nblb.gateway_instances(id) VALUES ($1)")
        .bind(owner_id)
        .execute(pool)
        .await
        .expect("seed test gateway owner");
    AppState {
        vault: VaultStore {
            vault: Mutex::new(Vault::open(path, [51; 32]).expect("open database test vault")),
            database: Some(pool.clone()),
            sync_lock: tokio::sync::Mutex::new(()),
            owner_id: Some(owner_id),
            _owner_guard: None,
        },
        router: Mutex::new(Default::default()),
        selection_lock: tokio::sync::Mutex::new(()),
        client: reqwest::Client::new(),
        admin_token: "test-admin".into(),
        upstream_url: "mock://provider".into(),
        require_downstream_token: false,
        public_port: 2456,
        csp_hashes: Vec::new(),
    }
}

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
fn live_qa_accepts_only_canonical_nvidia_provider_origins() {
    assert!(live_qa_provider_is_canonical(
        "https://integrate.api.nvidia.com/v1/chat/completions"
    ));
    for rejected in [
        "mock://provider",
        "http://integrate.api.nvidia.com/v1/chat/completions",
        "https://provider.example/v1/chat/completions",
        "https://integrate.api.nvidia.com.evil.example/v1/chat/completions",
        "https://user@integrate.api.nvidia.com/v1/chat/completions",
    ] {
        assert!(!live_qa_provider_is_canonical(rejected), "{rejected}");
    }
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
    let png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    assert!(
        parse_multimodal_request(
            "/v1/embeddings",
            &serde_json::to_vec(&serde_json::json!({
                "model":"nvidia/nvclip",
                "input":[png]
            }))
            .expect("NVCLIP image request"),
            "application/json",
        )
        .is_ok(),
        "NVCLIP accepts NVIDIA's documented image data URL input",
    );
    assert!(
        parse_multimodal_request(
            "/v1/images/generations",
            &serde_json::to_vec(&serde_json::json!({
                "model":"black-forest-labs/flux.1-kontext-dev",
                "prompt":"make it green",
                "image":png,
                "steps":30,
                "cfg_scale":3.5,
                "seed":0
            }))
            .expect("FLUX image-edit request"),
            "application/json",
        )
        .is_ok(),
        "FLUX Kontext accepts its documented image-edit input",
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
    assert!(
        parse_multimodal_request(
            "/v1/nvidia/inference",
            br#"{"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}}]}],"max_tokens":8}"#,
            "application/json",
        )
        .is_ok()
    );
    assert!(
        parse_multimodal_request(
            "/v1/nvidia/inference",
            br#"{"model":"nvidia/vila","input":{"image":"legacy-video-shape"}}"#,
            "application/json",
        )
        .is_err()
    );
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

fn pr1_key(id: u128, eligible: bool) -> KeySummary {
    KeySummary {
        id: Uuid::from_u128(id),
        label: format!("slot-{id}"),
        fingerprint: format!("fingerprint-{id}"),
        enabled: eligible,
        verified: eligible,
        cooldown_until: None,
        request_count: 0,
        failure_count: 0,
    }
}

#[test]
fn pr1_readiness_truth_table_is_explicit() {
    use super::health::Readiness;

    let empty = Readiness::from_state(true, &[]);
    assert_eq!(empty.status, "degraded");
    assert!(!empty.ready);
    assert!(empty.database_ready);
    assert_eq!(empty.eligible_keys, 0);
    assert_eq!(
        empty.reason_codes,
        vec!["no_eligible_upstream", "pair_not_ready"]
    );

    let one = Readiness::from_state(true, &[pr1_key(1, true)]);
    assert!(one.ready && one.traffic_ready);
    assert!(!one.pair_ready);
    assert_eq!(one.eligible_keys, 1);
    assert_eq!(one.reason_codes, vec!["pair_not_ready"]);

    let pair = Readiness::from_state(true, &[pr1_key(1, true), pr1_key(2, true)]);
    assert!(pair.ready && pair.traffic_ready && pair.pair_ready);
    assert_eq!(pair.eligible_keys, 2);
    assert!(pair.reason_codes.is_empty());

    let database_down = Readiness::from_state(false, &[pr1_key(1, true), pr1_key(2, true)]);
    assert!(!database_down.ready && !database_down.traffic_ready && !database_down.pair_ready);
    assert_eq!(database_down.eligible_keys, 0);
    assert_eq!(database_down.reason_codes, vec!["database_unavailable"]);
}

#[test]
fn pr1_admin_host_boundary_precedes_authentication() {
    let public = TestRequest::get()
        .uri("/admin/api/v1/overview")
        .insert_header((header::HOST, "nvidia-lb.dongwontuna.net"))
        .insert_header((header::AUTHORIZATION, "Bearer any-value"))
        .to_http_request();
    assert!(!admin_surface_allowed_for_port(&public, 2456));

    let local = TestRequest::get()
        .uri("/admin/api/v1/overview")
        .insert_header((header::HOST, "127.0.0.1:2456"))
        .to_http_request();
    assert!(admin_surface_allowed_for_port(&local, 2456));
}

#[actix_web::test]
async fn pr1_request_id_is_server_owned_and_operations_body_matches() {
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .route("/admin/api/test", web::get().to(pr1_operations_error)),
    )
    .await;
    let supplied = Uuid::new_v4();
    let response = test::call_service(
        &app,
        TestRequest::get()
            .uri("/admin/api/test")
            .insert_header(("x-request-id", supplied.to_string()))
            .to_request(),
    )
    .await;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    let response_id = response
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| Uuid::parse_str(value).ok())
        .expect("response request ID");
    assert_ne!(response_id, supplied);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["request_id"], response_id.to_string());
    assert_eq!(body["error"]["retryable"], false);
}

#[actix_web::test]
async fn pr1_openai_error_shape_keeps_request_id_in_header_only() {
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .route("/v1/fail", web::get().to(pr1_openai_error)),
    )
    .await;
    let response = test::call_service(&app, TestRequest::get().uri("/v1/fail").to_request()).await;
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(response.headers().contains_key("x-request-id"));
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["param"], serde_json::Value::Null);
    assert_eq!(body["error"]["code"], "no_eligible_upstream");
    assert!(body["error"].get("request_id").is_none());
}

#[actix_web::test]
async fn pr1_admin_v1_route_inventory_preserves_legacy_unauthorized_contract() {
    struct Case {
        method: Method,
        path: String,
        body: Option<serde_json::Value>,
    }

    let id = Uuid::from_u128(1);
    let cases = vec![
        Case {
            method: Method::GET,
            path: "/admin/api/v1/operator-readiness".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/overview".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/upstream-keys".into(),
            body: None,
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/upstream-keys".into(),
            body: Some(serde_json::json!({"label":"slot","credential":"credential"})),
        },
        Case {
            method: Method::DELETE,
            path: format!("/admin/api/v1/upstream-keys/{id}"),
            body: None,
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/state"),
            body: Some(serde_json::json!({"enabled":true})),
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/enable"),
            body: None,
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/disable"),
            body: None,
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/probe"),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/upstream-slots".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/model-capabilities".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/generation-readiness".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/downstream-credentials".into(),
            body: None,
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/downstream-credentials".into(),
            body: Some(serde_json::json!({"label":"client","scopes":["models:read"]})),
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/downstream-credentials/{id}/revoke"),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/downstream-tokens".into(),
            body: None,
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/downstream-tokens".into(),
            body: Some(serde_json::json!({"label":"client","scopes":["models:read"]})),
        },
        Case {
            method: Method::DELETE,
            path: format!("/admin/api/v1/downstream-tokens/{id}"),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/operations".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/operations/{id}"),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/attentions".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/events".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/events/{id}"),
            body: None,
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/evidence".into(),
            body: None,
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/evidence/{id}"),
            body: None,
        },
    ];
    let vault_path = std::env::temp_dir().join(format!("nblb-pr1-v1-{}.json", Uuid::new_v4()));
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(web::Data::new(pr1_legacy_test_state(&vault_path).await))
            .configure(routes),
    )
    .await;
    let expected = serde_json::json!({
        "error": {
            "code": "invalid_admin_token",
            "message": "Invalid admin token."
        }
    });

    for case in cases {
        let mut request = TestRequest::default()
            .method(case.method.clone())
            .uri(&case.path)
            .insert_header((header::HOST, "127.0.0.1:2456"));
        if let Some(body) = case.body {
            request = request.set_json(body);
        }
        let response = test::call_service(&app, request.to_request()).await;
        assert_eq!(
            response.status(),
            StatusCode::UNAUTHORIZED,
            "legacy method/path changed: {} {}",
            case.method,
            case.path
        );
        assert_eq!(
            response.headers().get(header::WWW_AUTHENTICATE),
            Some(&header::HeaderValue::from_static(
                "Bearer realm=\"nvidia-build-lb-admin\""
            )),
            "legacy auth challenge changed: {} {}",
            case.method,
            case.path
        );
        assert!(
            response.headers().contains_key("x-request-id"),
            "request correlation missing: {} {}",
            case.method,
            case.path
        );
        let body: serde_json::Value = test::read_body_json(response).await;
        assert_eq!(
            body, expected,
            "legacy response body changed: {} {}",
            case.method, case.path
        );
    }
    drop(app);
    let _ = std::fs::remove_file(vault_path);
}

#[actix_web::test]
async fn pr1_admin_v1_authenticated_status_and_body_fields_are_frozen() {
    struct Case {
        method: Method,
        path: String,
        request_body: Option<serde_json::Value>,
        status: StatusCode,
        exact_body: Option<serde_json::Value>,
        top_level_fields: Vec<&'static str>,
        values: Vec<(&'static str, serde_json::Value)>,
    }

    let id = Uuid::from_u128(1);
    let not_found = serde_json::json!({"error":{"code":"resource_not_found"}});
    let generation_not_ready = serde_json::json!({
        "error": {
            "code": "generation_not_ready",
            "message": "Both NVIDIA upstream slots must be probed and available before issuing a downstream credential.",
            "next_action": "probe_and_enable_all_upstream_slots"
        }
    });
    let cases = vec![
        Case {
            method: Method::GET,
            path: "/admin/api/v1/operator-readiness".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: Some(serde_json::json!({
                "runtime_state":"operational",
                "readiness_cause":"no_eligible_upstream",
                "ledger_status":"ok",
                "capacity_blocker":"none"
            })),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/overview".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec![
                "runtime",
                "upstream_keys",
                "downstream_credentials",
                "models",
                "evidence",
                "server_recommendation",
                "attentions",
                "checks",
                "public_health",
                "snapshot_observed_at",
            ],
            values: vec![
                ("/runtime/status", serde_json::json!("degraded")),
                ("/runtime/ready", serde_json::json!(false)),
                ("/runtime/traffic_ready", serde_json::json!(false)),
                ("/runtime/eligible_keys", serde_json::json!(0)),
                ("/upstream_keys/items", serde_json::json!([])),
                ("/downstream_credentials/items", serde_json::json!([])),
                (
                    "/server_recommendation/action",
                    serde_json::json!("add_upstream_key"),
                ),
            ],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/upstream-keys".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: Some(serde_json::json!({"items":[]})),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/upstream-slots".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec!["snapshot", "slots"],
            values: vec![("/slots", serde_json::json!([]))],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/model-capabilities".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec!["snapshot", "models"],
            values: vec![
                ("/models/0/id", serde_json::json!(PROFILES[0])),
                ("/models/0/available_now", serde_json::json!(false)),
                (
                    "/models/0/proof_status",
                    serde_json::json!("pair_not_ready"),
                ),
            ],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/generation-readiness".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec![
                "snapshot",
                "ready",
                "configured_slots",
                "verified_slots",
                "eligible_slots",
                "advertised_profiles",
                "available_profiles",
                "reasons",
            ],
            values: vec![
                ("/ready", serde_json::json!(false)),
                ("/configured_slots", serde_json::json!(0)),
                ("/verified_slots", serde_json::json!(0)),
                ("/eligible_slots", serde_json::json!(0)),
                ("/advertised_profiles", serde_json::json!(PROFILES.len())),
                ("/available_profiles", serde_json::json!(0)),
                (
                    "/reasons",
                    serde_json::json!([
                        "missing_upstream_slot",
                        "probe_required",
                        "slot_unavailable",
                        "provider_proof_required"
                    ]),
                ),
            ],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/downstream-credentials".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: Some(serde_json::json!({"items":[]})),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/downstream-tokens".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: Some(serde_json::json!({"items":[]})),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/operations".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec!["snapshot", "operations", "next_before"],
            values: vec![
                ("/operations", serde_json::json!([])),
                ("/next_before", serde_json::Value::Null),
            ],
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/operations/{id}"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/attentions".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec!["snapshot", "attentions"],
            values: vec![("/attentions", serde_json::json!([]))],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/events".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: None,
            top_level_fields: vec!["snapshot", "events", "next_before"],
            values: vec![
                ("/events", serde_json::json!([])),
                ("/next_before", serde_json::Value::Null),
            ],
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/events/{id}"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: "/admin/api/v1/evidence".into(),
            request_body: None,
            status: StatusCode::OK,
            exact_body: Some(serde_json::json!({
                "source_of_truth":"encrypted-file-fallback",
                "persisted_upstream_keys":0,
                "persisted_downstream_credentials":0,
                "persisted_routing_profiles":0,
                "persisted_request_attempts":0
            })),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::GET,
            path: format!("/admin/api/v1/evidence/{id}"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/upstream-keys".into(),
            request_body: Some(
                serde_json::json!({"label":"slot","credential":concat!("nvapi","-abcdefghijklmnopqrstuvwxyz123456")}),
            ),
            status: StatusCode::SERVICE_UNAVAILABLE,
            exact_body: Some(serde_json::json!({
                "error": {
                    "code": "audit_unavailable",
                    "message": "The audited mutation store is unavailable."
                }
            })),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::DELETE,
            path: format!("/admin/api/v1/upstream-keys/{id}"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/state"),
            request_body: Some(serde_json::json!({"enabled":true})),
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/enable"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/disable"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/upstream-keys/{id}/probe"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/downstream-credentials".into(),
            request_body: Some(serde_json::json!({"label":"client","scopes":["models:read"]})),
            status: StatusCode::CONFLICT,
            exact_body: Some(generation_not_ready.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: format!("/admin/api/v1/downstream-credentials/{id}/revoke"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found.clone()),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::POST,
            path: "/admin/api/v1/downstream-tokens".into(),
            request_body: Some(serde_json::json!({"label":"client","scopes":["models:read"]})),
            status: StatusCode::CONFLICT,
            exact_body: Some(generation_not_ready),
            top_level_fields: vec![],
            values: vec![],
        },
        Case {
            method: Method::DELETE,
            path: format!("/admin/api/v1/downstream-tokens/{id}"),
            request_body: None,
            status: StatusCode::NOT_FOUND,
            exact_body: Some(not_found),
            top_level_fields: vec![],
            values: vec![],
        },
    ];

    let vault_path = std::env::temp_dir().join(format!("nblb-pr1-v1-auth-{}.json", Uuid::new_v4()));
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(web::Data::new(pr1_legacy_test_state(&vault_path).await))
            .configure(routes),
    )
    .await;

    for case in cases {
        let mut request = TestRequest::default()
            .method(case.method.clone())
            .uri(&case.path)
            .insert_header((header::HOST, "127.0.0.1:2456"))
            .insert_header((header::AUTHORIZATION, "Bearer test-token"));
        if let Some(body) = case.request_body {
            request = request.set_json(body);
        }
        let response = test::call_service(&app, request.to_request()).await;
        let actual_status = response.status();
        assert!(response.headers().contains_key("x-request-id"));
        let body: serde_json::Value = test::read_body_json(response).await;
        assert_eq!(
            actual_status, case.status,
            "authenticated legacy status changed: {} {}; body={body}",
            case.method, case.path
        );
        if let Some(expected) = case.exact_body {
            assert_eq!(
                body, expected,
                "legacy body changed: {} {}",
                case.method, case.path
            );
            continue;
        }
        let object = body.as_object().unwrap_or_else(|| {
            panic!(
                "legacy body is not an object: {} {}",
                case.method, case.path
            )
        });
        let mut actual_fields = object.keys().map(String::as_str).collect::<Vec<_>>();
        let mut expected_fields = case.top_level_fields;
        actual_fields.sort_unstable();
        expected_fields.sort_unstable();
        assert_eq!(
            actual_fields, expected_fields,
            "legacy top-level fields changed: {} {}",
            case.method, case.path
        );
        for (pointer, expected) in case.values {
            assert_eq!(
                body.pointer(pointer),
                Some(&expected),
                "legacy field changed at {pointer}: {} {}",
                case.method,
                case.path
            );
        }
    }
    drop(app);
    let _ = std::fs::remove_file(vault_path);
}

#[actix_web::test]
async fn pr1_public_admin_boundary_returns_empty_404_before_auth_and_correlation() {
    let vault_path =
        std::env::temp_dir().join(format!("nblb-pr1-boundary-{}.json", Uuid::new_v4()));
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .wrap(from_fn(enforce_admin_boundary))
            .app_data(web::Data::new(pr1_legacy_test_state(&vault_path).await))
            .configure(routes),
    )
    .await;

    for authorization in [None, Some("Bearer test-token")] {
        for path in ["/admin", "/admin/api/v1/overview"] {
            let mut request = TestRequest::get()
                .uri(path)
                .insert_header((header::HOST, "nvidia-lb.dongwontuna.net"));
            if let Some(value) = authorization {
                request = request.insert_header((header::AUTHORIZATION, value));
            }
            let response = test::call_service(&app, request.to_request()).await;
            assert_eq!(response.status(), StatusCode::NOT_FOUND);
            assert!(!response.headers().contains_key(header::WWW_AUTHENTICATE));
            assert!(!response.headers().contains_key("x-request-id"));
            assert!(test::read_body(response).await.is_empty());
        }
    }
    drop(app);
    let _ = std::fs::remove_file(vault_path);
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
            concat!(
                "nblb_admin_",
                "0000000000000000000000000000000000000000000000000000000000000001"
            ),
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

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn disjoint_settings_updates_do_not_overwrite_each_other(pool: sqlx::PgPool) {
    use crate::operations::admin_repository;

    let (freshness, incidents) = tokio::join!(
        admin_repository::update_settings(&pool, Some(7_200), None, None, None, Uuid::new_v4(),),
        admin_repository::update_settings(&pool, None, None, None, Some(false), Uuid::new_v4(),)
    );
    freshness.expect("update proof freshness");
    incidents.expect("update public incident policy");
    let settings = admin_repository::settings(&pool)
        .await
        .expect("load converged settings");
    assert_eq!(settings.proof_freshness_seconds, 7_200);
    assert!(!settings.public_incidents_enabled);
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn stale_profile_proof_is_unavailable_across_health_public_and_admin(pool: sqlx::PgPool) {
    use crate::operations::{admin_repository, repository};

    sqlx::query(
        "UPDATE nblb.operations_settings SET proof_freshness_seconds=300 WHERE singleton=true",
    )
    .execute(&pool)
    .await
    .expect("set short proof freshness");
    let key_id = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO nblb.upstream_keys(id,slot_no,label,fingerprint,ciphertext,nonce,enabled,verified) VALUES ($1,1,'stale-key',decode(repeat('81',32),'hex'),decode(repeat('82',24),'hex'),decode(repeat('83',12),'hex'),true,true)",
    )
    .bind(key_id)
    .execute(&pool)
    .await
    .expect("seed stale-proof upstream");
    sqlx::query(
        "INSERT INTO nblb.profile_probe_receipts(profile_id,key_id,verified_at) VALUES ('z-ai/glm-5.2',$1,now()-interval '1 hour')",
    )
    .bind(key_id)
    .execute(&pool)
    .await
    .expect("seed stale profile receipt");

    assert!(
        repository::eligible_key_ids(&pool, "z-ai/glm-5.2")
            .await
            .expect("load health eligibility")
            .is_empty()
    );
    let upstream = admin_repository::upstream(&pool, key_id)
        .await
        .expect("load admin upstream")
        .expect("seeded upstream");
    assert!(!upstream.eligible_now);
    let glm_proof = upstream
        .proofs
        .iter()
        .find(|proof| proof.profile_id == "z-ai/glm-5.2")
        .expect("GLM proof projection");
    assert!(glm_proof.stale);
    assert_eq!(glm_proof.verified_key_count, 0);

    let models = repository::public_models(&pool)
        .await
        .expect("load public models");
    let glm = models
        .iter()
        .find(|model| model.id == "z-ai/glm-5.2")
        .expect("GLM public projection");
    assert!(!glm.available_now);
    assert_eq!(glm.proof_status, "proof_required");

    let owner_id = Uuid::new_v4();
    let directory = tempfile::tempdir().expect("selector vault directory");
    let state = web::Data::new(
        database_test_state(&pool, &directory.path().join("vault.json"), owner_id).await,
    );
    assert_eq!(select_initial_key(&state, "z-ai/glm-5.2").await, None);
    sqlx::query(
        "UPDATE nblb.profile_probe_receipts SET invalidated_at=now(),invalidation_reason='provider_auth_rejected' WHERE profile_id='z-ai/glm-5.2' AND key_id=$1",
    )
    .bind(key_id)
    .execute(&pool)
    .await
    .expect("invalidate legacy proof projection");
    assert!(
        crate::admin::profile_proof_keys(&state)
            .await
            .expect("load legacy valid proofs")
            .get("z-ai/glm-5.2")
            .is_none_or(|keys| !keys.contains(&key_id))
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT generation FROM nblb.routing_state WHERE profile_id='z-ai/glm-5.2'",
        )
        .fetch_one(&pool)
        .await
        .expect("load unchanged stale-proof cursor"),
        0,
    );
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn routing_simulation_matches_freshness_and_cursor_without_persisting(pool: sqlx::PgPool) {
    let first_id = Uuid::new_v4();
    let second_id = Uuid::new_v4();
    for (id, slot, byte) in [(first_id, 1_i16, "91"), (second_id, 2_i16, "92")] {
        sqlx::query(
            "INSERT INTO nblb.upstream_keys(id,slot_no,label,fingerprint,ciphertext,nonce,enabled,verified) VALUES ($1,$2,$3,decode(repeat($4,32),'hex'),decode(repeat($4,24),'hex'),decode(repeat($4,12),'hex'),true,true)",
        )
        .bind(id)
        .bind(slot)
        .bind(format!("slot-{slot}"))
        .bind(byte)
        .execute(&pool)
        .await
        .expect("seed simulation upstream");
        sqlx::query(
            "INSERT INTO nblb.profile_probe_receipts(profile_id,key_id,verified_at) VALUES ('z-ai/glm-5.2',$1,now())",
        )
        .bind(id)
        .execute(&pool)
        .await
        .expect("seed simulation proof");
    }
    sqlx::query(
        "UPDATE nblb.routing_state SET next_slot=2,generation=7 WHERE profile_id='z-ai/glm-5.2'",
    )
    .execute(&pool)
    .await
    .expect("seed simulation cursor");

    let directory = tempfile::tempdir().expect("simulation vault directory");
    let state = web::Data::new(
        database_test_state(&pool, &directory.path().join("vault.json"), Uuid::new_v4()).await,
    );
    let app = test::init_service(
        App::new()
            .app_data(state)
            .configure(crate::operations::admin::routes),
    )
    .await;
    let request = TestRequest::post()
        .uri("/admin/api/v2/routing/simulate")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "profile_id":"z-ai/glm-5.2",
            "endpoint":"/v1/chat/completions",
            "stream":true
        }))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["selected_slot"], 2);
    assert_eq!(body["eligible_order"][0], second_id.to_string());
    assert_eq!(body["eligible_order"][1], first_id.to_string());
    assert_eq!(body["would_advance_generation"], true);
    assert_eq!(body["would_persist"], false);
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT generation FROM nblb.routing_state WHERE profile_id='z-ai/glm-5.2'",
        )
        .fetch_one(&pool)
        .await
        .expect("load simulation generation"),
        7,
    );

    sqlx::query(
        "UPDATE nblb.profile_probe_receipts SET verified_at=now()-interval '8 days' WHERE key_id=$1",
    )
    .bind(second_id)
    .execute(&pool)
    .await
    .expect("expire preferred slot proof");
    let request = TestRequest::post()
        .uri("/admin/api/v2/routing/simulate")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "profile_id":"z-ai/glm-5.2",
            "endpoint":"/v1/chat/completions",
            "stream":true
        }))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["selected_slot"], 1);
    assert_eq!(body["eligible_order"], serde_json::json!([first_id]));
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn qa_admin_api_enforces_live_hermes_single_flight_and_cursor_contract(pool: sqlx::PgPool) {
    let active_id = sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO nblb.qa_runs(suite,live,deployment_commit,status,started_at) VALUES('smoke',false,$1,'running',now()) RETURNING id",
    )
    .bind(super::BUILD_COMMIT)
    .fetch_one(&pool)
    .await
    .expect("seed active QA run");
    let directory = tempfile::tempdir().expect("QA API vault directory");
    let state = web::Data::new(
        database_test_state(&pool, &directory.path().join("vault.json"), Uuid::new_v4()).await,
    );
    let app = test::init_service(
        App::new()
            .app_data(state)
            .configure(crate::operations::admin::routes),
    )
    .await;

    let fake_hermes = TestRequest::post()
        .uri("/admin/api/v2/qa/runs")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "suite":"hermes-e2e",
            "live":false,
            "confirm_billable":false
        }))
        .to_request();
    let response = test::call_service(&app, fake_hermes).await;
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "hermes_live_required");

    let conflicting = TestRequest::post()
        .uri("/admin/api/v2/qa/runs")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "suite":"distribution",
            "live":false,
            "confirm_billable":false
        }))
        .to_request();
    let response = test::call_service(&app, conflicting).await;
    assert_eq!(response.status(), StatusCode::CONFLICT);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "qa_run_active");
    assert_eq!(
        body["error"]["details"]["active_run_id"],
        active_id.to_string()
    );

    let invalid_cursor = TestRequest::get()
        .uri("/admin/api/v2/qa/runs?before=not-a-cursor")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .to_request();
    let response = test::call_service(&app, invalid_cursor).await;
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "invalid_page_cursor");
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn qa_admin_rejects_live_mock_but_accepts_explicit_fake_identity(pool: sqlx::PgPool) {
    let directory = tempfile::tempdir().expect("QA provider identity vault directory");
    let state = web::Data::new(
        database_test_state(&pool, &directory.path().join("vault.json"), Uuid::new_v4()).await,
    );
    let app = test::init_service(
        App::new()
            .app_data(state)
            .configure(crate::operations::admin::routes),
    )
    .await;

    let live_mock = TestRequest::post()
        .uri("/admin/api/v2/qa/runs")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "suite":"distribution",
            "live":true,
            "confirm_billable":false
        }))
        .to_request();
    let response = test::call_service(&app, live_mock).await;
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "live_nvidia_provider_required");
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.qa_runs")
            .fetch_one(&pool)
            .await
            .expect("count rejected live mock runs"),
        0
    );

    let fake_mock = TestRequest::post()
        .uri("/admin/api/v2/qa/runs")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, "Bearer test-admin"))
        .set_json(serde_json::json!({
            "suite":"distribution",
            "live":false,
            "confirm_billable":false
        }))
        .to_request();
    let response = test::call_service(&app, fake_mock).await;
    assert_eq!(response.status(), StatusCode::ACCEPTED);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["item"]["provider_identity"], "fake");
    assert_eq!(body["item"]["live"], false);
    let run_id =
        Uuid::parse_str(body["item"]["id"].as_str().expect("fake run ID")).expect("fake run UUID");
    let terminal_status = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        loop {
            let status: String = sqlx::query_scalar("SELECT status FROM nblb.qa_runs WHERE id=$1")
                .bind(run_id)
                .fetch_one(&pool)
                .await
                .expect("load fake run status");
            if matches!(status.as_str(), "passed" | "failed" | "cancelled") {
                return status;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("fake QA runner must reach a terminal state before the test database closes");
    assert_eq!(terminal_status, "failed");
}

async fn mock_runtime_test_state(
    pool: &sqlx::PgPool,
    vault_path: &std::path::Path,
    upstream_url: &str,
) -> (web::Data<AppState>, String) {
    let store = VaultStore::open(vault_path, [61; 32], Some(pool.clone()))
        .await
        .expect("open mock runtime vault");
    let mut key_ids = Vec::new();
    for (index, fill) in ['a', 'b'].into_iter().enumerate() {
        let credential = format!("{}{}{}", "nv", "api-", fill.to_string().repeat(80));
        let key = store
            .mutate(|vault| {
                let key = vault.add(format!("mock-slot-{}", index + 1), &credential)?;
                vault.mark_verified(key.id)?;
                vault.set_enabled(key.id, true)?;
                Ok(key)
            })
            .await
            .expect("seed mock upstream");
        key_ids.push(key.id);
    }
    for key_id in key_ids {
        for profile in PROFILES {
            sqlx::query(
                "INSERT INTO nblb.profile_probe_receipts(profile_id,key_id) VALUES ($1,$2)",
            )
            .bind(profile)
            .bind(key_id)
            .execute(pool)
            .await
            .expect("seed mock profile proof");
        }
    }
    let issued = store
        .mutate(|vault| {
            vault.issue_downstream(
                "mock-matrix-client",
                &[
                    "models:read",
                    "chat:write",
                    "embeddings:write",
                    "images:write",
                    "media:write",
                    "audio:write",
                ]
                .map(str::to_owned),
            )
        })
        .await
        .expect("issue mock matrix client");
    let state = web::Data::new(AppState {
        vault: store,
        router: Mutex::new(Default::default()),
        selection_lock: tokio::sync::Mutex::new(()),
        client: reqwest::Client::new(),
        admin_token: "test-admin".into(),
        upstream_url: upstream_url.into(),
        require_downstream_token: true,
        public_port: 2456,
        csp_hashes: Vec::new(),
    });
    (state, issued.token)
}

async fn restore_mock_upstreams(state: &web::Data<AppState>, pool: &sqlx::PgPool) {
    if let Some(owner_id) = state.vault.owner_id {
        sqlx::query("UPDATE nblb.gateway_instances SET last_seen_at=now() WHERE id=$1")
            .bind(owner_id)
            .execute(pool)
            .await
            .expect("refresh isolated mock gateway owner lease");
    }
    for key in state.vault.list() {
        state
            .vault
            .mutate(|vault| {
                vault.mark_verified(key.id)?;
                vault.set_enabled(key.id, true)?;
                Ok(())
            })
            .await
            .expect("restore mock upstream state");
        for profile in PROFILES {
            sqlx::query(
                "INSERT INTO nblb.profile_probe_receipts(profile_id,key_id,invalidated_at,invalidation_reason) VALUES ($1,$2,NULL,NULL) ON CONFLICT(profile_id,key_id) DO UPDATE SET verified_at=now(),invalidated_at=NULL,invalidation_reason=NULL",
            )
            .bind(profile)
            .bind(key.id)
            .execute(pool)
            .await
            .expect("restore mock profile proof");
        }
    }
}

async fn downstream_usage(pool: &sqlx::PgPool, client_id: Uuid) -> i64 {
    sqlx::query_scalar("SELECT request_count FROM nblb.downstream_credentials WHERE id=$1")
        .bind(client_id)
        .fetch_one(pool)
        .await
        .expect("load downstream usage")
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn expired_downstream_tokens_are_unauthorized_without_usage_mutation(pool: sqlx::PgPool) {
    let directory = tempfile::tempdir().expect("expired auth vault directory");
    let vault_path = directory.path().join("vault.json");
    let (state, scoped_token) =
        mock_runtime_test_state(&pool, &vault_path, "mock://provider").await;
    let restricted = state
        .vault
        .mutate(|vault| vault.issue_downstream("expired-no-chat", &["models:read".to_owned()]))
        .await
        .expect("issue expired restricted client");
    let valid_wrong_scope = state
        .vault
        .mutate(|vault| vault.issue_downstream("valid-no-chat", &["models:read".to_owned()]))
        .await
        .expect("issue valid restricted client");
    let valid_rejected_payload = state
        .vault
        .mutate(|vault| vault.issue_downstream("valid-invalid-payload", &["chat:write".to_owned()]))
        .await
        .expect("issue valid client for rejected payload");
    let scoped_id = state
        .vault
        .list_downstream()
        .into_iter()
        .find(|client| client.label == "mock-matrix-client")
        .expect("scoped mock client")
        .id;
    sqlx::query(
        "UPDATE nblb.downstream_credentials SET created_at=now()-interval '2 seconds',expires_at=now()-interval '1 second' WHERE id = ANY($1)",
    )
    .bind([scoped_id, restricted.summary.id])
    .execute(&pool)
    .await
    .expect("expire both credential shapes");

    let before_rows = sqlx::query_as::<_, (Uuid, i64, Option<DateTime<Utc>>)>(
        "SELECT id,request_count,last_used_at FROM nblb.downstream_credentials WHERE id = ANY($1) ORDER BY id",
    )
    .bind([
        scoped_id,
        restricted.summary.id,
        valid_wrong_scope.summary.id,
        valid_rejected_payload.summary.id,
    ])
    .fetch_all(&pool)
    .await
    .expect("load usage before rejected authentication");
    let before_vault = state.vault.list_downstream();
    let before_file =
        std::fs::read(&vault_path).expect("read vault before rejected authentication");
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(state.clone())
            .configure(routes),
    )
    .await;

    for token in [&scoped_token, &restricted.token] {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({
                "model":"z-ai/glm-5.2",
                "messages":[{"role":"user","content":"must not run"}]
            }))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let body: serde_json::Value = test::read_body_json(response).await;
        assert_eq!(body["error"]["code"], "invalid_downstream_token");
    }

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((
            header::AUTHORIZATION,
            format!("Bearer {}", valid_wrong_scope.token),
        ))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"must not run"}]
        }))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "insufficient_scope");

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((
            header::AUTHORIZATION,
            format!("Bearer {}", valid_rejected_payload.token),
        ))
        .insert_header((header::CONTENT_TYPE, "application/json"))
        .set_payload("{")
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "invalid_request");

    let after_rows = sqlx::query_as::<_, (Uuid, i64, Option<DateTime<Utc>>)>(
        "SELECT id,request_count,last_used_at FROM nblb.downstream_credentials WHERE id = ANY($1) ORDER BY id",
    )
    .bind([
        scoped_id,
        restricted.summary.id,
        valid_wrong_scope.summary.id,
        valid_rejected_payload.summary.id,
    ])
    .fetch_all(&pool)
    .await
    .expect("load usage after rejected authentication");
    assert_eq!(after_rows, before_rows);
    assert_eq!(state.vault.list_downstream(), before_vault);
    assert_eq!(
        std::fs::read(&vault_path).expect("read vault after rejected authentication"),
        before_file
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.proxy_requests")
            .fetch_one(&pool)
            .await
            .expect("count rejected request evidence"),
        0
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM nblb.downstream_request_permits")
            .fetch_one(&pool)
            .await
            .expect("count rejected permits"),
        0
    );
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn usage_persistence_failure_closes_parent_attempt_and_permit(pool: sqlx::PgPool) {
    let directory = tempfile::tempdir().expect("usage failure vault directory");
    let vault_path = directory.path().join("vault.json");
    let (state, token) = mock_runtime_test_state(&pool, &vault_path, "mock://provider").await;
    let client_id = state
        .vault
        .list_downstream()
        .into_iter()
        .find(|client| client.label == "mock-matrix-client")
        .expect("mock matrix client")
        .id;
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(state.clone())
            .configure(routes),
    )
    .await;

    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o500))
        .expect("make vault directory read-only");
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"usage persistence must fail"}]
        }))
        .to_request();
    let response = test::call_service(&app, request).await;
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700))
        .expect("restore vault directory permissions");

    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("usage failure request ID")
            .to_str()
            .expect("usage failure request ID text"),
    )
    .expect("parse usage failure request ID");
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "credential_store_unavailable");
    assert_eq!(
        sqlx::query_as::<_, (String, Option<i16>, Option<String>)>(
            "SELECT outcome,status_code,error_class FROM nblb.proxy_requests WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load usage failure parent"),
        (
            "failed".into(),
            Some(StatusCode::SERVICE_UNAVAILABLE.as_u16() as i16),
            Some("downstream_credential_usage_record_failed".into()),
        )
    );
    assert_eq!(
        sqlx::query_as::<_, (String, Option<i16>, Option<String>, bool)>(
            "SELECT outcome,status_code,error_class,finished_at IS NOT NULL FROM nblb.request_attempts WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load usage failure attempt"),
        (
            "failed".into(),
            Some(StatusCode::SERVICE_UNAVAILABLE.as_u16() as i16),
            Some("downstream_credential_usage_record_failed".into()),
            true,
        )
    );
    assert!(
        sqlx::query_scalar::<_, bool>(
            "SELECT released_at IS NOT NULL FROM nblb.downstream_request_permits WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load usage failure permit")
    );
    assert_eq!(
        sqlx::query_as::<_, (i64, Option<DateTime<Utc>>)>(
            "SELECT request_count,last_used_at FROM nblb.downstream_credentials WHERE id=$1",
        )
        .bind(client_id)
        .fetch_one(&pool)
        .await
        .expect("load usage after persistence failure"),
        (0, None)
    );
    let vault_client = state
        .vault
        .list_downstream()
        .into_iter()
        .find(|client| client.id == client_id)
        .expect("vault client after usage failure");
    assert_eq!(vault_client.request_count, 0);
    assert_eq!(vault_client.last_used_at, None);
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn downstream_usage_increments_once_only_after_full_admission(pool: sqlx::PgPool) {
    let directory = tempfile::tempdir().expect("admission usage vault directory");
    let (state, token) = mock_runtime_test_state(
        &pool,
        &directory.path().join("vault.json"),
        "mock://provider",
    )
    .await;
    let client_id = state
        .vault
        .list_downstream()
        .into_iter()
        .find(|client| client.label == "mock-matrix-client")
        .expect("mock matrix client")
        .id;
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(state.clone())
            .configure(routes),
    )
    .await;

    let accepted = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"accepted exactly once"}]
        }))
        .to_request();
    assert_eq!(
        test::call_service(&app, accepted).await.status(),
        StatusCode::OK
    );
    assert_eq!(downstream_usage(&pool, client_id).await, 1);
    assert_eq!(
        state
            .vault
            .list_downstream()
            .into_iter()
            .find(|client| client.id == client_id)
            .expect("accepted vault client")
            .request_count,
        1
    );

    let invalid_schema = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2"}))
        .to_request();
    assert_eq!(
        test::call_service(&app, invalid_schema).await.status(),
        StatusCode::BAD_REQUEST
    );

    let oversized = TestRequest::post()
        .uri("/v1/audio/speech")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .insert_header((header::CONTENT_TYPE, "application/json"))
        .set_payload(vec![b'x'; 65 * 1024])
        .to_request();
    assert_eq!(
        test::call_service(&app, oversized).await.status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );

    sqlx::query(
        "UPDATE nblb.downstream_credentials SET model_allowlist=ARRAY['other/model'] WHERE id=$1",
    )
    .bind(client_id)
    .execute(&pool)
    .await
    .expect("restrict model policy");
    let forbidden_model = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"forbidden"}]
        }))
        .to_request();
    assert_eq!(
        test::call_service(&app, forbidden_model).await.status(),
        StatusCode::FORBIDDEN
    );

    sqlx::query("UPDATE nblb.downstream_credentials SET model_allowlist=NULL WHERE id=$1")
        .bind(client_id)
        .execute(&pool)
        .await
        .expect("clear model policy");
    sqlx::query("UPDATE nblb.upstream_keys SET enabled=false")
        .execute(&pool)
        .await
        .expect("disable upstreams");
    let no_upstream = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"no upstream"}]
        }))
        .to_request();
    assert_eq!(
        test::call_service(&app, no_upstream).await.status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    sqlx::query("UPDATE nblb.upstream_keys SET enabled=true")
        .execute(&pool)
        .await
        .expect("restore upstreams");

    for (policy, code) in [
        ("rpm", "rpm_limit_exceeded"),
        ("daily", "daily_limit_exceeded"),
    ] {
        let statement = match policy {
            "rpm" => {
                "UPDATE nblb.downstream_credentials SET rpm_limit=1,request_limit_day=NULL WHERE id=$1"
            }
            _ => {
                "UPDATE nblb.downstream_credentials SET rpm_limit=NULL,request_limit_day=1 WHERE id=$1"
            }
        };
        sqlx::query(statement)
            .bind(client_id)
            .execute(&pool)
            .await
            .expect("set request-count policy");
        let limited = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({
                "model":"z-ai/glm-5.2",
                "messages":[{"role":"user","content":"limited"}]
            }))
            .to_request();
        let response = test::call_service(&app, limited).await;
        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        let body: serde_json::Value = test::read_body_json(response).await;
        assert_eq!(body["error"]["code"], code);
    }

    sqlx::query("UPDATE nblb.downstream_credentials SET rpm_limit=NULL,request_limit_day=NULL,max_concurrency=1 WHERE id=$1")
        .bind(client_id)
        .execute(&pool)
        .await
        .expect("set concurrency policy");
    sqlx::query(
        "INSERT INTO nblb.downstream_request_permits(request_id,downstream_credential_id,owner_id,profile_id) SELECT $1,$2,id,'z-ai/glm-5.2' FROM nblb.gateway_instances LIMIT 1",
    )
    .bind(Uuid::new_v4())
    .bind(client_id)
    .execute(&pool)
    .await
    .expect("seed active concurrency permit");
    let concurrent = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({
            "model":"z-ai/glm-5.2",
            "messages":[{"role":"user","content":"concurrent"}]
        }))
        .to_request();
    let response = test::call_service(&app, concurrent).await;
    assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
    let body: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(body["error"]["code"], "concurrency_limit_exceeded");

    assert_eq!(downstream_usage(&pool, client_id).await, 1);
    assert_eq!(
        state
            .vault
            .list_downstream()
            .into_iter()
            .find(|client| client.id == client_id)
            .expect("rejected vault client")
            .request_count,
        1
    );
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn fake_provider_matrix_enforces_retry_and_stream_boundaries(pool: sqlx::PgPool) {
    let directory = tempfile::tempdir().expect("mock matrix vault directory");
    let (state, token) = mock_runtime_test_state(
        &pool,
        &directory.path().join("vault.json"),
        "mock://provider",
    )
    .await;
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(state.clone())
            .configure(routes),
    )
    .await;

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_status":400}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("400 correlation header")
            .to_str()
            .expect("400 correlation UUID"),
    )
    .expect("parse 400 request ID");
    assert_eq!(
        sqlx::query_as::<_, (String, i16, i64)>(
            "SELECT request.outcome,request.failover_count,(SELECT count(*) FROM nblb.request_attempts attempt WHERE attempt.proxy_request_id=request.id) FROM nblb.proxy_requests request WHERE request.request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load 400 evidence"),
        ("rejected".into(), 0, 1),
    );

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_status":401}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .expect("parse 401 failover request ID");
    assert_eq!(
        sqlx::query_as::<_, (String, i16, i64)>(
            "SELECT request.outcome,request.failover_count,(SELECT count(*) FROM nblb.request_attempts attempt WHERE attempt.proxy_request_id=request.id) FROM nblb.proxy_requests request WHERE request.request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load 401 failover evidence"),
        ("succeeded".into(), 1, 2),
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.upstream_keys WHERE verified=false AND enabled=false",
        )
        .fetch_one(&pool)
        .await
        .expect("count quarantined mock upstreams"),
        1,
    );

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_status":429}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .expect("parse 429 failover request ID");
    let rate_limit = sqlx::query_as::<_, (i64, bool)>(
        "SELECT count(*),bool_and(cooldown_applied_until>=created_at+interval '29 seconds') FROM nblb.request_attempts WHERE request_id=$1 AND status_code=429",
    )
    .bind(request_id)
    .fetch_one(&pool)
    .await
    .expect("load 429 cooldown evidence");
    assert_eq!(rate_limit, (1, true));

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_status":500}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .expect("parse 500 failover request ID");
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.request_attempts WHERE request_id=$1 AND status_code=500 AND outcome='failed'",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load 500 failover evidence"),
        1,
    );

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"mock_response":"invalid_json"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);

    restore_mock_upstreams(&state, &pool).await;
    for scenario in [
        "fragmented_sse",
        "disconnect_before_first_frame",
        "invalid_sse",
    ] {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"mock_stream_scenario":scenario}}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK, "{scenario}");
        let body = test::read_body(response).await;
        assert!(
            body.windows(6).any(|window| window == b"[DONE]"),
            "{scenario}"
        );
        restore_mock_upstreams(&state, &pool).await;
    }

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"mock_stream_scenario":"disconnect_after_first_frame"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .expect("parse post-frame request ID");
    let body = test::read_body(response).await;
    assert!(body.windows(12).any(|window| window == b"event: error"));
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.request_attempts WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("count post-frame attempts"),
        1,
        "a committed stream must never be replayed",
    );

    restore_mock_upstreams(&state, &pool).await;
    let png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    let wav = format!(
        "data:audio/wav;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(mock_wav())
    );
    let mp4 = format!(
        "data:video/mp4;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(mock_mp4())
    );
    for (path, body) in [
        (
            "/v1/chat/completions",
            serde_json::json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":png}}]}]}),
        ),
        (
            "/v1/chat/completions",
            serde_json::json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Transcribe"},{"type":"audio_url","audio_url":{"url":wav}}]}]}),
        ),
        (
            "/v1/nvidia/inference",
            serde_json::json!({"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"video_url","video_url":{"url":mp4}}]}]}),
        ),
        (
            "/v1/embeddings",
            serde_json::json!({"model":"nvidia/nvclip","input":["QA",png]}),
        ),
        (
            "/v1/images/generations",
            serde_json::json!({"model":"black-forest-labs/flux.1-kontext-dev","prompt":"make it green","image":png,"n":1,"response_format":"b64_json"}),
        ),
        (
            "/v1/videos/generations",
            serde_json::json!({"model":"stabilityai/stable-video-diffusion","input_reference":png}),
        ),
        (
            "/v1/audio/speech",
            serde_json::json!({"model":"nvidia/magpie-tts-multilingual","input":"QA","response_format":"wav"}),
        ),
    ] {
        let request = TestRequest::post()
            .uri(path)
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(body)
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK, "{path}");
        assert!(!test::read_body(response).await.is_empty(), "{path}");
    }

    let boundary = "nblb-test-boundary";
    let mut multipart = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"qa.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
    )
    .into_bytes();
    multipart.extend_from_slice(&mock_wav());
    multipart.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    let request = TestRequest::post()
        .uri("/v1/audio/transcriptions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .insert_header((
            header::CONTENT_TYPE,
            format!("multipart/form-data; boundary={boundary}"),
        ))
        .set_payload(multipart)
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert!(!test::read_body(response).await.is_empty());

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/images/generations")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .insert_header(("x-nblb-mock-status", "500"))
        .set_json(serde_json::json!({"model":"black-forest-labs/flux.1-kontext-dev","prompt":"no replay","n":1,"response_format":"b64_json"}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .expect("parse generation failure request ID");
    assert_eq!(
        sqlx::query_as::<_, (i64, Option<i16>)>(
            "SELECT count(*),max(status_code) FROM nblb.request_attempts WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load generation no-replay evidence"),
        (1, Some(500)),
        "generation POST must not be replayed on another key",
    );

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/embeddings")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .insert_header(("x-nblb-mock-invalid-response", "1"))
        .set_json(serde_json::json!({"model":"nvidia/nvclip","input":"QA"}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let _ = test::read_body(response).await;

    restore_mock_upstreams(&state, &pool).await;
    let generation_before = sqlx::query_scalar::<_, i64>(
        "UPDATE nblb.routing_state SET next_slot=1 WHERE profile_id='z-ai/glm-5.2' RETURNING generation",
    )
    .fetch_one(&pool)
    .await
    .expect("prepare exact round-robin sequence");
    let mut distribution_ids = Vec::new();
    for _ in 0..6 {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}]}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK);
        distribution_ids.push(
            Uuid::parse_str(
                response
                    .headers()
                    .get("x-request-id")
                    .expect("distribution request ID")
                    .to_str()
                    .expect("distribution request ID text"),
            )
            .expect("parse distribution request ID"),
        );
        let _ = test::read_body(response).await;
    }
    let (slot_sequence, attempt_count, succeeded_attempts) =
        sqlx::query_as::<_, (Vec<i16>, i64, i64)>(
            "SELECT array_agg(key.slot_no ORDER BY request.started_at,request.request_id),count(attempt.id)::bigint,count(attempt.id) FILTER (WHERE attempt.outcome='succeeded')::bigint FROM nblb.proxy_requests request JOIN nblb.request_attempts attempt ON attempt.proxy_request_id=request.id JOIN nblb.upstream_keys key ON key.id=attempt.key_id WHERE request.request_id=ANY($1)",
        )
        .bind(&distribution_ids)
        .fetch_one(&pool)
        .await
        .expect("load exact round-robin sequence");
    assert_eq!(slot_sequence, vec![1_i16, 2, 1, 2, 1, 2]);
    assert_eq!((attempt_count, succeeded_attempts), (6, 6));
    let generation_after = sqlx::query_scalar::<_, i64>(
        "SELECT generation FROM nblb.routing_state WHERE profile_id='z-ai/glm-5.2'",
    )
    .fetch_one(&pool)
    .await
    .expect("load exact round-robin final generation");
    assert_eq!(generation_after - generation_before, 6);
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.downstream_request_permits WHERE released_at IS NULL",
        )
        .fetch_one(&pool)
        .await
        .expect("count unreleased downstream permits"),
        0,
        "every terminal fake-provider path must release its concurrency permit before returning",
    );
}

#[derive(Default)]
struct HttpProviderFixture {
    calls: Mutex<HashMap<String, usize>>,
    observations: Mutex<Vec<HttpProviderObservation>>,
}

#[derive(Clone, Debug)]
struct HttpProviderObservation {
    method: String,
    path: String,
    content_type: String,
    authorized: bool,
    body: Vec<u8>,
}

async fn http_provider_fixture(
    state: web::Data<HttpProviderFixture>,
    req: HttpRequest,
    body: web::Bytes,
) -> HttpResponse {
    use bytes::Bytes;
    use futures_util::{StreamExt, stream};
    use std::io;

    let path = req.path().to_owned();
    let method = req.method().as_str().to_owned();
    let content_type = req
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    let authorized = req.headers().contains_key(header::AUTHORIZATION);
    state
        .observations
        .lock()
        .expect("lock HTTP provider observations")
        .push(HttpProviderObservation {
            method: method.clone(),
            path: path.clone(),
            content_type,
            authorized,
            body: body.to_vec(),
        });
    let request: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
    let scenario = request
        .pointer("/metadata/http_fixture")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("success")
        .to_owned();
    let attempt = {
        let mut calls = state.calls.lock().expect("lock HTTP provider fixture");
        let key = format!("{method} {path} {scenario}");
        let count = calls.entry(key).or_default();
        *count += 1;
        *count
    };
    const PNG_BASE64: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    if method == "GET" && path.starts_with("/v2/nvcf/pexec/status/") {
        let request_id = path.rsplit('/').next().unwrap_or_default();
        return match request_id {
            "00000000-0000-4000-8000-000000000001" => HttpResponse::Ok().json(serde_json::json!({
                "video":base64::engine::general_purpose::STANDARD.encode(mock_mp4()),
                "finish_reason":"SUCCESS",
                "seed":1
            })),
            "00000000-0000-4000-8000-000000000002" => HttpResponse::InternalServerError()
                .json(serde_json::json!({"error":{"message":"poll failed"}})),
            "00000000-0000-4000-8000-000000000003" => HttpResponse::Accepted().finish(),
            _ => HttpResponse::NotFound().finish(),
        };
    }
    if path == "/v1/embeddings" {
        return HttpResponse::Ok().json(serde_json::json!({
            "object":"list",
            "data":[{"object":"embedding","index":0,"embedding":vec![0.0_f32;1024]}],
            "model":"nvidia/nvclip",
            "usage":{"prompt_tokens":1,"total_tokens":1}
        }));
    }
    if path == "/v1/images/generations" {
        return HttpResponse::Ok().json(serde_json::json!({
            "artifacts":[{"base64":PNG_BASE64,"finishReason":"SUCCESS","seed":1}]
        }));
    }
    if path == "/v1/videos/generations" {
        let request_id = match request.get("seed").and_then(serde_json::Value::as_u64) {
            Some(2) => "00000000-0000-4000-8000-000000000002",
            Some(3) => "00000000-0000-4000-8000-000000000003",
            _ => "00000000-0000-4000-8000-000000000001",
        };
        return HttpResponse::Accepted()
            .insert_header(("nvcf-reqid", request_id))
            .finish();
    }
    if path == "/v1/audio/speech" {
        return HttpResponse::Ok()
            .content_type("audio/wav")
            .body(mock_wav());
    }
    if path == "/v1/audio/transcriptions" {
        return HttpResponse::Ok().json(serde_json::json!({"text":"QA"}));
    }
    if path == "/v1/nvidia/inference" {
        return HttpResponse::Ok().json(serde_json::json!({
            "id":"chatcmpl-vila-fixture",
            "object":"chat.completion",
            "created":1,
            "model":"nvidia/vila",
            "choices":[{"index":0,"message":{"role":"assistant","content":"QA"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
        }));
    }
    let success = serde_json::json!({
        "id":"chatcmpl-http-fixture",
        "object":"chat.completion",
        "created":1,
        "model":"z-ai/glm-5.2",
        "choices":[{"index":0,"message":{"role":"assistant","content":"QA"},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
    });
    let fixture_status = if matches!(scenario.as_str(), "status-429-date" | "status-429-600") {
        Some(429)
    } else {
        scenario
            .strip_prefix("status-")
            .and_then(|value| value.parse::<u16>().ok())
    };
    if let Some(status) = fixture_status
        && attempt == 1
    {
        let mut response = HttpResponse::build(
            actix_web::http::StatusCode::from_u16(status)
                .unwrap_or(actix_web::http::StatusCode::INTERNAL_SERVER_ERROR),
        );
        if status == 429 {
            let retry_after = if scenario == "status-429-date" {
                httpdate::fmt_http_date(
                    std::time::SystemTime::now() + std::time::Duration::from_secs(4),
                )
            } else if scenario == "status-429-600" {
                "600".to_owned()
            } else {
                "3".to_owned()
            };
            response.insert_header((header::RETRY_AFTER, retry_after));
        }
        return response.json(serde_json::json!({"error":{"message":"fixture"}}));
    }
    if scenario == "slow-headers" && attempt == 1 {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    match scenario.as_str() {
        "malformed-json" => HttpResponse::Ok()
            .content_type("application/json")
            .body("{not-json"),
        "fragmented-sse" | "fixture-stream" | "slow-headers" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::iter([
                Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":")),
                Ok(Bytes::from_static(b"{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n")),
                Ok(Bytes::from_static(b"data: [DONE]\n\n")),
            ])),
        "done-then-pending" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(
                stream::iter([
                    Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n")),
                    Ok(Bytes::from_static(b"data: [DONE]\n\n")),
                ])
                .chain(stream::pending::<Result<Bytes, io::Error>>()),
            ),
        "disconnect-before-frame" if attempt == 1 => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::once(async {
                Err::<Bytes, _>(io::Error::new(io::ErrorKind::ConnectionReset, "fixture reset"))
            })),
        "disconnect-before-frame" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::iter([
                Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n")),
                Ok(Bytes::from_static(b"data: [DONE]\n\n")),
            ])),
        "disconnect-after-frame" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(
                stream::iter([Ok::<_, io::Error>(Bytes::from_static(
                    b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n",
                ))])
                .chain(stream::once(async {
                    tokio::time::sleep(std::time::Duration::from_millis(25)).await;
                    Err(io::Error::new(io::ErrorKind::ConnectionReset, "fixture reset"))
                })),
            ),
        "slow-first-sse" if attempt == 1 => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::pending::<Result<Bytes, io::Error>>()),
        "slow-first-sse" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::iter([
                Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n")),
                Ok(Bytes::from_static(b"data: [DONE]\n\n")),
            ])),
        "stall-after-frame" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(
                stream::once(async {
                    Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n"))
                })
                .chain(stream::pending::<Result<Bytes, io::Error>>()),
            ),
        "cancel-after-frame" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(
                stream::once(async {
                    Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n"))
                })
                .chain(stream::pending::<Result<Bytes, io::Error>>()),
            ),
        "reset-body" if attempt == 1 => HttpResponse::Ok()
            .content_type("application/json")
            .streaming(stream::once(async {
                Err::<Bytes, _>(io::Error::new(
                    io::ErrorKind::ConnectionReset,
                    "fixture body reset",
                ))
            })),
        "slow-body" if attempt == 1 => HttpResponse::Ok()
            .content_type("application/json")
            .streaming(stream::once(async {
                tokio::time::sleep(std::time::Duration::from_millis(750)).await;
                Ok::<_, io::Error>(Bytes::from_static(b"{}"))
            })),
        "wrong-stream-content-type" if attempt == 1 => HttpResponse::Ok()
            .content_type("application/json")
            .body("data: [DONE]\n\n"),
        "wrong-stream-content-type" => HttpResponse::Ok()
            .content_type("text/event-stream")
            .streaming(stream::iter([
                Ok::<_, io::Error>(Bytes::from_static(b"data: {\"id\":\"chatcmpl-http-fixture\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"z-ai/glm-5.2\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"QA\"},\"finish_reason\":null}]}\n\n")),
                Ok(Bytes::from_static(b"data: [DONE]\n\n")),
            ])),
        "oversized-chunked" => {
            let chunks = (0..257).map(|_| {
                Ok::<_, io::Error>(Bytes::from(vec![b'x'; 64 * 1024]))
            });
            HttpResponse::Ok()
                .content_type("application/json")
                .streaming(stream::iter(chunks))
        }
        _ => HttpResponse::Ok().json(success),
    }
}

#[sqlx::test(migrations = "../../migrations/sqlx")]
async fn real_http_provider_matrix_crosses_transport_and_evidence_boundaries(pool: sqlx::PgPool) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind HTTP provider fixture");
    let address = listener
        .local_addr()
        .expect("HTTP provider fixture address");
    let fixture = web::Data::new(HttpProviderFixture::default());
    let fixture_evidence = fixture.clone();
    let (handle_tx, handle_rx) = std::sync::mpsc::sync_channel(1);
    let server_thread = std::thread::spawn(move || {
        actix_web::rt::System::new().block_on(async move {
            let server = HttpServer::new(move || {
                App::new()
                    .app_data(fixture.clone())
                    .default_service(web::to(http_provider_fixture))
            })
            .listen(listener)
            .expect("listen HTTP provider fixture")
            .run();
            handle_tx
                .send(server.handle())
                .expect("publish HTTP provider handle");
            server.await.expect("run HTTP provider fixture");
        });
    });
    let handle = handle_rx.recv().expect("receive HTTP provider handle");

    let directory = tempfile::tempdir().expect("HTTP matrix vault directory");
    let (state, token) = mock_runtime_test_state(
        &pool,
        &directory.path().join("vault.json"),
        &format!("http://{address}/v1/chat/completions"),
    )
    .await;
    // This in-process service bypasses `main`, so mirror the production owner
    // watchdog while the transport matrix intentionally runs beyond the
    // 30-second lease window. Without this task, test duration rather than
    // gateway behavior decides whether later requests fail with 503.
    let heartbeat_state = state.clone();
    let owner_heartbeat = tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(2));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            interval.tick().await;
            heartbeat_state
                .vault
                .heartbeat_owner()
                .await
                .expect("refresh HTTP matrix gateway owner lease");
        }
    });
    let app = test::init_service(
        App::new()
            .wrap(from_fn(assign_request_id))
            .app_data(state.clone())
            .configure(routes),
    )
    .await;

    for status in [401_u16, 403, 402, 408, 429, 500, 502, 503, 504] {
        restore_mock_upstreams(&state, &pool).await;
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"http_fixture":format!("status-{status}")}}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK, "retry status {status}");
        let request_id = Uuid::parse_str(
            response
                .headers()
                .get("x-request-id")
                .expect("HTTP matrix request ID header")
                .to_str()
                .expect("HTTP matrix request ID text"),
        )
        .expect("HTTP matrix request ID");
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.request_attempts WHERE request_id=$1",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("count HTTP matrix attempts"),
            2,
            "retry status {status}",
        );
        if matches!(status, 401 | 403) {
            assert_eq!(
                sqlx::query_as::<_, (i64, i64)>(
                    "SELECT (SELECT count(*) FROM nblb.upstream_keys WHERE verified=false AND enabled=false),(SELECT count(*) FROM nblb.profile_probe_receipts WHERE invalidated_at IS NOT NULL)",
                )
                .fetch_one(&pool)
                .await
                .expect("load HTTP auth quarantine evidence"),
                (1, i64::try_from(PROFILES.len()).expect("profile count fits i64")),
                "status {status} must quarantine one key and invalidate every profile proof",
            );
        }
        if status == 429 {
            let cooldown_seconds = sqlx::query_scalar::<_, f64>(
                "SELECT extract(epoch FROM (cooldown_applied_until-created_at))::double precision FROM nblb.request_attempts WHERE request_id=$1 AND status_code=429",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("load HTTP Retry-After seconds evidence");
            assert!(
                (2.0..=4.0).contains(&cooldown_seconds),
                "numeric Retry-After must apply about three seconds, got {cooldown_seconds}",
            );
        }
    }

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"http_fixture":"status-429-date"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK, "HTTP-date Retry-After");
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("HTTP-date request ID")
            .to_str()
            .expect("HTTP-date request ID text"),
    )
    .expect("parse HTTP-date request ID");
    let cooldown_seconds = sqlx::query_scalar::<_, f64>(
        "SELECT extract(epoch FROM (cooldown_applied_until-created_at))::double precision FROM nblb.request_attempts WHERE request_id=$1 AND status_code=429",
    )
    .bind(request_id)
    .fetch_one(&pool)
    .await
    .expect("load HTTP-date Retry-After evidence");
    assert!(
        (2.0..=5.0).contains(&cooldown_seconds),
        "HTTP-date Retry-After must preserve the bounded provider delay, got {cooldown_seconds}",
    );

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"http_fixture":"status-429-600"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "clamped numeric Retry-After"
    );
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("clamped Retry-After request ID")
            .to_str()
            .expect("clamped Retry-After request ID text"),
    )
    .expect("parse clamped Retry-After request ID");
    let cooldown_seconds = sqlx::query_scalar::<_, f64>(
        "SELECT extract(epoch FROM (cooldown_applied_until-created_at))::double precision FROM nblb.request_attempts WHERE request_id=$1 AND status_code=429",
    )
    .bind(request_id)
    .fetch_one(&pool)
    .await
    .expect("load clamped Retry-After evidence");
    assert!(
        (299.0..=301.0).contains(&cooldown_seconds),
        "numeric Retry-After above the bound must clamp to 300 seconds, got {cooldown_seconds}",
    );

    restore_mock_upstreams(&state, &pool).await;
    for scenario in ["malformed-json", "oversized-chunked"] {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"http_fixture":scenario}}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY, "{scenario}");
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.upstream_keys WHERE enabled=true AND verified=true AND retired=false",
            )
            .fetch_one(&pool)
            .await
            .expect("count eligible keys after provider protocol failure"),
            2,
            "{scenario} must not quarantine a credential",
        );
        assert_eq!(
            sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.upstream_keys WHERE cooldown_until IS NOT NULL",
            )
            .fetch_one(&pool)
            .await
            .expect("count bounded cooldowns after provider protocol failure"),
            1,
            "{scenario} should apply a bounded attempt cooldown",
        );
        restore_mock_upstreams(&state, &pool).await;
    }

    for scenario in [
        "fragmented-sse",
        "done-then-pending",
        "disconnect-before-frame",
        "wrong-stream-content-type",
        "slow-headers",
        "slow-first-sse",
    ] {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":scenario}}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK, "{scenario}");
        let request_id = Uuid::parse_str(
            response
                .headers()
                .get("x-request-id")
                .expect("stream matrix request ID")
                .to_str()
                .expect("stream matrix request ID text"),
        )
        .expect("parse stream matrix request ID");
        let bytes = test::read_body(response).await;
        assert!(
            bytes.windows(6).any(|window| window == b"[DONE]"),
            "{scenario}"
        );
        if scenario == "done-then-pending" {
            assert_eq!(
                sqlx::query_as::<_, (i64, String, bool, Option<String>)>(
                    "SELECT count(*),min(outcome),bool_and(response_started),min(error_class) FROM nblb.request_attempts WHERE request_id=$1",
                )
                .bind(request_id)
                .fetch_one(&pool)
                .await
                .expect("load semantic stream completion evidence"),
                (1, "succeeded".into(), true, None),
                "[DONE] must complete successfully without EOF, timeout, or failover",
            );
        } else if scenario != "fragmented-sse" {
            let attempts = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM nblb.request_attempts WHERE request_id=$1",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("count stream failover attempts");
            assert_eq!(attempts, 2, "{scenario} must fail over before commit");
        }
        restore_mock_upstreams(&state, &pool).await;
    }

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":"disconnect-after-frame"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK, "disconnect after frame");
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("post-frame request ID")
            .to_str()
            .expect("post-frame request ID text"),
    )
    .expect("parse post-frame request ID");
    let bytes = match actix_web::body::to_bytes(response.into_body()).await {
        Ok(bytes) => bytes,
        Err(_) => panic!("post-frame reset must use the bounded SSE error contract"),
    };
    assert!(bytes.windows(12).any(|window| window == b"event: error"));
    assert!(bytes.windows(6).any(|window| window == b"[DONE]"));
    assert_eq!(
        sqlx::query_as::<_, (i64, bool, String)>(
            "SELECT count(*),bool_and(response_started),min(outcome) FROM nblb.request_attempts WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load post-frame disconnect evidence"),
        (1, true, "failed".into()),
        "a committed stream must never replay on the second key",
    );

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":"stall-after-frame"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK, "post-frame idle timeout");
    let request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("post-frame idle request ID")
            .to_str()
            .expect("post-frame idle request ID text"),
    )
    .expect("parse post-frame idle request ID");
    let bytes = test::read_body(response).await;
    assert!(bytes.windows(12).any(|window| window == b"event: error"));
    assert_eq!(
        sqlx::query_as::<_, (i64, bool, String, Option<String>)>(
            "SELECT count(*),bool_and(response_started),min(outcome),min(error_class) FROM nblb.request_attempts WHERE request_id=$1",
        )
        .bind(request_id)
        .fetch_one(&pool)
        .await
        .expect("load post-frame idle evidence"),
        (
            1,
            true,
            "failed".into(),
            Some("upstream_stream_idle_timeout".into()),
        ),
        "a committed stream idle timeout must terminate without replay",
    );

    restore_mock_upstreams(&state, &pool).await;
    let client_id = sqlx::query_scalar::<_, Uuid>(
        "SELECT id FROM nblb.downstream_credentials WHERE label='mock-matrix-client'",
    )
    .fetch_one(&pool)
    .await
    .expect("load fixture downstream client");
    let run_id = sqlx::query_scalar::<_, Uuid>(
        "INSERT INTO nblb.qa_runs(suite,live,provider_identity,status,started_at,deployment_commit) VALUES('failover',true,'nvidia_hosted','running',now(),$1) RETURNING id",
    )
    .bind(crate::BUILD_COMMIT)
    .fetch_one(&pool)
    .await
    .expect("arm live fixture run");
    for kind in ["before_first_frame", "after_first_frame"] {
        sqlx::query(
            "INSERT INTO nblb.qa_failure_fixtures(run_id,kind,downstream_credential_id) VALUES($1,$2,$3)",
        )
        .bind(run_id)
        .bind(kind)
        .bind(client_id)
        .execute(&pool)
        .await
        .expect("insert live failure fixture");
    }

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":"fixture-stream"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = test::read_body(response).await;
    assert!(bytes.windows(6).any(|window| window == b"[DONE]"));

    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":"fixture-stream"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = test::read_body(response).await;
    assert!(bytes.windows(12).any(|window| window == b"event: error"));
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.qa_failure_fixtures WHERE run_id=$1 AND consumed_at IS NOT NULL AND request_id IS NOT NULL AND key_id IS NOT NULL",
        )
        .bind(run_id)
        .fetch_one(&pool)
        .await
        .expect("count consumed live fixtures"),
        2,
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM nblb.request_attempts attempt JOIN nblb.qa_failure_fixtures fixture ON fixture.request_id=attempt.request_id WHERE fixture.run_id=$1 AND ((fixture.kind='before_first_frame' AND attempt.attempt_no=1 AND attempt.outcome='failed' AND attempt.response_started=false) OR (fixture.kind='after_first_frame' AND attempt.attempt_no=1 AND attempt.outcome='failed' AND attempt.response_started=true))",
        )
        .bind(run_id)
        .fetch_one(&pool)
        .await
        .expect("correlate live fixture attempts"),
        2,
    );

    for scenario in ["reset-body", "slow-body"] {
        restore_mock_upstreams(&state, &pool).await;
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"metadata":{"http_fixture":scenario}}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(response.status(), StatusCode::OK, "{scenario}");
        let request_id = Uuid::parse_str(
            response
                .headers()
                .get("x-request-id")
                .expect("non-stream transport request ID")
                .to_str()
                .expect("non-stream transport request ID text"),
        )
        .expect("parse non-stream transport request ID");
        let _ = test::read_body(response).await;
        assert_eq!(
            sqlx::query_as::<_, (i64, i64, i64)>(
                "SELECT count(*),count(*) FILTER (WHERE attempt_no=1 AND outcome='failed' AND error_class IN ('upstream_body_error','upstream_transport_error')),count(*) FILTER (WHERE attempt_no=2 AND outcome='succeeded') FROM nblb.request_attempts WHERE request_id=$1",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("load non-stream transport failover evidence"),
            (2, 1, 1),
            "{scenario} must fail over exactly once",
        );
    }

    restore_mock_upstreams(&state, &pool).await;
    let request = TestRequest::post()
        .uri("/v1/chat/completions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"QA"}],"stream":true,"metadata":{"http_fixture":"cancel-after-frame"}}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let cancelled_request_id = Uuid::parse_str(
        response
            .headers()
            .get("x-request-id")
            .expect("cancelled stream request ID")
            .to_str()
            .expect("cancelled stream request ID text"),
    )
    .expect("parse cancelled stream request ID");
    drop(response);
    let mut cancellation = None;
    for _ in 0..40 {
        cancellation = sqlx::query_as::<_, (String, String, Option<DateTime<Utc>>, Option<DateTime<Utc>>)>(
            "SELECT request.outcome,attempt.outcome,attempt.cooldown_applied_until,key.cooldown_until FROM nblb.proxy_requests request JOIN nblb.request_attempts attempt ON attempt.proxy_request_id=request.id JOIN nblb.upstream_keys key ON key.id=attempt.key_id WHERE request.request_id=$1 AND request.finished_at IS NOT NULL AND attempt.finished_at IS NOT NULL",
        )
        .bind(cancelled_request_id)
        .fetch_optional(&pool)
        .await
        .expect("poll cancellation evidence");
        if cancellation.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    assert_eq!(
        cancellation,
        Some(("cancelled".into(), "cancelled".into(), None, None)),
        "downstream cancellation must not penalize the selected upstream",
    );

    restore_mock_upstreams(&state, &pool).await;
    const PNG_DATA_URL: &str = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    for body in [
        serde_json::json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":PNG_DATA_URL}}]}]}),
        serde_json::json!({"model":"microsoft/phi-4-multimodal-instruct","messages":[{"role":"user","content":[{"type":"text","text":"Listen"},{"type":"audio_url","audio_url":{"url":format!("data:audio/wav;base64,{}",base64::engine::general_purpose::STANDARD.encode(mock_wav()))}}]}]}),
    ] {
        let request = TestRequest::post()
            .uri("/v1/chat/completions")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(body)
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(
            response.status(),
            StatusCode::OK,
            "actual HTTP Phi modality"
        );
        assert!(!test::read_body(response).await.is_empty());
    }

    let request = TestRequest::post()
        .uri("/v1/embeddings")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"nvidia/nvclip","input":[PNG_DATA_URL]}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let embedding: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(
        embedding["data"][0]["embedding"].as_array().map(Vec::len),
        Some(1024)
    );

    let request = TestRequest::post()
        .uri("/v1/images/generations")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"black-forest-labs/flux.1-kontext-dev","prompt":"green","image":PNG_DATA_URL,"n":1,"response_format":"b64_json"}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let image: serde_json::Value = test::read_body_json(response).await;
    assert!(image.get("artifacts").is_none());
    assert!(image["data"][0]["b64_json"].as_str().is_some());

    let request = TestRequest::post()
        .uri("/v1/videos/generations")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"stabilityai/stable-video-diffusion","input_reference":PNG_DATA_URL,"seed":1}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let video: serde_json::Value = test::read_body_json(response).await;
    assert!(video["data"][0]["b64_json"].as_str().is_some());

    let request = TestRequest::post()
        .uri("/v1/nvidia/inference")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe"},{"type":"image_url","image_url":{"url":PNG_DATA_URL}}]}]}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let vila: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(vila["model"], "nvidia/vila");

    let request = TestRequest::post()
        .uri("/v1/audio/speech")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .set_json(serde_json::json!({"model":"nvidia/magpie-tts-multilingual","input":"QA","voice":"en-US","response_format":"wav"}))
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok()),
        Some("audio/wav")
    );
    assert_eq!(
        test::read_body(response).await.as_ref(),
        mock_wav().as_slice()
    );

    let boundary = "actual-http-transcription-boundary";
    let mut multipart = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/parakeet-ctc-1.1b\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"qa.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
    )
    .into_bytes();
    multipart.extend_from_slice(&mock_wav());
    multipart.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    let request = TestRequest::post()
        .uri("/v1/audio/transcriptions")
        .insert_header((header::HOST, "localhost:2456"))
        .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
        .insert_header((
            header::CONTENT_TYPE,
            format!("multipart/form-data; boundary={boundary}"),
        ))
        .set_payload(multipart)
        .to_request();
    let response = test::call_service(&app, request).await;
    assert_eq!(response.status(), StatusCode::OK);
    let transcription: serde_json::Value = test::read_body_json(response).await;
    assert_eq!(transcription["text"], "QA");

    for seed in [2_u64, 3] {
        restore_mock_upstreams(&state, &pool).await;
        let request = TestRequest::post()
            .uri("/v1/videos/generations")
            .insert_header((header::HOST, "localhost:2456"))
            .insert_header((header::AUTHORIZATION, format!("Bearer {token}")))
            .set_json(serde_json::json!({"model":"stabilityai/stable-video-diffusion","input_reference":PNG_DATA_URL,"seed":seed}))
            .to_request();
        let response = test::call_service(&app, request).await;
        assert_eq!(
            response.status(),
            StatusCode::BAD_GATEWAY,
            "NVCF seed {seed}"
        );
        let request_id = Uuid::parse_str(
            response
                .headers()
                .get("x-request-id")
                .expect("NVCF failure request ID")
                .to_str()
                .expect("NVCF failure request ID text"),
        )
        .expect("parse NVCF failure request ID");
        let _ = test::read_body(response).await;
        assert_eq!(
            sqlx::query_as::<_, (i64, String)>(
                "SELECT count(*),min(error_class) FROM nblb.request_attempts WHERE request_id=$1",
            )
            .bind(request_id)
            .fetch_one(&pool)
            .await
            .expect("load NVCF failure evidence"),
            (1, "upstream_poll_error".into()),
            "an accepted NVCF POST must never be replayed",
        );
    }

    let observations = fixture_evidence
        .observations
        .lock()
        .expect("lock final HTTP provider observations")
        .clone();
    assert!(observations.iter().all(|item| item.authorized));
    let flux = observations
        .iter()
        .find(|item| item.path == "/v1/images/generations")
        .expect("observe FLUX provider request");
    let flux_body: serde_json::Value =
        serde_json::from_slice(&flux.body).expect("decode FLUX provider request");
    assert!(flux.content_type.starts_with("application/json"));
    assert!(flux_body.get("model").is_none());
    assert_eq!(flux_body["aspect_ratio"], "match_input_image");
    let video_request = observations
        .iter()
        .find(|item| item.path == "/v1/videos/generations")
        .expect("observe video provider request");
    let video_body: serde_json::Value =
        serde_json::from_slice(&video_request.body).expect("decode video provider request");
    assert!(video_body.get("model").is_none());
    assert!(video_body.get("input_reference").is_none());
    assert_eq!(video_body["image"], PNG_DATA_URL);
    let vila_request = observations
        .iter()
        .find(|item| item.path == "/v1/nvidia/inference")
        .expect("observe VILA provider request");
    let vila_body: serde_json::Value =
        serde_json::from_slice(&vila_request.body).expect("decode VILA provider request");
    assert!(vila_body.get("model").is_none());
    let speech_request = observations
        .iter()
        .find(|item| item.path == "/v1/audio/speech")
        .expect("observe speech provider request");
    assert!(
        speech_request
            .content_type
            .starts_with("multipart/form-data; boundary=")
    );
    assert!(
        speech_request
            .body
            .windows(b"name=\"text\"".len())
            .any(|window| window == b"name=\"text\"")
    );
    let transcription_request = observations
        .iter()
        .find(|item| item.path == "/v1/audio/transcriptions")
        .expect("observe transcription provider request");
    assert!(
        transcription_request
            .content_type
            .starts_with("multipart/form-data; boundary=")
    );
    assert!(
        transcription_request
            .body
            .windows(b"filename=\"qa.wav\"".len())
            .any(|window| window == b"filename=\"qa.wav\"")
    );
    assert!(
        observations
            .iter()
            .any(|item| item.method == "GET" && item.path.starts_with("/v2/nvcf/pexec/status/"))
    );

    owner_heartbeat.abort();
    let _ = owner_heartbeat.await;
    handle.stop(true).await;
    server_thread.join().expect("join HTTP provider fixture");
}

#[actix_web::test]
async fn nvcf_polling_repeats_202_until_same_origin_json_completion() {
    use std::io::{Read, Write};
    use std::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake NVCF provider");
    let address = listener.local_addr().expect("fake NVCF address");
    let request_id = "a52df6e3-804e-4f36-a3b5-ec79d15547ee";
    let server = std::thread::spawn(move || {
        let mut paths = Vec::new();
        for index in 0..3 {
            let (mut stream, _) = listener.accept().expect("accept fake NVCF request");
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            loop {
                let read = stream.read(&mut buffer).expect("read fake NVCF request");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8(request).expect("ASCII fake NVCF request");
            let path = request
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .expect("fake NVCF request path")
                .to_owned();
            paths.push(path);
            assert!(
                request
                    .lines()
                    .any(|line| line.eq_ignore_ascii_case("authorization: Bearer mock-credential")),
                "poll requests must keep provider authentication",
            );
            let response = match index {
                0 => format!(
                    "HTTP/1.1 202 Accepted\r\nnvcf-reqid: {request_id}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                ),
                1 => "HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    .to_owned(),
                _ => {
                    let body = r#"{"status":"fulfilled"}"#;
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len(),
                    )
                }
            };
            stream
                .write_all(response.as_bytes())
                .expect("write fake NVCF response");
        }
        paths
    });

    let client = reqwest::Client::builder()
        .build()
        .expect("build fake NVCF client");
    let origin = format!("http://{address}/invoke");
    let accepted = client
        .get(&origin)
        .bearer_auth("mock-credential")
        .send()
        .await
        .expect("request fake NVCF origin");
    let completed = poll_nvcf(&client, accepted, &origin, "mock-credential")
        .await
        .expect("poll fake NVCF completion");
    assert_eq!(completed.status(), reqwest::StatusCode::OK);
    assert_eq!(
        completed
            .json::<serde_json::Value>()
            .await
            .expect("decode fake NVCF completion"),
        serde_json::json!({"status":"fulfilled"}),
    );
    assert_eq!(
        server.join().expect("join fake NVCF server"),
        vec![
            "/invoke".to_owned(),
            format!("/v2/nvcf/pexec/status/{request_id}"),
            format!("/v2/nvcf/pexec/status/{request_id}"),
        ],
    );
}
