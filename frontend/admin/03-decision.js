const mutationRecommendations = new Set(["add", "probe", "enable", "issue"]);
function setRecommendation(kind, labelText, targetId = null, targetAction = null) {
  const button = byId("recommended-action");
  const globalRecovery = kind === "refresh" && !byId("global-error").hidden;
  recommendedPlan = {kind, targetId, targetAction};
  button.hidden = kind === "none" || globalRecovery;
  button.textContent = labelText;
  button.removeAttribute("data-mutation");
  if (mutationRecommendations.has(kind)) button.dataset.mutation = "true";
  if (!mutationRecommendations.has(kind)) button.disabled = false;
  byId("retry-dashboard").hidden = !globalRecovery;
  byId("refresh-dashboard").hidden = kind === "refresh";
  refreshMutationLocks();
}
function setControlPrerequisite(controlId, reasonId, message) {
  const button = byId(controlId);
  const reason = byId(reasonId);
  if (message) {
    button.dataset.prerequisite = "blocked";
    button.setAttribute("aria-describedby", reason.id);
    reason.textContent = message;
    reason.hidden = false;
  } else {
    delete button.dataset.prerequisite;
    button.removeAttribute("aria-describedby");
    reason.textContent = "";
    reason.hidden = true;
  }
  refreshMutationLocks();
}
function setAddPrerequisite(snapshot) {
  let message = "";
  if (!snapshotCurrent) message = "Current state is unknown. Refresh before adding an upstream key.";
  else if (snapshot.readinessCause === "runtime_unavailable") message = "Add is locked until the local runtime is restored.";
  else if (snapshot.readinessCause === "ledger_capacity_exhausted") message = "Add is locked while ledger capacity blocks new requests and changes.";
  else if (snapshot.upstreams.length > 2) message = "Return to exactly two registered upstream keys before adding another key.";
  setControlPrerequisite("add-upstream", "add-upstream-reason", message);
}
function setIssuePrerequisite(snapshot) {
  let message = "";
  if (!snapshotCurrent) message = "Current state is unknown. Refresh before issuing a downstream token.";
  else if (orphanedToken) message = "Revoke the credential whose one-time response was lost before issuing a replacement.";
  else if (snapshot.readinessCause === "runtime_unavailable") message = "Issue is locked until the local runtime is restored.";
  else if (snapshot.readinessCause === "ledger_capacity_exhausted") message = "Issue is locked while ledger capacity blocks new requests and changes.";
  else if (snapshot.upstreams.length > 2) message = "Return to exactly two registered upstream keys before issuing a downstream token.";
  else if (snapshot.overview.downstream_tokens.total === 0
    && (snapshot.upstreams.length !== 2 || snapshot.overview.upstream_keys.eligible !== 2)) message = "The first client credential is available after exactly two registered keys are verified, enabled, and eligible.";
  else if (snapshot.overview.upstream_keys.eligible === 0) message = "Issue is available after at least one verified key is enabled and eligible.";
  setControlPrerequisite("issue-downstream", "issue-downstream-reason", message);
}
function renderUnavailableDecision() {
  byId("ledger-recovery").hidden = true;
  byId("ledger-recovery").open = false;
  setText("decision-state", "Unavailable · Current state could not be confirmed");
  setText("decision-title", "Refresh current state");
  setText("decision-explanation", "Administration state could not be verified. Changes stay locked until Refresh succeeds.");
  setText("generated-at", "Current snapshot unavailable");
  setText("snapshot-evidence", "No complete snapshot is available.");
  setText("gateway-status", "Unavailable");
  setText("eligible-count", "Not available");
  setText("cooling-count", "Not available");
  setText("request-count", "Not available");
  setText("active-token-count", "Not available");
  setText("last-event-at", "Not available");
  tableMessage("upstream-body", 4, "Unavailable. Refresh current state to list upstream keys.");
  tableMessage("downstream-body", 4, "Unavailable. Refresh current state to list downstream tokens.");
  setText("activity-summary", "Recent activity is unavailable until refresh succeeds.");
  tableMessage("events-body", 5, "Unavailable. Refresh current state to load recent events.");
  setText("audit-summary", "Audit details · unavailable");
  setControlPrerequisite("add-upstream", "add-upstream-reason", "Add is locked until current state is refreshed.");
  setControlPrerequisite("issue-downstream", "issue-downstream-reason", "Issue is locked until current state is refreshed.");
  setRecommendation("refresh", "Refresh current state");
}
function excludedState(overview) {
  return overview.upstream_keys.eligible > 0
    ? `Reduced capacity · ${overview.upstream_keys.eligible} eligible ${overview.upstream_keys.eligible === 1 ? "key remains" : "keys remain"}`
    : "Blocked · No eligible upstream key";
}
function recommendCredentialRecovery(item, overview, upstreams) {
  const handle = keyHandle(item);
  setText("decision-state", excludedState(overview));
  if (item.last_status_class === "invalid_credential") {
    if (overview.upstream_keys.eligible > 0) {
      const action = item.enabled ? "disable" : "delete";
      setText("decision-title", `Retire rejected ${handle}`);
      setText("decision-explanation", `${handle} was rejected by NVIDIA, and another key is eligible. ${item.enabled ? "Disable it before permanent deletion." : "Delete it to finish the replacement journey."}`);
      setRecommendation("review", `Review ${handle}`, item.id, action);
      return;
    }
    setText("decision-title", `Replace ${handle}`);
    setText("decision-explanation", `${handle} was rejected by NVIDIA and remains excluded. Add a replacement, then probe and enable it.`);
    setRecommendation("add", `Add replacement for ${handle}`, item.id);
    return;
  }
  if (item.last_status_class === "credits_exhausted") {
    setText("decision-title", `Restore credits for ${handle}`);
    setText("decision-explanation", `${handle} remains excluded. Restore NVIDIA credits first, then run one recovery probe.`);
    setRecommendation("probe", `Probe ${handle} after restoring credits`, item.id);
    return;
  }
  setText("decision-title", `Probe ${handle}`);
  setText("decision-explanation", `${handle} is excluded after ${healthCopy[item.last_status_class] ?? "an unverified condition"}. A successful recovery probe is required.`);
  setRecommendation("probe", `Probe ${handle}`, item.id);
}

function recommendCoolingKey(cooling, overview, reference) {
  const until = remainingTime(cooling.cooldown_until, reference);
  setText("decision-state", excludedState(overview));
  if (!cooling.enabled) {
    setText("decision-title", `Wait before probing ${keyHandle(cooling)}`);
    setText("decision-explanation", `${keyHandle(cooling)} is disabled and cooling ${until}. It will remain disabled when cooldown ends. Refresh then, probe it, and enable only after a successful result.`);
  } else {
    setText("decision-title", `Wait for ${keyHandle(cooling)} cooldown`);
    setText("decision-explanation", `${keyHandle(cooling)} is temporarily excluded and should become eligible automatically ${until}. No action is needed before then.`);
  }
  setRecommendation("none", "");
}
function reconcileReplacementContext(upstreams) {
  if (replacementContext && (
    !upstreams.some((item) => item.id === replacementContext.sourceId)
    || !upstreams.some((item) => item.id === replacementContext.replacementId)
  )) replacementContext = null;
}
function renderDecision(snapshot, reference) {
  const {overview, upstreams, events} = snapshot;
  byId("ledger-recovery").hidden = true;
  byId("ledger-recovery").open = false;
  reconcileLocalIntent(upstreams, events);
  renderOperationSummary();
  setAddPrerequisite(snapshot);
  setIssuePrerequisite(snapshot);
  if (!snapshotCurrent) {
    setText("decision-state", "Stale · Current state is unknown");
    setText("decision-title", "Refresh before making changes");
    setText("decision-explanation", `${snapshotStaleReason ?? "The last verified values may be out of date."} Changes stay locked until Refresh confirms current state.`);
    setRecommendation("refresh", "Refresh current state");
    return;
  }
  if (snapshot.readinessCause === "runtime_unavailable") {
    const keyNoun = overview.upstream_keys.eligible === 1 ? "key is" : "keys are";
    setText("decision-state", "Outage · Gateway cannot accept requests");
    setText("decision-title", "Restore the local service");
    setText("decision-explanation", `${overview.upstream_keys.eligible} upstream ${keyNoun} eligible, but the gateway itself is unavailable. Restore the service, then refresh.`);
    setRecommendation("refresh", "Refresh after service recovery");
    return;
  }
  if (snapshot.readinessCause === "ledger_capacity_exhausted") {
    if (snapshot.ledger.status === "capacity_blocked") {
      const permanentCause = snapshot.ledger.capacity_blocker === "orphaned_pending"
        ? "An unfinished attempt has no live owner evidence."
        : "Legacy attempt evidence cannot be linked safely.";
      setText("decision-state", "Blocked · Retained evidence cannot be discarded safely");
      setText("decision-title", "Use the forward recovery procedure");
      setText("decision-explanation", `${permanentCause} Preserve a paired backup, keep the gateway withdrawn, and deploy a reviewed forward repair that preserves receipt, event, and pin evidence.`);
      byId("ledger-recovery").hidden = false;
      setRecommendation("recovery", "View recovery steps");
      return;
    }
    setText("decision-state", "Paused · Protected work or cleanup is still settling");
    setText("decision-title", "Wait for cleanup, then refresh evidence");
    const settlingReason = snapshot.ledger.capacity_blocker === "none"
      ? "the first automatic maintenance assessment"
      : humanize(snapshot.ledger.capacity_blocker).toLowerCase();
    setText("decision-explanation", `New requests and changes are paused pending ${settlingReason}. Protected work must settle or cleanup must retry before capacity can reopen.`);
    setRecommendation("refresh", "Refresh settlement evidence");
    return;
  }
  const unconfirmedProbe = upstreams.find((item) => unconfirmedProbeKeyIds.has(item.id));
  if (unconfirmedProbe) {
    setText("decision-state", `Pending confirmation · ${keyHandle(unconfirmedProbe)} operator probe`);
    setText("decision-title", "Wait for probe completion");
    setText("decision-explanation", `${keyHandle(unconfirmedProbe)} has a probe start with no confirmed terminal. Do not repeat or change this key; wait for it to settle, then refresh current state.`);
    setRecommendation("refresh", "Refresh probe status");
    return;
  }
  if (orphanedToken) {
    setText("decision-state", "Action required · Credential response was lost");
    setText("decision-title", `Review unrecoverable token for ${tokenHandle(orphanedToken)}`);
    setText("decision-explanation", "The server created this token but its one-time bearer was not received. Revoke it before issuing a replacement.");
    setRecommendation("review", `Review token for ${tokenHandle(orphanedToken)}`, orphanedToken.id, "revoke");
    return;
  }
  if (!upstreams.length) {
    setText("decision-state", "Setup · No upstream key");
    setText("decision-title", "Add the first upstream key");
    setText("decision-explanation", "The key will be encrypted and remain disabled until its probe succeeds.");
    setRecommendation("add", "Add first key");
    return;
  }
  if (upstreams.length === 2 && overview.upstream_keys.eligible === 2
    && overview.downstream_tokens.active === 0 && overview.downstream_tokens.total === 0) {
    setText("decision-state", "Setup · Gateway is ready");
    setText("decision-title", "Issue a downstream token");
    setText("decision-explanation", "Create one client credential with only the scopes that client needs.");
    setRecommendation("issue", "Issue downstream token");
    return;
  }
  if (upstreams.length > 2 && !replacementContext) {
    setText("decision-state", `Cleanup required · ${upstreams.length} registered keys`);
    setText("decision-title", "Return to exactly two upstream keys");
    setText("decision-explanation", "No new client credential can be issued in this state. Review the safe IDs and fingerprints, then disable and delete one unintended extra key.");
    setRecommendation("upstreams", "Review extra upstream keys");
    return;
  }
  if (recommendExcludedKey(upstreams, overview, reference)) return;
  if (overview.upstream_keys.eligible === 0) {
    setText("decision-state", "Paused · No eligible upstream key");
    setText("decision-title", "No automatic action");
    setText("decision-explanation", "All registered keys are disabled. This may be intentional; use a row action only when routing should resume.");
    setRecommendation("none", "");
    return;
  }
  if (upstreams.length === 1 && overview.upstream_keys.eligible === 1) {
    setText("decision-state", "Reduced resilience · One eligible key");
    setText("decision-title", "Add the second upstream key");
    setText("decision-explanation", "Requests can be accepted, but a second verified key is required for round-robin failover.");
    setRecommendation("add", "Add second key");
    return;
  }
  setText("decision-state", "Available · Ready for requests");
  setText("decision-title", "No action required");
  const keyNoun = overview.upstream_keys.eligible === 1 ? "upstream key is" : "upstream keys are";
  const clientNoun = overview.downstream_tokens.active === 1 ? "client credential is" : "client credentials are";
  setText("decision-explanation", `${overview.upstream_keys.eligible} ${keyNoun} eligible and ${overview.downstream_tokens.active} ${clientNoun} active. This confirms readiness, not current traffic.`);
  setRecommendation("none", "");
}
