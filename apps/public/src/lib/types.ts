export type Snapshot = {
  observed_at: string;
  generated_at: string;
  stale: boolean;
};

export type PublicMetric = {
  sample_count: number;
  success_rate: number | null;
  failover_rate: number | null;
  cancellation_rate: number | null;
  latency_p95_ms: number | null;
  ttfb_p95_ms: number | null;
  eligible_provider_count: number | null;
};

export type PublicSummary = {
  schema_version: "public.v1";
  snapshot: Snapshot;
  state: {
    status: "operational" | "degraded" | "maintenance" | "unknown";
    traffic_ready: boolean;
    reason_code: string | null;
  };
  capacity: { eligible: number; target: 2 };
  metrics_24h: PublicMetric;
  endpoints: Array<{
    kind: string;
    state: "verified" | "proof_required" | "unavailable" | "maintenance";
  }>;
};

export type PublicMetricPoint = PublicMetric & { at: string };

export type PublicMetrics = {
  schema_version: "public.v1";
  snapshot: Snapshot;
  window: string;
  step: string;
  points: PublicMetricPoint[];
};

export type PublicModel = {
  id: string;
  endpoint: string;
  input_modalities: string[];
  output_modalities: string[];
  streaming: boolean;
  tool_calling: boolean;
  advertised: boolean;
  proof_status: "pair_verified" | "provider_verified" | "proof_required" | "unavailable";
  available_now: boolean;
  verified_age_seconds: number | null;
};

export type PublicModels = {
  schema_version: "public.v1";
  snapshot: Snapshot;
  items: PublicModel[];
};

export type PublicIncident = {
  slug: string;
  title: string;
  status: "investigating" | "identified" | "monitoring" | "resolved";
  severity: "minor" | "major" | "critical";
  started_at: string;
  resolved_at: string | null;
  updates: Array<{ status: string; message: string; published_at: string }>;
};

export type PublicIncidents = {
  schema_version: "public.v1";
  snapshot: Snapshot;
  items: PublicIncident[];
};

export type PublicIncidentDetail = {
  schema_version: "public.v1";
  snapshot: Snapshot;
  item: PublicIncident;
};
