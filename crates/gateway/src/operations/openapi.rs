//! OpenAPI 3.1 documents generated from the canonical Rust route inventory.

use serde_json::{Map, Value, json};

type OperationSpec<'a> = (&'a str, &'a str, &'a str);
type PathSpec<'a> = (&'a str, &'a [OperationSpec<'a>]);

fn operation(summary: &str, tag: &str, protected: bool) -> Value {
    let mut value = json!({
        "summary": summary,
        "tags": [tag],
        "responses": {
            "200": {"description": "Successful response"},
            "201": {"description": "Resource created"},
            "202": {"description": "Request accepted"},
            "400": {"description": "Malformed request"},
            "403": {"description": "Permission or boundary rejected"},
            "404": {"description": "Resource not found"},
            "409": {"description": "State conflict or confirmation required"},
            "422": {"description": "Canonical validation error"},
            "429": {"description": "Rate limited"},
            "503": {"description": "Dependency unavailable"}
        }
    });
    if protected {
        value["security"] = json!([{"bearerAuth": []}]);
        value["responses"]["401"] = json!({"description":"Missing or invalid administrator token"});
    }
    value
}

fn paths(entries: &[PathSpec<'_>], protected: bool) -> Value {
    let mut paths = Map::new();
    for (path, methods) in entries {
        let mut item = Map::new();
        for (method, summary, tag) in *methods {
            item.insert((*method).to_owned(), operation(summary, tag, protected));
        }
        paths.insert((*path).to_owned(), Value::Object(item));
    }
    Value::Object(paths)
}

pub(crate) fn public_document() -> Value {
    let mut document = json!({
        "openapi":"3.1.0",
        "info":{"title":"NVIDIA Build LB Public API","version":"1.0.0","description":"Sanitized public status API. Identity, credentials, raw provider errors, prompts, and media are never exposed."},
        "servers":[{"url":"https://nvidia-lb.dongwontuna.net"}],
        "paths":paths(&[
            ("/api/public/v1/summary", &[("get","Current readiness, capacity, endpoints, and privacy-suppressed 24h metrics","Status")]),
            ("/api/public/v1/metrics", &[("get","Privacy-suppressed aggregate metric series","Status")]),
            ("/api/public/v1/models", &[("get","Advertised model capabilities and proof state","Models")]),
            ("/api/public/v1/incidents", &[("get","Published incidents","Incidents")]),
            ("/api/public/v1/incidents/{slug}", &[("get","Published incident detail","Incidents")]),
            ("/api/public/v1/openapi.json", &[("get","This OpenAPI document","Documentation")]),
            ("/health/live", &[("get","Process liveness","Health")]),
            ("/health/ready", &[("get","Traffic readiness for the default chat profile","Health")]),
            ("/health", &[("get","Traffic readiness compatibility endpoint","Health")]),
            ("/v1/models", &[("get","OpenAI-compatible model list","OpenAI compatibility")]),
            ("/v1/chat/completions", &[("post","OpenAI-compatible chat and SSE streaming","OpenAI compatibility")]),
            ("/v1/embeddings", &[("post","NVCLIP text or image embeddings","Multimodal")]),
            ("/v1/images/generations", &[("post","Image generation without side-effect replay","Multimodal")]),
            ("/v1/videos/generations", &[("post","Video generation without side-effect replay","Multimodal")]),
            ("/v1/audio/speech", &[("post","Text-to-speech WAV output","Audio")]),
            ("/v1/audio/transcriptions", &[("post","Multipart audio transcription","Audio")]),
            ("/v1/nvidia/inference", &[("post","VILA text, image, or video inference","Multimodal")])
        ], false),
        "components":{"securitySchemes":{"downstreamBearer":{"type":"http","scheme":"bearer"}}},
        "x-nblb-error-contract":{"public":"{error:{code,message,request_id,retryable}}","openai":"{error:{message,type,param,code}}"}
    });
    for (path, method) in [
        ("/v1/models", "get"),
        ("/v1/chat/completions", "post"),
        ("/v1/embeddings", "post"),
        ("/v1/images/generations", "post"),
        ("/v1/videos/generations", "post"),
        ("/v1/audio/speech", "post"),
        ("/v1/audio/transcriptions", "post"),
        ("/v1/nvidia/inference", "post"),
    ] {
        document["paths"][path][method]["security"] = json!([{"downstreamBearer": []}]);
    }
    document
}

pub(crate) fn admin_document() -> Value {
    let mut document = json!({
        "openapi":"3.1.0",
        "info":{"title":"NVIDIA Build LB Administrator API","version":"2.0.0","description":"Loopback-only typed operations API. All mutations are audited and no plaintext credential can be read back."},
        "servers":[{"url":"http://127.0.0.1:2456/admin/api/v2"}],
        "paths":paths(&[
            ("/overview", &[("get","Command-center snapshot and primary action","Overview")]),
            ("/attentions", &[("get","Ordered operator attention queue","Overview")]),
            ("/upstreams", &[("get","List upstream slots","Upstreams"),("post","Create encrypted upstream slot","Upstreams")]),
            ("/upstreams/{id}", &[("get","Get upstream slot","Upstreams")]),
            ("/upstreams/{id}/probe", &[("post","Probe credential and atomically persist proof","Upstreams")]),
            ("/upstreams/{id}/probe-profiles", &[("post","Probe selected provider profiles","Upstreams")]),
            ("/upstreams/{id}/enable", &[("post","Enable verified upstream","Upstreams")]),
            ("/upstreams/{id}/disable", &[("post","Disable upstream","Upstreams")]),
            ("/upstreams/{id}/retire", &[("post","Retire encrypted upstream","Upstreams")]),
            ("/clients", &[("get","List downstream clients","Clients"),("post","Issue one-time downstream token","Clients")]),
            ("/clients/{id}", &[("get","Get downstream client","Clients"),("patch","Patch nullable policy fields","Clients")]),
            ("/clients/{id}/rotate", &[("post","Rotate and show one-time token","Clients")]),
            ("/clients/{id}/revoke", &[("post","Revoke downstream client","Clients")]),
            ("/routing/policy", &[("get","Get active routing policy","Routing"),("patch","Activate a new routing policy version","Routing")]),
            ("/routing/simulate", &[("post","Simulate hard eligibility and retry boundary","Routing")]),
            ("/models", &[("get","List model capabilities","Models")]),
            ("/models/sync", &[("post","Synchronize canonical model catalog","Models")]),
            ("/models/probe", &[("post","Probe a model on one or two upstreams","Models")]),
            ("/requests", &[("get","List privacy-safe proxy requests","Evidence")]),
            ("/requests/{id}", &[("get","Get request and ordered attempts","Evidence")]),
            ("/probes", &[("get","List probe runs","Evidence")]),
            ("/probes/{id}", &[("get","Get probe run","Evidence")]),
            ("/incidents", &[("get","List incidents","Incidents"),("post","Create incident","Incidents")]),
            ("/incidents/{id}", &[("patch","Patch incident metadata","Incidents")]),
            ("/incidents/{id}/updates", &[("post","Publish an incident update","Incidents")]),
            ("/audit", &[("get","List immutable audit events","Governance")]),
            ("/qa/runs", &[("get","List recent QA runs and recover in-progress work","QA"),("post","Start an evidence-backed QA run","QA")]),
            ("/qa/secret-scan", &[("get","Count raw NVIDIA credential matches in persisted operations data","QA")]),
            ("/qa/runs/{id}", &[("get","Poll QA run and case evidence","QA")]),
            ("/qa/runs/{id}/hermes-completion", &[("post","Complete an armed Hermes run with root-helper scalar evidence","QA")]),
            ("/settings", &[("get","Get operations settings","Governance"),("patch","Patch operations settings","Governance")]),
            ("/openapi.json", &[("get","This OpenAPI document","Documentation")])
        ], true),
        "components":{"securitySchemes":{"bearerAuth":{"type":"http","scheme":"bearer"}}},
        "x-nblb-error-contract":"{error:{code,message,request_id,retryable,details?}}"
    });
    document["paths"]["/qa/runs"]["get"]["parameters"] = json!([
        {
            "name":"limit",
            "in":"query",
            "required":false,
            "description":"Page size. Defaults to 50.",
            "schema":{"type":"integer","minimum":1,"maximum":100}
        },
        {
            "name":"before",
            "in":"query",
            "required":false,
            "description":"Opaque next_before cursor from the preceding page.",
            "schema":{"type":"string","minLength":1}
        }
    ]);
    document["paths"]["/qa/runs"]["get"]["responses"]["422"]["description"] =
        json!("Invalid limit or opaque before cursor");
    document["paths"]["/qa/runs"]["post"]["requestBody"] = json!({
        "required":true,
        "content":{"application/json":{"schema":{
            "type":"object",
            "additionalProperties":false,
            "required":["suite","live","confirm_billable"],
            "properties":{
                "suite":{"type":"string","enum":["smoke","distribution","failover","persistence","multimodal","hermes-e2e"]},
                "live":{"type":"boolean","description":"Must be true for hermes-e2e."},
                "confirm_billable":{"type":"boolean"}
            }
        }}}
    });
    document["paths"]["/qa/runs"]["post"]["responses"]["409"] = json!({
        "description":"qa_run_active: one queued or running QA run already owns the global single-flight lease; error.details.active_run_id identifies it"
    });
    document["paths"]["/qa/runs"]["post"]["responses"]["422"]["description"] =
        json!("Canonical validation error; hermes-e2e is rejected unless live is true");
    document["paths"]["/qa/runs"]["post"]["x-nblb-global-single-flight"] = json!(true);
    document["paths"]["/qa/runs"]["post"]["x-nblb-hermes-live-required"] = json!(true);
    document
}

#[cfg(test)]
mod tests {
    use super::{admin_document, public_document};
    use serde_json::json;
    use std::collections::BTreeSet;

    #[test]
    fn openapi_documents_cover_versioned_route_roots() {
        let public = public_document();
        let admin = admin_document();
        assert_eq!(public["openapi"], "3.1.0");
        assert!(public["paths"]["/v1/chat/completions"]["post"].is_object());
        assert!(admin["paths"]["/upstreams/{id}/probe-profiles"]["post"].is_object());
        assert!(admin["paths"]["/qa/runs/{id}"]["get"].is_object());
        assert!(admin["paths"]["/qa/secret-scan"]["get"].is_object());
        assert_eq!(
            admin["paths"]["/qa/runs"]["get"]["parameters"][0]["schema"]["maximum"],
            100
        );
        assert_eq!(
            admin["paths"]["/qa/runs"]["post"]["x-nblb-global-single-flight"],
            true
        );
        assert_eq!(
            admin["paths"]["/qa/runs"]["post"]["x-nblb-hermes-live-required"],
            true
        );
        assert!(admin["paths"]["/routing/policy"]["patch"].is_object());
        assert!(admin["paths"]["/routing"].is_null());
        assert_eq!(
            admin["paths"].as_object().map(|paths| paths.len()),
            Some(32)
        );
        assert_eq!(
            public["paths"].as_object().map(|paths| paths.len()),
            Some(17)
        );
    }

    #[test]
    fn openapi_route_and_auth_inventory_is_exact() {
        let public = public_document();
        let admin = admin_document();
        let public_paths = public["paths"].as_object().expect("public OpenAPI paths");
        let documented = public_paths
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        let expected = [
            "/api/public/v1/incidents",
            "/api/public/v1/incidents/{slug}",
            "/api/public/v1/metrics",
            "/api/public/v1/models",
            "/api/public/v1/openapi.json",
            "/api/public/v1/summary",
            "/health/live",
            "/health/ready",
            "/health",
            "/v1/audio/speech",
            "/v1/audio/transcriptions",
            "/v1/chat/completions",
            "/v1/embeddings",
            "/v1/images/generations",
            "/v1/models",
            "/v1/nvidia/inference",
            "/v1/videos/generations",
        ]
        .into_iter()
        .collect::<BTreeSet<_>>();
        assert_eq!(documented, expected);
        for (path, method) in [
            ("/v1/models", "get"),
            ("/v1/chat/completions", "post"),
            ("/v1/embeddings", "post"),
            ("/v1/images/generations", "post"),
            ("/v1/videos/generations", "post"),
            ("/v1/audio/speech", "post"),
            ("/v1/audio/transcriptions", "post"),
            ("/v1/nvidia/inference", "post"),
        ] {
            assert_eq!(
                public["paths"][path][method]["security"],
                json!([{"downstreamBearer":[]}]),
                "{method} {path}",
            );
        }
        for methods in admin["paths"]
            .as_object()
            .expect("admin OpenAPI paths")
            .values()
            .filter_map(serde_json::Value::as_object)
        {
            for operation in methods.values() {
                assert_eq!(operation["security"], json!([{"bearerAuth":[]}]))
            }
        }
        assert!(public_paths.keys().all(|path| !path.starts_with("/admin")));
    }
}
