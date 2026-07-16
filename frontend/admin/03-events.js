function eventResource(item) {
  if (item.upstream_key_fingerprint) return fingerprintHandle(item.upstream_key_fingerprint);
  if (item.upstream_key_id) {
    const upstream = upstreamItems.find((candidate) => candidate.id === item.upstream_key_id);
    return upstream
      ? keyHandle(upstream)
      : upstreamHandleHistory.get(item.upstream_key_id) ?? `Legacy deleted key · ID ${shortId(item.upstream_key_id)}`;
  }
  if (item.downstream_token_id) {
    const downstream = downstreamItems.find((candidate) => candidate.id === item.downstream_token_id);
    return downstream
      ? tokenHandle(downstream)
      : downstreamHandleHistory.get(item.downstream_token_id) ?? `Removed client · ID ${shortId(item.downstream_token_id)}`;
  }
  return "The gateway";
}

function eventCondition(item) {
  if (!item.status_class || item.status_class === "success") return "";
  return ` · ${healthCopy[item.status_class] ?? humanize(item.status_class)}`;
}

function attemptLinkEvidence(item, eventIds, terminalByStart) {
  if (!["upstream_attempt", "upstream_probe"].includes(item.event_type)) return "Not an attempt event";
  if (item.outcome_class === "started") {
    const terminal = terminalByStart.get(item.id);
    return terminal
      ? `Linked terminal ${terminal.id}`
      : "No linked terminal in this snapshot window";
  }
  if (item.attempt_started_event_id === null) return "Legacy unlinked terminal";
  return eventIds.has(item.attempt_started_event_id)
    ? `Exact start ${item.attempt_started_event_id}`
    : `Start outside recent window · ${item.attempt_started_event_id}`;
}

function eventSummary(item, reference, current, eventIds, terminalByStart) {
  const subject = eventResource(item);
  const link = attemptLinkEvidence(item, eventIds, terminalByStart);
  const unconfirmedStart = item.outcome_class === "started" && !terminalByStart.has(item.id);
  const actions = {
    upstream_key_created: `${subject} was added disabled`,
    upstream_key_enabled: `${subject} was enabled`,
    upstream_key_disabled: `${subject} was disabled`,
    upstream_key_deleted: `${subject} was deleted`,
    upstream_probe: item.outcome_class === "started"
      ? unconfirmedStart
        ? `${subject} operator probe completion unconfirmed · Do not retry from this view`
        : `${subject} operator probe · ${link}`
      : `${subject} operator probe ${item.outcome_class}`,
    downstream_token_issued: `A token was issued for ${subject}`,
    downstream_token_revoked: `The token for ${subject} was revoked`,
    upstream_attempt: item.outcome_class === "started"
      ? unconfirmedStart
        ? `${subject} request completion unconfirmed · Do not retry from this view`
        : `${subject} request · ${link}`
      : `${subject} request ${item.outcome_class}`,
  };
  const observed = current ? relativeTime(item.occurred_at, reference) : `Observed ${item.occurred_at}`;
  return `${actions[item.event_type]}${eventCondition(item)} · ${observed}`;
}

function attentionEvents(items) {
  const attemptTypes = new Set(["upstream_attempt", "upstream_probe"]);
  const linkedStartIds = new Set(items
    .filter((item) => attemptTypes.has(item.event_type) && item.attempt_started_event_id !== null)
    .map((item) => item.attempt_started_event_id));
  return items.filter((item) => {
    if (!attemptTypes.has(item.event_type)) return true;
    if (item.event_type === "upstream_attempt" && item.outcome_class === "succeeded") return false;
    if (item.outcome_class === "started" && linkedStartIds.has(item.id)) return false;
    return true;
  }).slice(0, 5);
}

function unconfirmedProbeTargets(items) {
  const linkedStartIds = new Set(items
    .filter((item) => item.attempt_started_event_id !== null)
    .map((item) => item.attempt_started_event_id));
  return new Set(items
    .filter((item) => item.event_type === "upstream_probe"
      && item.outcome_class === "started"
      && !linkedStartIds.has(item.id)
      && item.upstream_key_id !== null)
    .map((item) => item.upstream_key_id));
}

function renderMaintenanceEvidence(summary, ledger) {
  if (ledger.status !== "maintenance_overdue") return;
  const note = document.createElement("p");
  note.className = "metadata";
  note.textContent = snapshotCurrent
    ? "Maintenance evidence is overdue. Capacity remains available and the next cleanup retry is automatic."
    : "Last confirmed maintenance evidence was overdue. Current capacity and the next cleanup retry are unconfirmed until Refresh succeeds.";
  summary.prepend(note);
}

function renderEvents(items, reference, ledger) {
  const statePrefix = snapshotCurrent ? "" : "Last confirmed · ";
  const noteworthy = attentionEvents(items);
  const summary = byId("activity-summary");
  const eventIds = new Set(items.map((item) => item.id));
  const terminalByStart = new Map(items
    .filter((item) => item.attempt_started_event_id !== null)
    .map((item) => [item.attempt_started_event_id, item]));
  if (!items.length) {
    summary.textContent = `${statePrefix}No recent activity needs attention.`;
    renderMaintenanceEvidence(summary, ledger);
    tableMessage("events-body", 5, `${statePrefix}No recent events were observed.`);
    setText("audit-summary", "Audit details · 0 events");
    return;
  }
  if (!noteworthy.length) {
    summary.textContent = `${statePrefix}No recent exceptions or configuration changes. Routine evidence is available below.`;
  } else {
    const list = document.createElement("ul");
    list.className = "activity-list";
    for (const item of noteworthy) {
      const entry = document.createElement("li");
      entry.textContent = `${statePrefix}${eventSummary(item, reference, snapshotCurrent, eventIds, terminalByStart)}`;
      list.append(entry);
    }
    summary.replaceChildren(list);
  }
  renderMaintenanceEvidence(summary, ledger);
  const rows = items.map((item) => {
    const row = document.createElement("tr");
    const link = attemptLinkEvidence(item, eventIds, terminalByStart);
    row.append(
      createRowHeader("Event", `${item.event_type} · ${item.id}`, "machine"),
      createCell("Request", item.request_id, "machine"),
      createCell("Resource", eventResource(item), "machine"),
      createCell("Outcome", `${item.outcome_class} · ${item.status_class ?? "No status"} · ${item.latency_ms ?? "No latency"} · ${link}`, "machine"),
      createCell("Observed", item.occurred_at, "machine"),
    );
    return row;
  });
  byId("events-body").replaceChildren(...rows);
  setText("audit-summary", `${snapshotCurrent ? "Audit details" : "Stale audit details"} · ${items.length} ${items.length === 1 ? "event" : "events"}`);
}
