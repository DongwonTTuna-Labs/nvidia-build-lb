async function upstreamFingerprint(value) {
  const bytes = new TextEncoder().encode(value);
  try {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return `sha256:${[...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  } finally {
    bytes.fill(0);
  }
}

function rememberUnresolvedMutation(kind, item, handle) {
  unresolvedMutation = {
    kind,
    targetId: item.id,
    handle,
    before: {
      enabled: item.enabled ?? null,
      routing_state: item.routing_state ?? null,
      health_state: item.health_state ?? null,
      cooldown_until: item.cooldown_until ?? null,
      last_status_class: item.last_status_class ?? null,
      revoked_at: item.revoked_at ?? null,
    },
  };
}

function unresolvedRecoveryContext() {
  if (unresolvedAdd) return `Adding submitted ${fingerprintHandle(unresolvedAdd.fingerprint)}`;
  if (unresolvedIssue) return `Issuing a token for ${unresolvedIssue.label} · ${accessLabel(unresolvedIssue.scopes)}`;
  if (!unresolvedMutation) return null;
  const verbs = {
    probe: "Probing",
    enable: "Enabling",
    disable: "Disabling",
    delete: "Deleting",
    revoke: "Revoking",
  };
  return `${verbs[unresolvedMutation.kind]} ${unresolvedMutation.handle}`;
}

function orphanRecoveryMessage() {
  return orphanedToken
    ? `Token for ${orphanedToken.label} exists, but its one-time credential was not received. Reauthenticate, refresh, and revoke it before issuing a replacement.`
    : null;
}

function probeStateChanged(item, before) {
  return ["routing_state", "health_state", "cooldown_until", "last_status_class"]
    .some((field) => item[field] !== before[field]);
}

function currentProbeState(item) {
  if (item.last_status_class) return healthCopy[item.last_status_class] ?? humanize(item.last_status_class);
  return item.health_state === "healthy" ? "Verified · Health is current" : healthCopy.unknown;
}

function setReconciledResult(targetId, message) {
  setOperationResult(targetId, message);
  pendingResultAnnouncement = message;
}

function finishMutationReconciliation(targetId, message) {
  setReconciledResult(targetId, message);
  unresolvedMutation = null;
}

function reconcileUnresolvedMutation(upstreams, downstreams) {
  if (!unresolvedMutation) return;
  const {kind, targetId, handle, before} = unresolvedMutation;
  if (kind === "revoke") {
    const token = downstreams.find((item) => item.id === targetId);
    if (token?.revoked_at) {
      if (orphanedToken?.id === targetId) orphanedToken = null;
      finishMutationReconciliation(targetId, `Fresh state confirms the ${handle} is revoked. The missing response cannot identify which request completed it.`);
    } else if (token) {
      finishMutationReconciliation(targetId, `Fresh state confirms the ${handle} remains active. Revoke can be tried again.`);
    } else {
      if (orphanedToken?.id === targetId) orphanedToken = null;
      finishMutationReconciliation("downstream-heading", `Fresh state no longer lists the ${handle}. No active credential is shown, but the missing response cannot identify why it disappeared.`);
    }
    return;
  }
  const key = upstreams.find((item) => item.id === targetId);
  if (kind === "probe") {
    if (!key) {
      finishMutationReconciliation("upstream-heading", `Fresh state no longer lists ${handle}; there is no probe target to retry.`);
    } else if (probeStateChanged(key, before)) {
      finishMutationReconciliation(targetId, `Fresh state for ${handle} is ${currentProbeState(key)}. The missing response cannot identify which probe produced that state.`);
    } else {
      finishMutationReconciliation(targetId, `Fresh state for ${handle} is unchanged, so no probe result is confirmed. Probe can be tried again.`);
    }
    return;
  }
  if (kind === "enable") {
    if (key?.enabled) finishMutationReconciliation(targetId, `Fresh state confirms ${handle} is enabled. The missing response cannot identify which request enabled it.`);
    else if (key) finishMutationReconciliation(targetId, `Fresh state confirms ${handle} remains disabled. Enable can be tried again after its prerequisites are met.`);
    else finishMutationReconciliation("upstream-heading", `Fresh state no longer lists ${handle}; there is no key to enable.`);
    return;
  }
  if (kind === "disable") {
    if (key && !key.enabled) {
      deliberatelyPausedKeyIds.set(targetId, {version: null});
      finishMutationReconciliation(targetId, `Fresh state confirms ${handle} is disabled. The missing response cannot identify which request disabled it.`);
    }
    else if (key) finishMutationReconciliation(targetId, `Fresh state confirms ${handle} remains enabled. Disable can be tried again.`);
    else finishMutationReconciliation("upstream-heading", `Fresh state no longer lists ${handle}; it cannot be selected for new requests.`);
    return;
  }
  if (key?.enabled) finishMutationReconciliation(targetId, `Fresh state confirms ${handle} still exists and is enabled. Disable it before trying Delete again.`);
  else if (key) finishMutationReconciliation(targetId, `Fresh state confirms ${handle} still exists and is disabled. Delete can be tried again.`);
  else finishMutationReconciliation("upstream-heading", `Fresh state confirms ${handle} no longer exists. The missing response cannot identify which request removed it.`);
}

function reconcileUnresolvedIssue(downstreams) {
  if (!unresolvedIssue) return;
  const matching = downstreams.find((item) => item.label === unresolvedIssue.label);
  const created = matching && !unresolvedIssue.knownIds.includes(matching.id);
  if (created && !matching.revoked_at) {
    orphanedToken = matching;
    setReconciledResult(matching.id, `Token for ${matching.label} exists, but its one-time credential response was lost. Revoke it before issuing a replacement.`);
  } else if (created) {
    orphanedToken = null;
    setReconciledResult(matching.id, `Token for ${matching.label} was created and is already revoked. Its label remains reserved; use a new label if a replacement is needed.`);
  } else if (matching) {
    orphanedToken = null;
    setReconciledResult(matching.id, `Refresh confirmed that ${matching.label} was already registered. No new token is confirmed; use a different unique label.`);
  } else {
    orphanedToken = null;
    setReconciledResult("issue-downstream", `Refresh confirmed that no new token for ${unresolvedIssue.label} exists. Issuance can be tried again.`);
  }
  unresolvedIssue = null;
}

function reconcileOrphanedToken(downstreams) {
  if (!orphanedToken) return;
  const previous = orphanedToken;
  const current = downstreams.find((item) => item.id === previous.id);
  if (!current) {
    orphanedToken = null;
    setReconciledResult("downstream-heading", `Fresh state no longer lists the token for ${previous.label}. No active credential is shown; the lost-credential recovery is complete.`);
    return;
  }
  if (current.revoked_at) {
    orphanedToken = null;
    setReconciledResult(current.id, `Fresh state confirms the token for ${current.label} is revoked. The lost-credential recovery is complete.`);
    return;
  }
  orphanedToken = current;
}

function reconcileUnresolvedAdd(upstreams) {
  if (!unresolvedAdd) return;
  const exact = upstreams.find((item) => item.fingerprint === unresolvedAdd.fingerprint);
  const unrelatedNew = upstreams.filter((item) => !unresolvedAdd.knownIds.includes(item.id) && item.fingerprint !== unresolvedAdd.fingerprint);
  if (exact && !unresolvedAdd.knownIds.includes(exact.id)) {
    if (unresolvedAdd.sourceId) replacementContext = {sourceId: unresolvedAdd.sourceId, replacementId: exact.id};
    setReconciledResult(exact.id, `Fresh state confirms ${keyHandle(exact)} exists, is ${exact.enabled ? "enabled" : "disabled"}, and has routing state ${humanize(exact.routing_state).toLowerCase()}. Its fingerprint matches the submitted key; the missing response cannot identify which request added it.`);
  } else if (exact) {
    setReconciledResult(exact.id, `Fresh state confirms the submitted key was already registered as ${keyHandle(exact)}. No new copy was added.`);
  } else {
    const concurrent = unrelatedNew.length ? ` ${unrelatedNew.length} unrelated new ${unrelatedNew.length === 1 ? "key appeared" : "keys appeared"} meanwhile.` : "";
    setReconciledResult("add-upstream", `Fresh state does not contain the submitted key.${concurrent} Adding that key can be retried.`);
  }
  unresolvedAdd = null;
}
