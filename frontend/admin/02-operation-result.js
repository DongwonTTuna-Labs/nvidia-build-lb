const knownNoSuccessMutationCodes = new Set([
  "invalid_request",
  "resource_not_found",
  "resource_conflict",
  "runtime_unavailable",
  "ledger_capacity_exhausted",
]);

function mutationProblemIsKnownNoSuccess(problem) {
  return knownNoSuccessMutationCodes.has(problem.code);
}

function resultSurface(targetId) {
  if (targetId === "upstream-heading" || targetId === "add-upstream"
    || upstreamItems.some((item) => item.id === targetId)
    || upstreamHandleHistory.has(targetId)
    || unresolvedMutation?.targetId === targetId && unresolvedMutation.kind !== "revoke") return "upstream";
  if (targetId === "downstream-heading" || targetId === "issue-downstream"
    || downstreamItems.some((item) => item.id === targetId)
    || downstreamHandleHistory.has(targetId)
    || unresolvedMutation?.targetId === targetId && unresolvedMutation.kind === "revoke") return "downstream";
  return null;
}
function setOperationResult(targetId, message, confirmed = true, surface = null) {
  const locatedSurface = surface ?? resultSurface(targetId);
  if (confirmed) {
    operationResult = {targetId, message, confirmed: true, surface: locatedSurface};
    operationStatus = null;
  } else operationStatus = {targetId, message, confirmed: false, surface: locatedSurface};
  renderOperationSummary();
}
function renderOperationSummary() {
  setText("last-result", operationResult?.message ?? "No action has completed in this tab.");
  byId("operation-status-line").hidden = !operationStatus;
  setText("operation-status", operationStatus?.message ?? "No operation is pending.");
}
function clearInFlightOperation() {
  operationStatus = null;
  renderOperationSummary();
}
function renderLocatedResult(targetId, message, live) {
  const result = byId(`result-${targetId}`);
  if (result) {
    if (live) result.setAttribute("role", "status");
    else result.removeAttribute("role");
    result.hidden = false;
    result.textContent = message;
  }
}
function setLocatedResult(targetId, message, confirmed = true, live = true) {
  setOperationResult(targetId, message, confirmed);
  renderLocatedResult(targetId, message, live);
}
function setLocatedKnownNoSuccess(targetId, message, live = false) {
  clearInFlightOperation();
  renderLocatedResult(targetId, message, live);
}
