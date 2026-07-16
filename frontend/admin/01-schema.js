const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const timestampPattern = /^((?!0000)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(?:\.(\d{1,6}))?Z$/;
const oneOf = (...values) => (value) => values.includes(value);
const nullable = (check) => (value) => value === null || check(value);
const text = (value) => typeof value === "string" && value.length > 0;
const counter = (value) => Number.isSafeInteger(value) && value >= 0 || typeof value === "bigint" && value >= 0n && value <= 9223372036854775807n;
const uuid = (value) => typeof value === "string" && uuidPattern.test(value);
function utcTimestampInstant(value) {
  const parsed = typeof value === "string" ? timestampPattern.exec(value) : null;
  if (!parsed) return null;
  const milliseconds = Date.parse(`${parsed[1]}Z`);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString().slice(0, 19) !== parsed[1]) return null;
  const microseconds = BigInt((parsed[2] || "").padEnd(6, "0") || "0");
  return (BigInt(milliseconds) * 1000n) + microseconds;
}
const timestamp = (value) => utcTimestampInstant(value) !== null;
const fingerprint = (value) => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value);
const token = (value) => typeof value === "string" && /^nblb_ds_[0-9a-f]{64}$/.test(value);
const label = (value) => text(value) && [...value].length <= 128 && value === value.trim() && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value);
const scopeSets = [["models:read"], ["chat:write"], ["models:read", "chat:write"]];
const scopes = (value) => Array.isArray(value) && scopeSets.some((expected) => expected.length === value.length && expected.every((scope, index) => scope === value[index]));
const healthState = oneOf("unknown", "healthy", "degraded");
const routingState = oneOf("disabled", "eligible", "cooldown", "quarantined");
const statusClass = oneOf("success", "invalid_credential", "credits_exhausted", "rate_limited", "request_rejected", "timeout", "upstream_unavailable", "upstream_bad_gateway", "upstream_internal_error", "upstream_protocol_error", "delivery_failed", "cancelled");
const safeCode = oneOf("host_forbidden", "origin_forbidden", "unauthorized", "admin_unauthorized", "insufficient_scope", "model_not_found", "resource_not_found", "resource_conflict", "invalid_request", "internal_server_error", "no_upstream_keys", "database_unavailable", "upstream_auth_error", "upstream_credits_exhausted", "upstream_timeout", "upstream_rate_limited", "upstream_request_rejected", "upstream_internal_error", "upstream_bad_gateway", "upstream_unavailable", "upstream_protocol_error", "poll_timeout", "admin_read_timeout", "admin_mutation_timeout", "admin_mutation_response_invalid", "admin_mutation_settling", "runtime_unavailable", "ledger_capacity_exhausted");

function matches(schema, value) {
  if (typeof schema === "function") return schema(value);
  if (Array.isArray(schema)) return Array.isArray(value) && value.every((item) => matches(schema[0], item));
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(schema);
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key) && matches(schema[key], value[key]));
}

const upstreamDto = {id: uuid, fingerprint, enabled: (value) => typeof value === "boolean", routing_state: routingState, health_state: healthState, cooldown_until: nullable(timestamp), request_count: counter, success_count: counter, failure_count: counter, last_status_class: nullable(statusClass), last_used_at: nullable(timestamp), created_at: timestamp, updated_at: timestamp};
const downstreamDto = {id: uuid, label, scopes, revoked_at: nullable(timestamp), request_count: counter, last_used_at: nullable(timestamp), created_at: timestamp};
const eventDto = {id: uuid, request_id: text, event_type: oneOf("upstream_key_created", "upstream_key_enabled", "upstream_key_disabled", "upstream_key_deleted", "upstream_probe", "downstream_token_issued", "downstream_token_revoked", "upstream_attempt"), upstream_key_id: nullable(uuid), downstream_token_id: nullable(uuid), outcome_class: oneOf("started", "succeeded", "failed", "cancelled"), status_class: nullable(statusClass), latency_ms: nullable(counter), occurred_at: timestamp};
const dashboardEventDto = {...eventDto, upstream_key_fingerprint: nullable(fingerprint), attempt_started_event_id: nullable(uuid)};
const overviewDto = {status: oneOf("ok", "degraded"), ready: (value) => typeof value === "boolean", upstream_keys: {total: counter, enabled: counter, eligible: counter, cooling: counter, degraded: counter}, downstream_tokens: {total: counter, active: counter, revoked: counter}, request_count: counter, last_event_at: nullable(timestamp), generated_at: timestamp};
const ledgerDto = {status: oneOf("ok", "maintenance_overdue", "capacity_exhausted_recovering", "capacity_blocked"), capacity_blocker: oneOf("none", "active_attempts", "reconciliation_grace", "lock_contention", "orphaned_pending", "legacy_unlinked"), event_rows: counter, reserved_terminal_slots: counter, event_capacity: counter, attempt_rows: counter, attempt_capacity: counter, last_maintenance_completed_at: nullable(timestamp), last_pruned_event_rows: counter, last_pruned_attempt_rows: counter, oldest_event_at: nullable(timestamp)};
const dashboardDto = {runtime_state: oneOf("operational", "unavailable"), readiness_cause: oneOf("ready", "runtime_unavailable", "ledger_capacity_exhausted", "no_eligible_upstream"), ledger: ledgerDto, overview: overviewDto, upstream_keys: {items: [upstreamDto]}, downstream_tokens: {items: [downstreamDto]}, events: {items: [dashboardEventDto]}};
const errorDto = {code: safeCode, message: text, request_id: text};
const dtoSchemas = {dashboard: (value) => matches(dashboardDto, value) && value.events.items.length <= 100, overview: overviewDto, upstream: upstreamDto, upstreamList: {items: [upstreamDto]}, downstreamList: {items: [downstreamDto]}, eventList: (value) => matches({items: [eventDto]}, value) && value.items.length <= 100, probe: {id: uuid, enabled: (value) => typeof value === "boolean", probe_status: oneOf("valid", "invalid_credential", "rate_limited", "upstream_unavailable"), observed_at: timestamp}, issued: {...downstreamDto, token}, error: {error: errorDto}, validationError: {error: {code: (value) => value === "invalid_request", message: (value) => value === "request validation failed", request_id: text}}};

function parseAdminDto(kind, value) {
  if (!Object.hasOwn(dtoSchemas, kind) || !matches(dtoSchemas[kind], value)) throw new TypeError("invalid administration response");
  return value;
}

class AdminResponseError extends Error {}

function parseAdminResponse(kind, source) {
  try {
    return parseAdminDto(kind, parseJson(source));
  } catch (error) {
    if (!(error instanceof TypeError || error instanceof SyntaxError)) throw error;
    throw new AdminResponseError("invalid administration response", {cause: error});
  }
}

function parseJson(source) {
  return JSON.parse(source, (key, value, context) => {
    if (typeof value !== "number") return value;
    if (!/^(?:0|[1-9]\d*)$/.test(context.source)) throw new TypeError("invalid administration counter");
    return BigInt(context.source) <= BigInt(Number.MAX_SAFE_INTEGER) ? value : BigInt(context.source);
  });
}
