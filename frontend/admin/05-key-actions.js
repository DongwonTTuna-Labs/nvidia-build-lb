function openDialog(id, invoker, focusId) {
  lastInvoker = invoker;
  const dialog = byId(id);
  dialog.setAttribute("aria-busy", "false");
  clearDialogStaleState(dialog);
  for (const status of dialog.querySelectorAll("[data-dialog-busy]")) {
    status.hidden = true;
    setDialogDescription(dialog, status.id, false);
  }
  dialog.showModal();
  byId(focusId).focus();
}

function closeDialog(id, focusId = null, requireIdle = true) {
  const dialog = byId(id);
  if (requireIdle && dialog.getAttribute("aria-busy") === "true") {
    announce("Wait for the current request to finish. Its result must be confirmed before this dialog can close.");
    return false;
  }
  const targetId = focusId ?? lastInvoker?.id ?? null;
  const fallbackId = id === "confirm-dialog" && pendingAction?.kind === "revoke" ? "downstream-heading" : id === "confirm-dialog" ? "upstream-heading" : "dashboard-title";
  dialog.querySelector("form")?.reset();
  for (const control of dialog.querySelectorAll('[aria-busy="true"]')) {
    control.setAttribute("aria-busy", "false");
    if (control.dataset.idleLabel) {
      control.textContent = control.dataset.idleLabel;
      delete control.dataset.idleLabel;
    }
  }
  if (id === "confirm-dialog") pendingAction = null;
  if (id === "upstream-dialog") pendingReplacementSourceId = null;
  dialog.close();
  const target = targetId ? byId(targetId) : null;
  (target?.isConnected && !target.disabled && target.getClientRects().length ? target : byId(fallbackId))?.focus();
  lastInvoker = null;
  refreshMutationLocks();
  return true;
}

function actionProgress(item, action) {
  const handle = keyHandle(item);
  return action === "probe" ? `Probing ${handle}…` : `Enabling ${handle}…`;
}

function consumeDeleteFocusTarget() {
  const candidateIds = deleteFocusCandidateIds; deleteFocusCandidateIds = [];
  for (const id of candidateIds) {
    const row = byId(`key-${id}-probe`)?.closest("tr");
    const action = row?.querySelector("[data-mutation]:not(:disabled)");
    if (action) return action;
  }
  return byId("upstream-heading");
}

async function runKeyAction(item, action, button) {
  if (!snapshotCurrent || button.disabled) return;
  const generation = requestGeneration;
  const handle = keyHandle(item);
  const context = `${action === "probe" ? "Probing" : "Enabling"} ${handle}`;
  rememberUnresolvedMutation(action, item, handle);
  activeMutationContext = context;
  setLocatedResult(item.id, `${context} is in progress. No result is assumed yet.`, false);
  byId(`result-${item.id}`)?.focus();
  setControlBusy(button, true, actionProgress(item, action));
  try {
    const result = await api(`/upstream-keys/${item.id}/${action}`, {method: "POST"}, action === "probe" ? "probe" : null, mutationDeadlineMs);
    if (!requestIsCurrent(generation)) return;
    if (result.status === 401) {
      unresolvedMutation = null;
      activeMutationContext = null;
      clearInFlightOperation();
      mountLoginWithRecovery(true, false, context);
      return;
    }
    if (!result.ok) {
      const problem = result.problem;
      mutationRecoveryFocusId = button.id;
      if (mutationProblemIsKnownNoSuccess(problem)) {
        unresolvedMutation = null;
        setLocatedKnownNoSuccess(item.id, `${context} did not complete. The service confirmed no successful result for this request.`);
        showProblem(problem, context, false, true);
      } else {
        setLocatedResult(item.id, `${context} returned a failure response that does not prove the outcome. The action may have completed; do not repeat it until fresh state is reconciled.`, false, false);
        showProblem(problem, context);
      }
      return;
    }
    if (action === "probe") {
      const probe = result.value;
      deliberatelyPausedKeyIds.delete(item.id);
      if (probe.probe_status === "valid" && !probe.enabled) {
        enableReadyKeyEvidence.set(item.id, {version: null, observedAt: probe.observed_at});
      }
      else enableReadyKeyEvidence.delete(item.id);
      const message = probe.probe_status === "valid"
        ? probe.enabled
          ? `Probe confirmed ${handle} is valid and remains enabled. It can receive new requests.`
          : `Probe confirmed ${handle} is valid. Enable is now available.`
        : probe.enabled
          ? `Probe confirmed ${handle} is ${humanize(probe.probe_status).toLowerCase()}. It remains enabled, but this result does not confirm that it can receive new requests.`
          : `Probe confirmed ${handle} is ${humanize(probe.probe_status).toLowerCase()}. It remains disabled and cannot receive new requests.`;
      setLocatedResult(item.id, message);
    } else {
      enableReadyKeyEvidence.delete(item.id); deliberatelyPausedKeyIds.delete(item.id);
      setLocatedResult(item.id, `${handle} was enabled and can receive new requests.`);
    }
    unresolvedMutation = null;
    activeMutationContext = null;
    refreshMutationLocks();
    await refreshDashboard(`key-${item.id}-${action === "enable" ? "toggle" : "probe"}`);
  } catch (error) {
    if (requestIsCurrent(generation) && error instanceof AdminRequestTimeoutError) {
      setLocatedResult(item.id, `${context} reached the browser deadline and may have completed. Do not repeat it until current state is refreshed.`, false, false);
      mutationRecoveryFocusId = button.id;
      showProblem({code: "admin_mutation_timeout", message: "mutation outcome unknown", request_id: "unavailable"}, context);
      return;
    }
    if (requestIsCurrent(generation) && error instanceof AdminResponseError) {
      setLocatedResult(item.id, `${context} received an invalid service response. Its result is unknown until current state is refreshed.`, false, false);
      mutationRecoveryFocusId = button.id;
      showProblem({code: "invalid_response", message: "response did not match the strict administration contract", request_id: "unavailable"}, context);
      return;
    }
    if (!(error instanceof TypeError || error instanceof DOMException || error instanceof SyntaxError)) throw error;
    if (requestIsCurrent(generation)) {
      setLocatedResult(item.id, `${context} did not complete. Success was not assumed.`, false, false);
      mutationRecoveryFocusId = button.id;
      showProblem({code: "offline", message: "local service is unreachable", request_id: "unavailable"}, context);
    }
  } finally {
    if (requestIsCurrent(generation)) activeMutationContext = null;
    refreshMutationLocks();
    if (requestIsCurrent(generation) && button.isConnected) setControlBusy(button, false);
  }
}

function openConfirmation(kind, item, invoker) {
  const handle = item.fingerprint
    ? keyHandle(item)
    : `token for ${tokenHandle(item)}`;
  const confirmationHandle = item.fingerprint
    ? `${keyHandle(item)} · ID ${shortId(item.id)}`
    : `token for ${tokenHandle(item)} · ID ${shortId(item.id)}`;
  const descriptions = {
    disable: `Disable ${confirmationHandle}? New requests will stop selecting it. Existing work is not claimed cancelled.`,
    delete: `Permanently delete ${confirmationHandle}? It must already be disabled and this cannot be undone.`,
    revoke: `Revoke the ${confirmationHandle}? Future requests will be rejected and this cannot be undone.`,
  };
  pendingAction = {kind, item, handle};
  setText("confirm-title", kind === "delete" ? "Delete upstream key" : kind === "revoke" ? "Revoke downstream token" : "Disable upstream key");
  setText("confirm-description", descriptions[kind]);
  setText("confirm-action", kind === "delete" ? "Delete key" : kind === "revoke" ? "Revoke token" : "Disable key");
  byId("confirm-error").hidden = true; setProblemEvidence("confirm-error");
  openDialog("confirm-dialog", invoker, "confirm-title");
}

async function confirmPendingAction() {
  if (!pendingAction || !snapshotCurrent) return;
  const generation = requestGeneration;
  const {kind, item, handle} = pendingAction;
  const context = `${kind === "disable" ? "Disabling" : kind === "delete" ? "Deleting" : "Revoking"} ${handle}`;
  rememberUnresolvedMutation(kind, item, handle);
  activeMutationContext = context;
  const path = kind === "revoke" ? `/downstream-tokens/${item.id}` : kind === "delete" ? `/upstream-keys/${item.id}` : `/upstream-keys/${item.id}/disable`;
  setControlBusy("confirm-action", true, `${context}…`);
  setLocatedResult(item.id, `${context} is in progress. No result is assumed yet.`, false, false);
  try {
    const result = await api(path, {method: kind === "disable" ? "POST" : "DELETE"}, null, mutationDeadlineMs);
    if (!requestIsCurrent(generation)) return;
    if (result.status === 401) {
      unresolvedMutation = null;
      activeMutationContext = null;
      clearInFlightOperation();
      mountLoginWithRecovery(true, false, context);
      return;
    }
    if (!result.ok) {
      const problem = result.problem;
      const knownNoSuccess = mutationProblemIsKnownNoSuccess(problem);
      if (knownNoSuccess) {
        unresolvedMutation = null;
        setLocatedKnownNoSuccess(item.id, `${context} did not complete. The service confirmed no successful result for this request.`);
      } else {
        setLocatedResult(item.id, `${context} returned a failure response that does not prove the outcome. The action may have completed; do not repeat it until fresh state is reconciled.`, false, false);
      }
      setText("confirm-error-message", knownNoSuccess
        ? `${context} did not complete. The service confirmed no successful result. ${humanize(problem.code)}. Cancel this dialog, then refresh current state before retrying.`
        : `${context} may have completed because the failure response does not prove the outcome. Cancel this dialog, then refresh current state before doing anything else.`);
      setProblemEvidence("confirm-error", problem);
      byId("confirm-error").hidden = false;
      byId("confirm-error").focus();
      markSnapshotStale(knownNoSuccess
        ? "The last change did not confirm current state. Refresh before trying again."
        : "The mutation outcome is unknown until a fresh dashboard reconciles it.", false);
      return;
    }
    let focusId = kind === "revoke" ? `token-${item.id}-revoke` : `key-${item.id}-toggle`;
    if (kind === "delete") {
      const index = upstreamItems.findIndex((candidate) => candidate.id === item.id);
      deleteFocusCandidateIds = [...upstreamItems.slice(index + 1), ...upstreamItems.slice(0, index).reverse()].map((candidate) => candidate.id);
      focusId = "upstream-heading";
    }
    const confirmedMessage = kind === "disable" ? `${handle} was disabled and is excluded from new requests.` : kind === "delete" ? `${handle} was permanently deleted.` : `The ${handle} was revoked and future requests will be rejected.`;
    if (kind === "disable" || kind === "delete") enableReadyKeyEvidence.delete(item.id);
    if (kind === "disable") {
      deliberatelyPausedKeyIds.set(item.id, {version: null});
    }
    if (kind === "delete") deliberatelyPausedKeyIds.delete(item.id);
    setLocatedResult(item.id, confirmedMessage, true, false);
    pendingResultAnnouncement = confirmedMessage;
    unresolvedMutation = null;
    if (kind === "revoke" && orphanedToken?.id === item.id) orphanedToken = null;
    activeMutationContext = null;
    refreshMutationLocks();
    closeDialog("confirm-dialog", null, false);
    pendingAction = null;
    await refreshDashboard(focusId);
  } catch (error) {
    if (!(error instanceof AdminRequestTimeoutError || error instanceof AdminResponseError || error instanceof TypeError || error instanceof DOMException || error instanceof SyntaxError)) throw error;
    if (requestIsCurrent(generation)) {
      const code = error instanceof AdminRequestTimeoutError ? "admin_mutation_timeout" : error instanceof AdminResponseError ? "invalid_response" : "offline";
      setLocatedResult(item.id, `${context} response was not confirmed and the action may have completed. Do not repeat it until fresh state is reconciled.`, false, false);
      setText("confirm-error-message", `${context} may have completed, but its response was not confirmed. Cancel this dialog, then refresh current state before doing anything else.`);
      setProblemEvidence("confirm-error", {code, message: "mutation outcome unknown", request_id: "unavailable"});
      byId("confirm-error").hidden = false;
      byId("confirm-error").focus();
      markSnapshotStale("The mutation outcome is unknown until a fresh dashboard reconciles it.", false);
    }
  } finally {
    if (requestIsCurrent(generation)) activeMutationContext = null;
    refreshMutationLocks();
    if (requestIsCurrent(generation) && byId("confirm-action")) setControlBusy("confirm-action", false);
  }
}
