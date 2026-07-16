function showUpstreamProblem(problem) {
  if (problem.code === "resource_conflict") {
    showUpstreamError("This key is already registered. Re-enter a different key or cancel.", true, problem);
    return;
  }
  if (problem.code === "invalid_request") {
    showUpstreamError("The service rejected this key value. Re-enter a valid NVIDIA hosted API key or cancel.", true, problem);
    return;
  }
  showUpstreamError(`Adding the key did not complete. ${humanize(problem.code)}. Cancel, then refresh current state.`, false, problem);
  markSnapshotStale("The service response did not confirm a safe basis for another change.", false);
}
function showDownstreamProblem(problem) {
  if (problem.code === "resource_conflict") {
    showDownstreamError("That client label already exists. Enter a unique label; the selected scopes are preserved.", true, false, true, problem);
    return;
  }
  if (problem.code === "invalid_request") {
    showDownstreamError("The service rejected this label or scope selection. Review both fields or cancel.", true, true, true, problem);
    return;
  }
  showDownstreamError(`Issuing the token did not complete. ${humanize(problem.code)}. Cancel, then refresh current state.`, false, false, true, problem);
  markSnapshotStale("The service response did not confirm a safe basis for another change.", false);
}
async function submitUpstream(event) {
  event.preventDefault();
  if (!snapshotCurrent) return;
  const field = byId("upstream-key");
  if (!field.value.trim()) {
    showUpstreamError("Enter a non-empty NVIDIA hosted API key. The key remains only in this dialog until submission.");
    return;
  }
  clearUpstreamError();
  const generation = requestGeneration;
  let opaqueKey = field.value;
  field.value = "";
  let body = "";
  const replacementSource = upstreamItems.find((item) => item.id === pendingReplacementSourceId);
  const context = replacementSource ? `Adding a replacement for ${keyHandle(replacementSource)}` : "Adding an upstream key";
  activeMutationContext = context;
  setOperationResult("add-upstream", `${context} is in progress. No result is assumed yet.`, false);
  setControlBusy("submit-upstream", true, `${context}…`);
  try {
    let fingerprint;
    try {
      fingerprint = await upstreamFingerprint(opaqueKey);
    } catch (error) {
      if (!(error instanceof TypeError || error instanceof DOMException)) throw error;
      setControlBusy("submit-upstream", false);
      setOperationResult("add-upstream", "The key was not sent because its confirmation fingerprint could not be created.");
      showUpstreamError("The key was not sent because this browser could not create its confirmation fingerprint. Re-enter it in a supported browser or cancel.");
      return;
    }
    unresolvedAdd = {fingerprint, knownIds: upstreamItems.map((item) => item.id), sourceId: pendingReplacementSourceId};
    body = JSON.stringify({key: opaqueKey});
    const result = await api("/upstream-keys", {method: "POST", headers: {"Content-Type": "application/json"}, body}, "upstream", mutationDeadlineMs);
    if (!requestIsCurrent(generation)) return;
    if (result.status === 401) {
      unresolvedAdd = null;
      activeMutationContext = null;
      clearInFlightOperation();
      mountLoginWithRecovery(true, false, context);
      return;
    }
    if (!result.ok) {
      const problem = result.problem;
      if (mutationProblemIsKnownNoSuccess(problem)) {
        unresolvedAdd = null;
        clearInFlightOperation();
        showUpstreamProblem(problem);
      } else {
        const submittedHandle = fingerprintHandle(unresolvedAdd.fingerprint);
        setOperationResult("add-upstream", `${context} returned a failure response that does not prove the outcome. Its result is unknown until fresh state is reconciled.`, false);
        showUpstreamError(`The failure response for submitted ${submittedHandle} does not prove whether the key was added. The submitted value was removed. Cancel and refresh; do not submit it again until its fingerprint is reconciled.`, false, problem);
        markSnapshotStale("The add outcome is unknown until a fresh dashboard reconciles it.", false);
      }
      return;
    }
    const created = result.value;
    unresolvedAdd = null;
    if (pendingReplacementSourceId) replacementContext = {sourceId: pendingReplacementSourceId, replacementId: created.id};
    pendingReplacementSourceId = null;
    setOperationResult(created.id, replacementSource ? `${keyHandle(created)} was encrypted and added disabled as the replacement for ${keyHandle(replacementSource)}. Probe it before enabling.` : `${keyHandle(created)} was encrypted and added disabled. Probe it before enabling.`); pendingResultAnnouncement = operationResult.message;
    activeMutationContext = null;
    refreshMutationLocks();
    closeDialog("upstream-dialog", "add-upstream", false);
    await refreshDashboard("add-upstream");
  } catch (error) {
    if (error instanceof AdminRequestTimeoutError && requestIsCurrent(generation) && byId("upstream-dialog").open) {
      const submittedHandle = fingerprintHandle(unresolvedAdd.fingerprint);
      setOperationResult("add-upstream", `${context} reached the browser deadline and may have completed.`, false);
      showUpstreamError(`The request for ${submittedHandle} reached its deadline and may have completed. The submitted value was removed. Cancel and refresh current state; do not submit it again until its fingerprint is reconciled.`, false, {code: "admin_mutation_timeout", message: "mutation outcome unknown", request_id: "unavailable"});
      markSnapshotStale("The add outcome is unknown until a fresh dashboard reconciles it.", false);
      return;
    }
    if (error instanceof AdminResponseError && requestIsCurrent(generation) && byId("upstream-dialog").open) {
      const submittedHandle = fingerprintHandle(unresolvedAdd.fingerprint);
      setOperationResult("add-upstream", `${context} returned an invalid response. Its result is unknown until fresh state is reconciled.`, false);
      showUpstreamError(`The service returned an invalid response after accepting ${submittedHandle}. Its outcome is unknown. The editable field is a new draft; Cancel and refresh current state to reconcile the submitted fingerprint before retrying.`, false, {code: "invalid_response", message: "response did not match the strict administration contract", request_id: "unavailable"});
      markSnapshotStale("The add response did not match the strict administration contract.", false);
      return;
    }
    if (!(error instanceof TypeError || error instanceof DOMException || error instanceof SyntaxError)) throw error;
    if (requestIsCurrent(generation) && byId("upstream-dialog").open) {
      const submittedHandle = fingerprintHandle(unresolvedAdd.fingerprint);
      setOperationResult("add-upstream", `${context} response was lost. Its result is unknown until fresh state is reconciled.`, false);
      showUpstreamError(`The response for submitted ${submittedHandle} was lost, so its result is unknown and the submitted value was removed. The editable field is a new draft. Cancel and refresh; success is identified only by that submitted fingerprint.`, false, {code: "offline", message: "the local response was not received", request_id: "unavailable"});
      markSnapshotStale("The add response was not confirmed.", false);
    }
  } finally {
    opaqueKey = "";
    body = "";
    if (requestIsCurrent(generation)) activeMutationContext = null;
    refreshMutationLocks();
    if (requestIsCurrent(generation) && byId("submit-upstream")) setControlBusy("submit-upstream", false);
  }
}
async function submitDownstream(event) {
  event.preventDefault();
  if (!snapshotCurrent) return;
  const labelField = byId("downstream-label");
  const labelValue = labelField.value;
  const scopes = [...document.querySelectorAll("input[name='scope']:checked")].map((input) => input.value);
  const labelInvalid = !labelValue.trim() || labelValue !== labelValue.trim();
  const scopesInvalid = scopes.length === 0;
  if (labelInvalid || scopesInvalid) {
    const correction = labelInvalid && scopesInvalid ? "Enter a unique label without surrounding spaces and choose at least one scope." : labelInvalid ? "Enter a unique label without surrounding spaces." : "Choose at least one scope.";
    showDownstreamError(correction, labelInvalid, scopesInvalid);
    return;
  }
  clearDownstreamError();
  const generation = requestGeneration;
  let body = JSON.stringify({label: labelValue, scopes});
  const context = `Issuing a token for ${labelValue}`;
  unresolvedIssue = {label: labelValue, scopes: [...scopes], knownIds: downstreamItems.map((item) => item.id)};
  activeMutationContext = context;
  setOperationResult("issue-downstream", `${context} is in progress. No result is assumed yet.`, false);
  setControlBusy("submit-downstream", true, `${context}…`);
  try {
    const result = await api("/downstream-tokens", {method: "POST", headers: {"Content-Type": "application/json"}, body}, "issued", mutationDeadlineMs);
    if (!requestIsCurrent(generation)) return;
    if (result.status === 401) {
      unresolvedIssue = null;
      activeMutationContext = null;
      clearInFlightOperation();
      mountLoginWithRecovery(true, false, context);
      return;
    }
    if (!result.ok) {
      const problem = result.problem;
      if (mutationProblemIsKnownNoSuccess(problem)) {
        unresolvedIssue = null;
        clearInFlightOperation();
        showDownstreamProblem(problem);
      } else {
        const submittedTarget = `Client ${unresolvedIssue.label} · ${accessLabel(unresolvedIssue.scopes)}`;
        setOperationResult("issue-downstream", `${context} returned a failure response that does not prove the outcome. Its result is unknown until fresh state is reconciled.`, false);
        showDownstreamError(`The failure response for ${submittedTarget} does not prove whether a token was issued, and no credential is available. Cancel and refresh; if that client exists, revoke it before replacement.`, false, false, true, problem);
        markSnapshotStale("The token issue outcome is unknown until a fresh dashboard reconciles it.", false);
      }
      return;
    }
    const issued = result.value;
    unresolvedIssue = null;
    oneTimeToken = issued.token;
    issuedTokenContext = {id: issued.id, label: issued.label, scopes: issued.scopes};
    copiedCredential = false;
    clipboardAvailableWhenIssued = clipboardUsable();
    byId("one-time-token").value = oneTimeToken;
    setText("credential-title", `Store credential for ${compactLabel(issued.label)}`);
    setText("credential-target", `Client ${issued.label} · ${accessLabel(issued.scopes)}`);
    byId("clipboard-error").hidden = true;
    byId("downstream-form").reset();
    activeMutationContext = null;
    refreshMutationLocks();
    closeDialog("downstream-dialog", null, false);
    openDialog("credential-dialog", byId("issue-downstream"), "credential-title");
  } catch (error) {
    if (error instanceof AdminRequestTimeoutError && requestIsCurrent(generation) && byId("downstream-dialog").open) {
      const submittedTarget = `Client ${unresolvedIssue.label} · ${accessLabel(unresolvedIssue.scopes)}`;
      setOperationResult("issue-downstream", `${context} reached the browser deadline and may have completed.`, false);
      showDownstreamError(`The request for ${submittedTarget} reached its deadline and may have completed, but no credential is available. Cancel and refresh; if that client exists, revoke it before issuing a replacement.`, false, false, true, {code: "admin_mutation_timeout", message: "mutation outcome unknown", request_id: "unavailable"});
      markSnapshotStale("The token issue outcome is unknown until a fresh dashboard reconciles it.", false);
      return;
    }
    if (error instanceof AdminResponseError && requestIsCurrent(generation) && byId("downstream-dialog").open) {
      const submittedTarget = `Client ${unresolvedIssue.label} · ${accessLabel(unresolvedIssue.scopes)}`;
      setOperationResult("issue-downstream", `${context} returned an invalid response. Its result is unknown until fresh state is reconciled.`, false);
      showDownstreamError(`The service returned an invalid response after accepting ${submittedTarget}. Its outcome is unknown. The editable fields are a new draft. Cancel and refresh; if that submitted client exists, revoke it before replacement.`, false, false, true, {code: "invalid_response", message: "response did not match the strict administration contract", request_id: "unavailable"});
      markSnapshotStale("The token response did not match the strict administration contract.", false);
      return;
    }
    if (!(error instanceof TypeError || error instanceof DOMException || error instanceof SyntaxError)) throw error;
    if (requestIsCurrent(generation) && byId("downstream-dialog").open) {
      const submittedTarget = `Client ${unresolvedIssue.label} · ${accessLabel(unresolvedIssue.scopes)}`;
      setOperationResult("issue-downstream", `${context} response was lost. Its result is unknown until fresh state is reconciled.`, false);
      showDownstreamError(`The response for submitted ${submittedTarget} was lost, so issuance is unknown. The editable fields are a new draft. Cancel and refresh; if that submitted client exists, revoke it before replacement.`, false, false, true, {code: "offline", message: "the local response was not received", request_id: "unavailable"});
      markSnapshotStale("The token issue response was not confirmed.", false);
    }
  } finally {
    body = "";
    if (requestIsCurrent(generation)) activeMutationContext = null;
    refreshMutationLocks();
    if (requestIsCurrent(generation) && byId("submit-downstream")) setControlBusy("submit-downstream", false);
  }
}
