use super::admin::{PageCursor, PageQuery, encode_page_cursor, page_before};
use super::provider::upstream_endpoint;
use super::proxy::{parse_multimodal_request, validate_chat_request};
use super::request_id::{assign_request_id, enforce_admin_boundary};
use super::{
    AppState, PROFILES, SseValidator, VaultStore, admin_host_allowed,
    admin_surface_allowed_for_port, bearer, eligible_key_count, format_origin_host,
    host_authority_well_formed, inline_script_bodies, openai_error, operations_error,
    percent_encode_userinfo, routes, should_migrate_file_vault, upstream_endpoint_for,
    validate_admin_token, validate_chat_response,
};
use actix_web::http::{Method, StatusCode, header};
use actix_web::middleware::from_fn;
use actix_web::test::{self, TestRequest};
use actix_web::{App, HttpRequest, HttpResponse, web};
use chrono::{Duration, Utc};
use nvidia_build_lb_core::{KeySummary, Router};
use std::{collections::HashMap, sync::Mutex};
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
                serde_json::json!({"label":"slot","credential":"nvapi-abcdefghijklmnopqrstuvwxyz123456"}),
            ),
            status: StatusCode::CREATED,
            exact_body: None,
            top_level_fields: vec![
                "id",
                "label",
                "fingerprint",
                "enabled",
                "verified",
                "cooldown_until",
                "request_count",
                "failure_count",
            ],
            values: vec![
                ("/label", serde_json::json!("slot")),
                ("/enabled", serde_json::json!(false)),
                ("/verified", serde_json::json!(false)),
                ("/cooldown_until", serde_json::Value::Null),
                ("/request_count", serde_json::json!(0)),
                ("/failure_count", serde_json::json!(0)),
            ],
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
        assert_eq!(
            response.status(),
            case.status,
            "authenticated legacy status changed: {} {}",
            case.method,
            case.path
        );
        assert!(response.headers().contains_key("x-request-id"));
        let body: serde_json::Value = test::read_body_json(response).await;
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
