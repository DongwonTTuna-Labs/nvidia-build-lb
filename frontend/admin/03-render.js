function renderUpstreamSectionResult(items) {
  const targetId = operationResult?.targetId ?? null;
  const targetMissing = targetId && !items.some((item) => item.id === targetId);
  const located = operationResult
    && operationResult.surface === "upstream"
    && (targetId === "upstream-heading" || targetMissing)
    ? operationResult
    : null;
  byId("upstream-result").hidden = !located;
  byId("upstream-result").textContent = located?.message ?? "";
}

function renderUpstreams(items, reference) {
  upstreamItems = items;
  for (const item of items) upstreamHandleHistory.set(item.id, keyHandle(item));
  renderUpstreamSectionResult(items);
  if (!items.length) {
    tableMessage("upstream-body", 4, snapshotCurrent ? "No upstream keys registered. Add one to begin." : "Last confirmed · No upstream keys were registered.");
    return;
  }
  const rows = items.map((item) => {
    const handle = keyHandle(item);
    const row = document.createElement("tr");
    const keyCell = createRowHeader("Key", "", "cell-stack");
    const heading = document.createElement("strong");
    heading.textContent = handle;
    keyCell.append(heading);
    const evidence = createEvidence("Evidence", [
      ["Internal ID", item.id],
      ["Fingerprint", item.fingerprint],
      ["Cooldown until", displayTime(item.cooldown_until)],
      ["Requests", String(item.request_count)],
      ["Succeeded", String(item.success_count)],
      ["Failed", String(item.failure_count)],
      ["Last used", displayTime(item.last_used_at)],
      ["Updated", item.updated_at],
    ], `Evidence for ${handle}`, `key-${item.id}-evidence`);
    const [routingLabel, defaultRoutingMeaning, routingStatus] = routingCopy[item.routing_state];
    const cooldownActive = item.cooldown_until && Date.parse(item.cooldown_until) > Date.parse(reference);
    const cooldownReturn = snapshotCurrent && cooldownActive
      ? item.enabled
        ? `Returns automatically ${remainingTime(item.cooldown_until, reference)}`
        : `Cooldown ends ${remainingTime(item.cooldown_until, reference)}; remains disabled`
      : null;
    const statePrefix = snapshotCurrent ? "" : "Last confirmed · ";
    const routeCell = createCell("Routing", `${statePrefix}${routingLabel} · ${cooldownReturn ?? defaultRoutingMeaning}`, "status");
    routeCell.dataset.status = routingStatus;
    const cooldownEnded = snapshotCurrent && item.cooldown_until && Date.parse(item.cooldown_until) <= Date.parse(reference);
    const healthMeaning = item.last_status_class === "rate_limited" && cooldownEnded
      ? item.routing_state === "eligible"
        ? "Cooldown ended · Eligible for new requests"
        : "Cooldown ended · Probe the disabled key before enabling"
      : item.last_status_class
      ? healthCopy[item.last_status_class]
      : item.health_state === "healthy"
        ? "Verified · Health is current"
        : healthCopy.unknown;
    const visibleHealthMeaning = !snapshotCurrent && item.last_status_class === "rate_limited"
      ? "Rate limited at the last snapshot"
      : healthMeaning;
    const healthCell = createCell("Health", `${statePrefix}${visibleHealthMeaning}`, "status");
    healthCell.dataset.status = item.health_state === "healthy" ? "healthy" : item.health_state === "degraded" ? "degraded" : "disabled";
    const actions = createCell("Actions", "", "actions");
    const probe = createButton("Probe", `key-${item.id}-probe`, `Probe ${handle}`, (event) => runKeyAction(item, "probe", event.currentTarget));
    const toggle = createButton(item.enabled ? "Disable" : "Enable", `key-${item.id}-toggle`, `${item.enabled ? "Disable" : "Enable"} ${handle}`, (event) => item.enabled ? openConfirmation("disable", item, event.currentTarget) : runKeyAction(item, "enable", event.currentTarget));
    const remove = createButton("Delete", `key-${item.id}-delete`, `Delete ${handle}`, (event) => openConfirmation("delete", item, event.currentTarget));
    if (unconfirmedProbeKeyIds.has(item.id)) {
      const reasonId = `key-${item.id}-unconfirmed-probe-reason`;
      for (const control of [probe, toggle, remove]) {
        control.dataset.prerequisite = "blocked";
        control.setAttribute("aria-describedby", reasonId);
      }
      const reason = document.createElement("span");
      reason.className = "action-reason";
      reason.id = reasonId;
      reason.textContent = "A previous operator probe has no confirmed completion. Wait for it to settle, then Refresh current state before any action on this key.";
      actions.append(probe, toggle, remove, reason);
    } else {
      let probeReason = null;
      let probeBlocked = false;
      if (cooldownActive) {
        probeBlocked = true;
        probeReason = snapshotCurrent
          ? item.enabled
            ? `Probe is available after cooldown ends ${remainingTime(item.cooldown_until, reference)}.`
            : `Wait until cooldown ends ${remainingTime(item.cooldown_until, reference)}, then probe this disabled key.`
          : `Last confirmed cooldown deadline: ${item.cooldown_until}. Refresh current state before probing.`;
      } else if (item.last_status_class === "invalid_credential") {
        probeBlocked = true;
        probeReason = "Use a replacement credential; probing this rejected credential is unavailable.";
      } else if (item.last_status_class === "credits_exhausted") {
        probe.textContent = "Probe after credits";
        probe.setAttribute("aria-label", `Probe after credits for ${handle}`);
        probeReason = "Run only after restoring NVIDIA credits; the console cannot observe that external change.";
      }
      if (probeReason) {
        const reasonId = `key-${item.id}-probe-reason`;
        if (probeBlocked) probe.dataset.prerequisite = "blocked";
        probe.setAttribute("aria-describedby", reasonId);
        const reason = document.createElement("span");
        reason.className = "action-reason";
        reason.id = reasonId;
        reason.textContent = probeReason;
        actions.append(probe, reason);
      } else actions.append(probe);
      if (!item.enabled && item.health_state !== "healthy") {
        const reasonId = `key-${item.id}-enable-reason`;
        toggle.dataset.prerequisite = "blocked";
        toggle.setAttribute("aria-describedby", reasonId);
        const reason = document.createElement("span");
        reason.className = "action-reason";
        reason.id = reasonId;
        reason.textContent = item.last_status_class === "invalid_credential"
          ? "This rejected credential cannot be enabled. Add, probe, and enable a replacement first."
          : "Enable is available after a successful probe.";
        actions.append(toggle, reason, remove);
      } else {
        actions.append(toggle, remove);
      }
      if (item.enabled) {
        const reasonId = `key-${item.id}-delete-reason`;
        remove.dataset.prerequisite = "blocked";
        remove.setAttribute("aria-describedby", reasonId);
        const reason = document.createElement("span");
        reason.className = "action-reason";
        reason.id = reasonId;
        reason.textContent = "Disable this key before deleting it.";
        actions.append(reason);
      } else if (item.last_status_class === "invalid_credential"
        && currentSnapshot.overview.upstream_keys.eligible === 0) {
        const reasonId = `key-${item.id}-delete-reason`;
        remove.dataset.prerequisite = "blocked";
        remove.setAttribute("aria-describedby", reasonId);
        const reason = document.createElement("span");
        reason.className = "action-reason";
        reason.id = reasonId;
        reason.textContent = "Add, probe, and enable a replacement before deleting this rejected key.";
        actions.append(reason);
      }
    }
    const result = document.createElement("p");
    result.className = "row-result";
    result.id = `result-${item.id}`;
    result.tabIndex = -1;
    const locatedResult = operationStatus?.targetId === item.id ? operationStatus : operationResult?.targetId === item.id ? operationResult : null;
    result.hidden = !locatedResult;
    result.textContent = locatedResult?.message ?? "";
    actions.append(result, evidence);
    row.append(keyCell, routeCell, healthCell, actions);
    return row;
  });
  byId("upstream-body").replaceChildren(...rows);
  refreshMutationLocks();
}

function renderDownstreams(items) {
  downstreamItems = items;
  for (const item of items) downstreamHandleHistory.set(item.id, tokenHandle(item));
  if (!items.length) {
    tableMessage("downstream-body", 4, snapshotCurrent ? "No downstream tokens issued. Issue one with the minimum scopes." : "Last confirmed · No downstream tokens were issued.");
    return;
  }
  const rows = items.map((item) => {
    const row = document.createElement("tr");
    const client = createRowHeader("Client", "", "cell-stack");
    const human = document.createElement("strong");
    human.className = "human-label";
    human.textContent = tokenHandle(item);
    client.append(human);
    const evidence = createEvidence("Evidence", [
      ["Full label", item.label],
      ["Internal ID", item.id],
      ["Requests", String(item.request_count)],
      ["Last used", displayTime(item.last_used_at)],
      ["Created", item.created_at],
      ["Revoked", displayTime(item.revoked_at)],
    ], `Evidence for ${tokenHandle(item)}`, `token-${item.id}-evidence`);
    const access = createCell("Access", accessLabel(item.scopes));
    const statePrefix = snapshotCurrent ? "" : "Last confirmed · ";
    const state = createCell("State", `${statePrefix}${item.revoked_at ? "Revoked · Requests are rejected" : "Active · Credential accepted"}`, "status");
    state.dataset.status = item.revoked_at ? "revoked" : "healthy";
    const actions = createCell("Actions", "", "actions");
    const actionId = `token-${item.id}-revoke`;
    if (item.revoked_at) {
      const revoked = document.createElement("span");
      revoked.id = actionId;
      revoked.className = "status";
      revoked.dataset.status = "revoked";
      revoked.tabIndex = -1;
      revoked.textContent = "Revoked · No action available";
      actions.append(revoked);
    } else {
      actions.append(createButton("Revoke", actionId, `Revoke token for ${tokenHandle(item)}`, (event) => openConfirmation("revoke", item, event.currentTarget)));
    }
    const result = document.createElement("p");
    result.className = "row-result";
    result.id = `result-${item.id}`;
    result.tabIndex = -1;
    const locatedResult = operationStatus?.targetId === item.id ? operationStatus : operationResult?.targetId === item.id ? operationResult : null;
    result.hidden = !locatedResult;
    result.textContent = locatedResult?.message ?? "";
    actions.append(result, evidence);
    row.append(client, access, state, actions);
    return row;
  });
  byId("downstream-body").replaceChildren(...rows);
  refreshMutationLocks();
}
function renderSnapshot(snapshot) {
  const openEvidenceIds = captureOpenEvidenceIds();
  const reference = snapshot.referenceAt ?? snapshot.overview.generated_at;
  unconfirmedProbeKeyIds = unconfirmedProbeTargets(snapshot.events);
  renderOverview(snapshot, reference);
  renderUpstreams(snapshot.upstreams, reference);
  renderDownstreams(snapshot.downstreams);
  renderEvents(snapshot.events, reference, snapshot.ledger);
  renderDecision(snapshot, reference);
  restoreOpenEvidence(openEvidenceIds);
}
