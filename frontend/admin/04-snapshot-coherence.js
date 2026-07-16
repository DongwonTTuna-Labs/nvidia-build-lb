function countersEqual(actual, expected) { return String(actual) === String(expected); }
function counterBigInt(value) { return BigInt(String(value)); }

function ledgerIsCoherent(ledger) {
  const transientBlockers = new Set(["none", "active_attempts", "reconciliation_grace", "lock_contention"]);
  const permanentBlockers = new Set(["orphaned_pending", "legacy_unlinked"]);
  if (["ok", "maintenance_overdue"].includes(ledger.status) && ledger.capacity_blocker !== "none") return false;
  if (ledger.status === "capacity_exhausted_recovering" && !transientBlockers.has(ledger.capacity_blocker)) return false;
  if (ledger.status === "capacity_blocked" && !permanentBlockers.has(ledger.capacity_blocker)) return false;
  const eventAvailable = counterBigInt(ledger.event_rows) + counterBigInt(ledger.reserved_terminal_slots) + 2n <= counterBigInt(ledger.event_capacity);
  const attemptAvailable = counterBigInt(ledger.attempt_rows) + 1n <= counterBigInt(ledger.attempt_capacity);
  if (ledger.status === "capacity_blocked") return true;
  return (ledger.status === "capacity_exhausted_recovering") === !(eventAvailable && attemptAvailable);
}

function dashboardEventsAreCoherent(events, overview) {
  const newest = events.length ? events[0].occurred_at : null;
  if (newest !== overview.last_event_at) return false;
  const attemptTypes = new Set(["upstream_attempt", "upstream_probe"]);
  const eventsById = new Map(events.map((item) => [item.id, item]));
  const linkedStartIds = new Set();
  for (let index = 1; index < events.length; index += 1) {
    const previous = events[index - 1];
    const current = events[index];
    const previousInstant = utcTimestampInstant(previous.occurred_at);
    const currentInstant = utcTimestampInstant(current.occurred_at);
    if (previousInstant === null || currentInstant === null) return false;
    if (previousInstant < currentInstant) return false;
    if (previousInstant === currentInstant && previous.id < current.id) return false;
  }
  for (const item of events) {
    const startedId = item.attempt_started_event_id;
    if (item.outcome_class === "started" && startedId !== null) return false;
    if (!attemptTypes.has(item.event_type) && startedId !== null) return false;
    if (startedId === null) continue;
    if (linkedStartIds.has(startedId)) return false;
    linkedStartIds.add(startedId);
    const started = eventsById.get(startedId);
    if (started && (started.outcome_class !== "started" || started.event_type !== item.event_type)) return false;
  }
  return true;
}

function dashboardReadinessIsCoherent(dashboard) {
  const {overview, ledger, readiness_cause: cause, runtime_state: runtime} = dashboard;
  if (overview.ready !== (cause === "ready")) return false;
  if ((overview.status === "ok") !== overview.ready) return false;
  if ((runtime === "unavailable") !== (cause === "runtime_unavailable")) return false;
  const capacityBlocked = ["capacity_exhausted_recovering", "capacity_blocked"].includes(ledger.status);
  if (runtime === "operational" && capacityBlocked !== (cause === "ledger_capacity_exhausted")) return false;
  if (cause === "no_eligible_upstream" && overview.upstream_keys.eligible !== 0) return false;
  return cause !== "ready" || overview.upstream_keys.eligible > 0;
}

function snapshotIsCoherent(dashboard) {
  const {overview, upstream_keys: upstreams, downstream_tokens: downstreams, events, ledger} = dashboard;
  const upstreamCounts = {
    total: upstreams.items.length,
    enabled: upstreams.items.filter((item) => item.enabled).length,
    eligible: upstreams.items.filter((item) => item.routing_state === "eligible").length,
    cooling: upstreams.items.filter((item) => item.routing_state === "cooldown").length,
    degraded: upstreams.items.filter((item) => item.health_state === "degraded").length,
  };
  const revoked = downstreams.items.filter((item) => item.revoked_at !== null).length;
  const downstreamCounts = {total: downstreams.items.length, active: downstreams.items.length - revoked, revoked};
  if (Object.entries(upstreamCounts).some(([key, value]) => !countersEqual(overview.upstream_keys[key], value))) return false;
  if (Object.entries(downstreamCounts).some(([key, value]) => !countersEqual(overview.downstream_tokens[key], value))) return false;
  return ledgerIsCoherent(ledger)
    && dashboardEventsAreCoherent(events.items, overview)
    && dashboardReadinessIsCoherent(dashboard);
}
