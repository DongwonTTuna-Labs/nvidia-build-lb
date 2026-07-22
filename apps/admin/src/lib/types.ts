export interface Snapshot {
  observed_at: string;
  generated_at: string;
  stale: boolean;
}

export interface Page<T> {
  snapshot: Snapshot;
  items: T[];
  next_before?: string | null;
}

export interface ProfileProof {
  profile_id: string;
  status: string;
  verified_key_count: number;
  last_verified_at: string | null;
  stale: boolean;
}

export interface Upstream {
  id: string;
  slot_no: number;
  label: string;
  enabled: boolean;
  verified: boolean;
  retired: boolean;
  eligible_now: boolean;
  cooldown_until: string | null;
  request_count: number;
  failure_count: number;
  proofs: ProfileProof[];
}

export interface Client {
  id: string;
  label: string;
  scopes: string[];
  active: boolean;
  prefix: string;
  expires_at: string | null;
  model_allowlist: string[] | null;
  rpm_limit: number | null;
  max_concurrency: number | null;
  request_limit_day: number | null;
  request_count: number;
  last_used_at: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface Model {
  id: string;
  endpoint: string;
  input_modalities: string[];
  output_modalities: string[];
  streaming: boolean;
  tool_calling: boolean;
  advertised: boolean;
  proof_status: string;
  verified_key_count: number;
  available_now: boolean;
  verified_age_seconds: number | null;
  proofs: ProfileProof[];
}

export interface PublicMetric {
  sample_count: number;
  success_rate: number | null;
  failover_rate: number | null;
  cancellation_rate: number | null;
  latency_p95_ms: number | null;
  ttfb_p95_ms: number | null;
  eligible_provider_count: number | null;
}

export interface Action {
  severity: string;
  code?: string;
  title: string;
  reason: string;
  label: string;
  href: string;
}

export interface Overview {
  snapshot: Snapshot;
  runtime: {
    live: boolean;
    database_ready: boolean;
    owner_lease_ready: boolean;
    traffic_ready: boolean;
  };
  capacity: {
    configured_slots: number;
    verified_slots: number;
    eligible_slots: number;
    pair_ready: boolean;
  };
  profiles: { catalogued: number; advertised: number; proven: number; available: number };
  clients: { active_count: number };
  qa: { required: number; passed: number; complete: boolean };
  recent: { last_nvidia_success_at: string | null; last_hermes_e2e_at: string | null };
  metrics_24h: PublicMetric;
  primary_action: Action | null;
  attention_count: number;
}

export interface Attention {
  severity: string;
  code?: string;
  title: string;
  reason: string;
  action: { label: string; href: string };
  observed_at?: string;
}

export interface Attempt {
  id: string;
  attempt_no: number;
  upstream_id: string;
  upstream_slot_no: number;
  upstream_label: string;
  outcome: string;
  status_code: number | null;
  error_class: string | null;
  latency_ms: number | null;
  ttfb_ms: number | null;
  response_started: boolean;
  cooldown_applied_until: string | null;
  bytes_out: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface ProxyRequest {
  request_id: string;
  client_id: string | null;
  endpoint: string;
  profile_id: string;
  modality: string;
  stream: boolean;
  outcome: string;
  status_code: number | null;
  error_class: string | null;
  duration_ms: number | null;
  ttfb_ms: number | null;
  failover_count: number;
  started_at: string;
  finished_at: string | null;
  attempts?: Attempt[];
}

export interface RequestAggregate {
  total: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  active: number;
  average_duration_ms: number | null;
}

export interface RequestPage extends Page<ProxyRequest> {
  aggregate: RequestAggregate;
}

export interface ProbeRun {
  id: string;
  kind: string;
  upstream_id: string | null;
  profile_id: string | null;
  status: string;
  status_code: number | null;
  latency_ms: number | null;
  error_class: string | null;
  billable: boolean;
  requested_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface IncidentUpdate {
  id: string;
  status: string;
  public_message: string;
  published_at: string;
}

export interface Incident {
  id: string;
  slug: string;
  title: string;
  status: string;
  severity: string;
  public: boolean;
  started_at: string;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  updates?: IncidentUpdate[];
}

export interface AuditEvent {
  id: string;
  action: string;
  resource_kind: string;
  resource_id: string | null;
  actor_kind: string;
  request_id: string | null;
  outcome: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface QaCase {
  id: string;
  name: string;
  status: string;
  evidence: Record<string, string | number | boolean | null>;
  started_at: string | null;
  finished_at: string | null;
}

export interface QaRun {
  id: string;
  suite: string;
  live: boolean;
  deployment_commit: string;
  status: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cases: QaCase[];
}

export interface QaRunsPage extends Page<QaRun> {
  deployment_commit: string;
  completion: Array<{
    suite: string;
    passed: boolean;
    passed_run: QaRun | null;
    latest_run: QaRun | null;
  }>;
}

export interface RoutingPolicy {
  version: number;
  active: boolean;
  retryable_statuses: number[];
  default_cooldown_seconds: number;
  stream_failover_before_first_frame_only: boolean;
  generation_retry: boolean;
}

export interface Settings {
  proof_freshness_seconds: number;
  request_retention_days: number;
  metric_retention_days: number;
  public_incidents_enabled: boolean;
}
