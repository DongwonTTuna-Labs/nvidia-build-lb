import type { AdminRouteId } from "$lib/copy";

export type SnapshotState =
  | "loading"
  | "empty"
  | "ready"
  | "degraded"
  | "stale"
  | "offline"
  | "error"
  | "success"
  | "partial"
  | "conflict"
  | "recovery";

export type MutationState = "idle" | "pending" | "unknown" | "reconcile";

export type Key = {
  id: string;
  label: string;
  fingerprint: string;
  enabled: boolean;
  verified: boolean;
  cooldown_until: string | null;
  request_count: number;
  failure_count: number;
};

export type Client = {
  id: string;
  label: string;
  scopes: string[];
  active: boolean;
  request_count: number;
  revoked_at: string | null;
};

export type Evidence = {
  source_of_truth: string;
  persisted_upstream_keys: number;
  persisted_downstream_credentials: number;
  persisted_routing_profiles: number;
  persisted_request_attempts?: number;
};

export type Attention = {
  id?: string;
  resource?: { kind?: string; id?: string };
  code: string;
  label?: string;
  next_action: string;
  expires_at?: string | null;
};

export type Check = {
  id: string;
  label: string;
  status: string;
  request_count: number;
  failure_count: number;
};

export type Recommendation = {
  action: string;
  label: string;
  route: AdminRouteId;
  reason: string;
};

export type PublicHealth = { hostname: string; status: string; next_action: string };

export type ProfileCapability = {
  id: string;
  route: string;
  advertised: boolean;
  available_now: boolean;
  proof_status?: string;
  modalities: string[];
};

export type SlotProfile = { profile_id: string; eligible_now: boolean; reason: string | null };

export type SlotProjection = { slot_no: number; key_id: string; profiles: SlotProfile[] };

export type AdminEvent = {
  id: string;
  kind?: string;
  outcome?: string;
  created_at?: string;
  profile_id?: string;
  request_id?: string;
  key_id?: string;
};
