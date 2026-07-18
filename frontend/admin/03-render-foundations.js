const scopeCopy = {"models:read": "Read models", "chat:write": "Write chat"};
function accessLabel(scopes) { return scopes.map((scope) => scopeCopy[scope]).join(" · "); }
const routingCopy = {
  disabled: ["Disabled", "Not selected for new requests", "disabled"],
  eligible: ["Eligible", "Available for new requests", "healthy"],
  cooldown: ["Cooling", "Temporarily excluded", "cooldown"],
  quarantined: ["Quarantined", "Excluded until a successful recovery probe", "error"],
};
const healthCopy = {
  unknown: "Not verified · Probe required",
  success: "Verified · Last check succeeded",
  invalid_credential: "Credential rejected · Replacement required",
  credits_exhausted: "Credits exhausted · Restore credits before probing",
  rate_limited: "Rate limited · Waiting for cooldown",
  request_rejected: "Request rejected · Review request eligibility",
  timeout: "Timed out · Recovery probe available",
  upstream_unavailable: "Upstream unavailable · Recovery probe available",
  upstream_bad_gateway: "Upstream gateway failed · Recovery probe available",
  upstream_internal_error: "Upstream failed · Recovery probe available",
  upstream_protocol_error: "Protocol failed · Recovery probe available",
  delivery_failed: "Delivery failed · Recovery probe available",
  cancelled: "Attempt cancelled · Recovery probe available",
};
function renderOverview(snapshot, reference) {
  const {overview, readinessCause} = snapshot;
  const currentReadiness = {
    ready: "Ready for requests · Current traffic not measured",
    runtime_unavailable: "Not ready · Local runtime unavailable",
    ledger_capacity_exhausted: "Not ready · New requests and changes paused",
    no_eligible_upstream: "Not ready · No eligible key",
  }[readinessCause];
  const readiness = snapshotCurrent ? currentReadiness : "Stale · Readiness requires refresh";
  const valuePrefix = snapshotCurrent ? "" : "Last confirmed · ";
  setText("gateway-status", readiness);
  setText("eligible-count", `${valuePrefix}${overview.upstream_keys.total} registered · ${overview.upstream_keys.eligible} eligible`);
  setText("cooling-count", `${valuePrefix}${overview.upstream_keys.cooling} temporarily excluded`);
  setText("request-count", `${valuePrefix}${overview.request_count} observed`);
  setText("active-token-count", snapshotCurrent ? String(overview.downstream_tokens.active) : `Last confirmed: ${overview.downstream_tokens.active}`);
  setText("last-event-at", snapshotCurrent
    ? `Current · ${relativeTime(overview.last_event_at, reference)}`
    : "Stale · Last event freshness requires refresh");
  setText("generated-at", snapshotCurrent ? "Current server-confirmed snapshot" : "Snapshot requires refresh");
  setText("snapshot-evidence", `Generated ${overview.generated_at} · Last event ${displayTime(overview.last_event_at)} · Runtime ${snapshot.runtimeState} · Readiness ${snapshot.readinessCause} · Ledger ${snapshot.ledger.status}/${snapshot.ledger.capacity_blocker} · Events ${snapshot.ledger.event_rows}/${snapshot.ledger.event_capacity} with ${snapshot.ledger.reserved_terminal_slots} terminal slots reserved · Attempts ${snapshot.ledger.attempt_rows}/${snapshot.ledger.attempt_capacity} · Last maintenance ${displayTime(snapshot.ledger.last_maintenance_completed_at)}`);
}
