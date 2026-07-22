//! Wire contracts shared by the operations application layer and its handlers.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Snapshot {
    pub(crate) observed_at: DateTime<Utc>,
    pub(crate) generated_at: DateTime<Utc>,
    pub(crate) stale: bool,
}

impl Snapshot {
    pub(crate) fn current() -> Self {
        let now = Utc::now();
        Self {
            observed_at: now,
            generated_at: now,
            stale: false,
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub(crate) struct PublicMetric {
    pub(crate) sample_count: u64,
    pub(crate) success_rate: Option<f64>,
    pub(crate) failover_rate: Option<f64>,
    pub(crate) cancellation_rate: Option<f64>,
    pub(crate) latency_p95_ms: Option<u64>,
    pub(crate) ttfb_p95_ms: Option<u64>,
    pub(crate) eligible_provider_count: Option<u8>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicMetricPoint {
    pub(crate) at: DateTime<Utc>,
    #[serde(flatten)]
    pub(crate) metric: PublicMetric,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicState {
    pub(crate) status: &'static str,
    pub(crate) traffic_ready: bool,
    pub(crate) reason_code: Option<&'static str>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicCapacity {
    pub(crate) eligible: u8,
    pub(crate) target: u8,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicEndpoint {
    pub(crate) kind: &'static str,
    pub(crate) state: &'static str,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicSummary {
    pub(crate) schema_version: &'static str,
    pub(crate) snapshot: Snapshot,
    pub(crate) state: PublicState,
    pub(crate) capacity: PublicCapacity,
    pub(crate) metrics_24h: PublicMetric,
    pub(crate) endpoints: Vec<PublicEndpoint>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicMetrics {
    pub(crate) schema_version: &'static str,
    pub(crate) snapshot: Snapshot,
    pub(crate) window: String,
    pub(crate) step: String,
    pub(crate) points: Vec<PublicMetricPoint>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct ModelSpec {
    pub(crate) id: &'static str,
    pub(crate) endpoint: &'static str,
    pub(crate) input_modalities: &'static [&'static str],
    pub(crate) output_modalities: &'static [&'static str],
    pub(crate) streaming: bool,
    pub(crate) tool_calling: bool,
    pub(crate) billable_probe: bool,
}

pub(crate) const MODEL_SPECS: [ModelSpec; 8] = [
    ModelSpec {
        id: "z-ai/glm-5.2",
        endpoint: "/v1/chat/completions",
        input_modalities: &["text"],
        output_modalities: &["text"],
        streaming: true,
        tool_calling: true,
        billable_probe: false,
    },
    ModelSpec {
        id: "microsoft/phi-4-multimodal-instruct",
        endpoint: "/v1/chat/completions",
        input_modalities: &["text", "image", "audio"],
        output_modalities: &["text"],
        streaming: true,
        tool_calling: false,
        billable_probe: false,
    },
    ModelSpec {
        id: "nvidia/vila",
        endpoint: "/v1/nvidia/inference",
        input_modalities: &["text", "image", "video"],
        output_modalities: &["text"],
        streaming: false,
        tool_calling: false,
        billable_probe: false,
    },
    ModelSpec {
        id: "nvidia/nvclip",
        endpoint: "/v1/embeddings",
        input_modalities: &["text", "image"],
        output_modalities: &["embedding"],
        streaming: false,
        tool_calling: false,
        billable_probe: false,
    },
    ModelSpec {
        id: "black-forest-labs/flux.1-kontext-dev",
        endpoint: "/v1/images/generations",
        input_modalities: &["text", "image"],
        output_modalities: &["image"],
        streaming: false,
        tool_calling: false,
        billable_probe: true,
    },
    ModelSpec {
        id: "stabilityai/stable-video-diffusion",
        endpoint: "/v1/videos/generations",
        input_modalities: &["image"],
        output_modalities: &["video"],
        streaming: false,
        tool_calling: false,
        billable_probe: true,
    },
    ModelSpec {
        id: "nvidia/magpie-tts-multilingual",
        endpoint: "/v1/audio/speech",
        input_modalities: &["text"],
        output_modalities: &["audio"],
        streaming: false,
        tool_calling: false,
        billable_probe: false,
    },
    ModelSpec {
        id: "nvidia/parakeet-ctc-1.1b",
        endpoint: "/v1/audio/transcriptions",
        input_modalities: &["audio"],
        output_modalities: &["text"],
        streaming: false,
        tool_calling: false,
        billable_probe: false,
    },
];

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicModel {
    pub(crate) id: &'static str,
    pub(crate) endpoint: &'static str,
    pub(crate) input_modalities: &'static [&'static str],
    pub(crate) output_modalities: &'static [&'static str],
    pub(crate) streaming: bool,
    pub(crate) tool_calling: bool,
    pub(crate) advertised: bool,
    pub(crate) proof_status: &'static str,
    pub(crate) available_now: bool,
    pub(crate) verified_age_seconds: Option<u64>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicModels {
    pub(crate) schema_version: &'static str,
    pub(crate) snapshot: Snapshot,
    pub(crate) items: Vec<PublicModel>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicIncidentUpdate {
    pub(crate) status: String,
    pub(crate) message: String,
    pub(crate) published_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicIncident {
    pub(crate) slug: String,
    pub(crate) title: String,
    pub(crate) status: String,
    pub(crate) severity: String,
    pub(crate) started_at: DateTime<Utc>,
    pub(crate) resolved_at: Option<DateTime<Utc>>,
    pub(crate) updates: Vec<PublicIncidentUpdate>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicIncidents {
    pub(crate) schema_version: &'static str,
    pub(crate) snapshot: Snapshot,
    pub(crate) items: Vec<PublicIncident>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PublicIncidentDetail {
    pub(crate) schema_version: &'static str,
    pub(crate) snapshot: Snapshot,
    pub(crate) item: PublicIncident,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct AuditMutation<T> {
    pub(crate) item: T,
    pub(crate) audit_event_id: Uuid,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Page<T> {
    pub(crate) snapshot: Snapshot,
    pub(crate) items: Vec<T>,
    pub(crate) next_before: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct AttentionAction {
    pub(crate) label: String,
    pub(crate) href: String,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Attention {
    pub(crate) severity: &'static str,
    pub(crate) code: &'static str,
    pub(crate) title: &'static str,
    pub(crate) reason: &'static str,
    pub(crate) action: AttentionAction,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewRuntime {
    pub(crate) live: bool,
    pub(crate) database_ready: bool,
    pub(crate) owner_lease_ready: bool,
    pub(crate) traffic_ready: bool,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewCapacity {
    pub(crate) configured_slots: u8,
    pub(crate) verified_slots: u8,
    pub(crate) eligible_slots: u8,
    pub(crate) pair_ready: bool,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewProfiles {
    pub(crate) catalogued: u8,
    pub(crate) advertised: u8,
    pub(crate) proven: u8,
    pub(crate) available: u8,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewClients {
    pub(crate) active_count: u64,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewRecent {
    pub(crate) last_nvidia_success_at: Option<DateTime<Utc>>,
    pub(crate) last_hermes_e2e_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct PrimaryAction {
    pub(crate) severity: &'static str,
    pub(crate) code: &'static str,
    pub(crate) title: &'static str,
    pub(crate) reason: &'static str,
    pub(crate) label: String,
    pub(crate) href: String,
}

impl From<Attention> for PrimaryAction {
    fn from(value: Attention) -> Self {
        Self {
            severity: value.severity,
            code: value.code,
            title: value.title,
            reason: value.reason,
            label: value.action.label,
            href: value.action.href,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct AdminOverview {
    pub(crate) snapshot: Snapshot,
    pub(crate) runtime: OverviewRuntime,
    pub(crate) capacity: OverviewCapacity,
    pub(crate) profiles: OverviewProfiles,
    pub(crate) clients: OverviewClients,
    pub(crate) qa: OverviewQa,
    pub(crate) recent: OverviewRecent,
    pub(crate) metrics_24h: PublicMetric,
    pub(crate) primary_action: Option<PrimaryAction>,
    pub(crate) attention_count: u64,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct OverviewQa {
    pub(crate) required: u8,
    pub(crate) passed: u8,
    pub(crate) complete: bool,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct ProfileProof {
    pub(crate) profile_id: String,
    pub(crate) status: &'static str,
    pub(crate) verified_key_count: u8,
    pub(crate) last_verified_at: Option<DateTime<Utc>>,
    pub(crate) stale: bool,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Upstream {
    pub(crate) id: Uuid,
    pub(crate) slot_no: u8,
    pub(crate) label: String,
    pub(crate) enabled: bool,
    pub(crate) verified: bool,
    pub(crate) retired: bool,
    pub(crate) eligible_now: bool,
    pub(crate) cooldown_until: Option<DateTime<Utc>>,
    pub(crate) request_count: u64,
    pub(crate) failure_count: u64,
    pub(crate) proofs: Vec<ProfileProof>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Client {
    pub(crate) id: Uuid,
    pub(crate) label: String,
    pub(crate) scopes: Vec<String>,
    pub(crate) active: bool,
    pub(crate) prefix: String,
    pub(crate) expires_at: Option<DateTime<Utc>>,
    pub(crate) model_allowlist: Option<Vec<String>>,
    pub(crate) rpm_limit: Option<u32>,
    pub(crate) max_concurrency: Option<u32>,
    pub(crate) request_limit_day: Option<u32>,
    pub(crate) request_count: u64,
    pub(crate) last_used_at: Option<DateTime<Utc>>,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) revoked_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Attempt {
    pub(crate) id: Uuid,
    pub(crate) attempt_no: u8,
    pub(crate) upstream_id: Uuid,
    pub(crate) upstream_slot_no: u8,
    pub(crate) upstream_label: String,
    pub(crate) outcome: String,
    pub(crate) status_code: Option<u16>,
    pub(crate) error_class: Option<String>,
    pub(crate) latency_ms: Option<u64>,
    pub(crate) ttfb_ms: Option<u64>,
    pub(crate) response_started: bool,
    pub(crate) cooldown_applied_until: Option<DateTime<Utc>>,
    pub(crate) bytes_out: Option<u64>,
    pub(crate) started_at: DateTime<Utc>,
    pub(crate) finished_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct ProxyRequest {
    pub(crate) request_id: Uuid,
    pub(crate) client_id: Option<Uuid>,
    pub(crate) endpoint: String,
    pub(crate) profile_id: String,
    pub(crate) modality: String,
    pub(crate) stream: bool,
    pub(crate) outcome: String,
    pub(crate) status_code: Option<u16>,
    pub(crate) error_class: Option<String>,
    pub(crate) duration_ms: Option<u64>,
    pub(crate) ttfb_ms: Option<u64>,
    pub(crate) failover_count: u8,
    pub(crate) started_at: DateTime<Utc>,
    pub(crate) finished_at: Option<DateTime<Utc>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) attempts: Option<Vec<Attempt>>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub(crate) struct RequestAggregate {
    pub(crate) total: u64,
    pub(crate) succeeded: u64,
    pub(crate) failed: u64,
    pub(crate) cancelled: u64,
    pub(crate) active: u64,
    pub(crate) average_duration_ms: Option<u64>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct ProbeRun {
    pub(crate) id: Uuid,
    pub(crate) kind: String,
    pub(crate) upstream_id: Option<Uuid>,
    pub(crate) profile_id: Option<String>,
    pub(crate) status: String,
    pub(crate) status_code: Option<u16>,
    pub(crate) latency_ms: Option<u64>,
    pub(crate) error_class: Option<String>,
    pub(crate) billable: bool,
    pub(crate) requested_by: String,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) started_at: Option<DateTime<Utc>>,
    pub(crate) finished_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct AdminModel {
    pub(crate) id: &'static str,
    pub(crate) endpoint: &'static str,
    pub(crate) input_modalities: &'static [&'static str],
    pub(crate) output_modalities: &'static [&'static str],
    pub(crate) streaming: bool,
    pub(crate) tool_calling: bool,
    pub(crate) advertised: bool,
    pub(crate) proof_status: &'static str,
    pub(crate) verified_key_count: u8,
    pub(crate) available_now: bool,
    pub(crate) verified_age_seconds: Option<u64>,
    pub(crate) proofs: Vec<ProfileProof>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct IncidentUpdate {
    pub(crate) id: Uuid,
    pub(crate) status: String,
    pub(crate) public_message: String,
    pub(crate) published_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Incident {
    pub(crate) id: Uuid,
    pub(crate) slug: String,
    pub(crate) title: String,
    pub(crate) status: String,
    pub(crate) severity: String,
    pub(crate) public: bool,
    pub(crate) started_at: DateTime<Utc>,
    pub(crate) resolved_at: Option<DateTime<Utc>>,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) updated_at: DateTime<Utc>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) updates: Option<Vec<IncidentUpdate>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct AuditEvent {
    pub(crate) id: Uuid,
    pub(crate) action: String,
    pub(crate) resource_kind: String,
    pub(crate) resource_id: Option<Uuid>,
    pub(crate) actor_kind: String,
    pub(crate) request_id: Option<Uuid>,
    pub(crate) outcome: String,
    pub(crate) detail: Value,
    pub(crate) created_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct QaCase {
    pub(crate) id: Uuid,
    pub(crate) name: String,
    pub(crate) status: String,
    pub(crate) evidence: Value,
    pub(crate) started_at: Option<DateTime<Utc>>,
    pub(crate) finished_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct QaRun {
    pub(crate) id: Uuid,
    pub(crate) suite: String,
    pub(crate) live: bool,
    pub(crate) provider_identity: String,
    pub(crate) deployment_commit: String,
    pub(crate) status: String,
    pub(crate) created_at: DateTime<Utc>,
    pub(crate) started_at: Option<DateTime<Utc>>,
    pub(crate) finished_at: Option<DateTime<Utc>>,
    pub(crate) cases: Vec<QaCase>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct QaCompletion {
    pub(crate) suite: String,
    pub(crate) passed: bool,
    pub(crate) passed_run: Option<QaRun>,
    pub(crate) latest_run: Option<QaRun>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct QaRunsPage {
    pub(crate) snapshot: Snapshot,
    pub(crate) deployment_commit: String,
    pub(crate) items: Vec<QaRun>,
    pub(crate) completion: Vec<QaCompletion>,
    pub(crate) next_before: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Settings {
    pub(crate) proof_freshness_seconds: u32,
    pub(crate) request_retention_days: u32,
    pub(crate) metric_retention_days: u32,
    pub(crate) public_incidents_enabled: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            proof_freshness_seconds: 604_800,
            request_retention_days: 30,
            metric_retention_days: 90,
            public_incidents_enabled: true,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RoutingPolicy {
    pub(crate) version: u32,
    pub(crate) active: bool,
    pub(crate) retryable_statuses: Vec<u16>,
    pub(crate) default_cooldown_seconds: u32,
    pub(crate) stream_failover_before_first_frame_only: bool,
    pub(crate) generation_retry: bool,
}

impl Default for RoutingPolicy {
    fn default() -> Self {
        Self {
            version: 1,
            active: true,
            retryable_statuses: vec![402, 408, 429, 500, 502, 503, 504],
            default_cooldown_seconds: 2,
            stream_failover_before_first_frame_only: true,
            generation_retry: false,
        }
    }
}
