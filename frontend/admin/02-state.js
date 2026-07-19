const byId = (id) => document.getElementById(id);
// Keep the pure DTO helpers importable by the contract harness, which runs
// without a browser DOM. The template is materialized only in the browser.
const loginTemplate = typeof document === "undefined"
  ? null
  : byId("login-form")?.cloneNode(true);
const apiRoot = "/admin/api/v1";
const snapshotTtlMs = 60_000;
const readDeadlineMs = 8_000;
const mutationDeadlineMs = 130_000;
let adminBearer = null;
let oneTimeToken = null;
let copiedCredential = false;
let clipboardAvailableWhenIssued = false;
let clipboardWritePending = false;
let clipboardWritePromise = null;
let sessionController = new AbortController();
let requestGeneration = 0;
let hasSafeData = false;
let snapshotCurrent = false;
let snapshotTimer = null;
let refreshInProgress = false;
let currentSnapshot = null;
let upstreamItems = [];
let downstreamItems = [];
let unconfirmedProbeKeyIds = new Set();
let upstreamHandleHistory = new Map();
let downstreamHandleHistory = new Map();
let lastInvoker = null;
let pendingAction = null;
let operationResult = null;
let operationStatus = null;
let pendingResultAnnouncement = null;
let recommendedPlan = null;
let issuedTokenContext = null;
let runtimeMutationBlocked = false;
let activeMutationContext = null;
let snapshotStaleReason = null;
let unresolvedAdd = null;
let unresolvedIssue = null;
let unresolvedMutation = null;
let orphanedToken = null;
let pendingReplacementSourceId = null; let replacementContext = null;
let enableReadyKeyEvidence = new Map(); let deliberatelyPausedKeyIds = new Map(); let mutationRecoveryFocusId = null;
let deleteFocusCandidateIds = [];
let credentialBusyOwner = null;
function announce(message) { byId("live-region").textContent = message; }
function setText(id, value) { byId(id).textContent = value ?? "Not available"; }
function displayTime(value) { return value ?? "Not available"; }
function humanize(value) { return value ? value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase()) : "Not available"; }
function fingerprintHandle(value) { return `Key ${value.slice(7, 15)}`; }
function keyHandle(item) { return fingerprintHandle(item.fingerprint); }
function shortId(value) { return value.slice(-8); }
function compactLabel(value) { const characters = [...value]; return characters.length <= 48 ? value : `${characters.slice(0, 48).join("")}…`; }
function tokenHandle(item) { return item.label; }
function relativeTime(value, reference) {
  if (!value) return "No activity yet";
  const elapsed = Math.max(0, Date.parse(reference) - Date.parse(value));
  if (elapsed < 60_000) return "Just now";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m earlier`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h earlier`;
  return `${Math.floor(elapsed / 86_400_000)}d earlier`;
}
function remainingTime(value, reference) {
  const remaining = Date.parse(value) - Date.parse(reference);
  if (remaining <= 0) return "now";
  if (remaining < 60_000) return "in under 1m";
  if (remaining < 3_600_000) return `in ${Math.ceil(remaining / 60_000)}m`;
  if (remaining < 86_400_000) return `in ${Math.ceil(remaining / 3_600_000)}h`;
  return `in ${Math.ceil(remaining / 86_400_000)}d`;
}
function createCell(labelText, value, className = "") {
  const cell = document.createElement("td");
  cell.dataset.label = labelText;
  cell.textContent = value;
  if (className) cell.className = className;
  return cell;
}
function createRowHeader(labelText, value, className = "") {
  const cell = document.createElement("th"); cell.scope = "row";
  cell.dataset.label = labelText; cell.textContent = value;
  if (className) cell.className = className;
  return cell;
}
function createButton(labelText, id, ariaLabel, handler) {
  const button = document.createElement("button");
  button.className = "control secondary";
  button.id = id;
  button.type = "button";
  button.textContent = labelText;
  button.setAttribute("aria-label", ariaLabel);
  button.dataset.mutation = "true";
  button.addEventListener("click", handler);
  return button;
}
const machineEvidenceTerms = new Set(["Internal ID", "Fingerprint", "Exact time", "Cooldown until", "Requests", "Succeeded", "Failed", "Last used", "Updated", "Created", "Revoked"]);
function createEvidence(summaryText, entries, accessibleName = summaryText, summaryId = null) {
  const details = document.createElement("details");
  details.className = "resource-details";
  const summary = document.createElement("summary");
  summary.textContent = summaryText;
  summary.setAttribute("aria-label", accessibleName);
  if (summaryId) summary.id = summaryId;
  const list = document.createElement("dl");
  for (const [term, value] of entries) {
    const termNode = document.createElement("dt");
    const valueNode = document.createElement("dd");
    termNode.textContent = term;
    valueNode.textContent = value;
    if (machineEvidenceTerms.has(term)) valueNode.className = "machine-id";
    list.append(termNode, valueNode);
  }
  details.append(summary, list);
  return details;
}
function tableMessage(bodyId, span, message) {
  const row = document.createElement("tr");
  const cell = createCell("State", message);
  cell.colSpan = span;
  row.append(cell);
  byId(bodyId).replaceChildren(row);
}
function resetSessionController() {
  requestGeneration += 1;
  sessionController.abort();
  sessionController = new AbortController();
}
function requestIsCurrent(generation) { return generation === requestGeneration; }
function setDialogDescription(dialog, descriptionId, present) {
  const described = new Set((dialog.getAttribute("aria-describedby") ?? "").split(" ").filter(Boolean));
  if (present) described.add(descriptionId);
  else described.delete(descriptionId);
  dialog.setAttribute("aria-describedby", [...described].join(" "));
}
function setControlBusy(controlOrId, busy, progressLabel = null) {
  const control = typeof controlOrId === "string" ? byId(controlOrId) : controlOrId;
  const dialog = control.closest("dialog");
  const dialogMutation = dialog && control.hasAttribute("data-mutation");
  const busyStatus = dialogMutation ? dialog.querySelector("[data-dialog-busy]") : null;
  if (busy && dialogMutation) {
    dialog.setAttribute("aria-busy", "true");
    if (busyStatus) {
      const task = (progressLabel ?? control.textContent).replace(/…$/u, "");
      busyStatus.textContent = `${task}. Cancel and Escape are unavailable while this request is in flight.`;
      busyStatus.hidden = false;
      setDialogDescription(dialog, busyStatus.id, true);
      busyStatus.focus();
    }
  }
  if (busy) {
    control.dataset.idleLabel = control.textContent;
    if (progressLabel) control.textContent = progressLabel;
  } else if (control.dataset.idleLabel) {
    control.textContent = control.dataset.idleLabel;
    delete control.dataset.idleLabel;
  }
  control.setAttribute("aria-busy", String(busy));
  control.disabled = busy;
  if (dialogMutation) {
    dialog.setAttribute("aria-busy", String(busy));
    if (!busy && busyStatus) {
      busyStatus.hidden = true;
      setDialogDescription(dialog, busyStatus.id, false);
    }
    for (const closer of dialog.querySelectorAll(`[data-close='${dialog.id}']`)) {
      closer.disabled = busy;
      if (busy) closer.setAttribute("aria-disabled", "true");
      else closer.removeAttribute("aria-disabled");
    }
    for (const field of dialog.querySelectorAll("input, select, textarea")) {
      if (busy && !field.disabled) {
        field.dataset.busyDisabled = "true";
        field.disabled = true;
      } else if (!busy && field.dataset.busyDisabled === "true") {
        field.disabled = false;
        delete field.dataset.busyDisabled;
      }
    }
  }
  refreshMutationLocks();
  if (!busy && !control.hasAttribute("data-mutation")) control.disabled = false;
}
function refreshMutationLocks() {
  const taskActive = activeMutationContext !== null || refreshInProgress;
  for (const control of document.querySelectorAll("[data-mutation]")) {
    const busy = control.getAttribute("aria-busy") === "true";
    const prerequisite = control.dataset.prerequisite === "blocked";
    const anotherMutation = taskActive && !busy;
    control.disabled = busy || anotherMutation || prerequisite || !snapshotCurrent || runtimeMutationBlocked;
  }
  const refreshBusy = byId("refresh-dashboard").getAttribute("aria-busy") === "true";
  byId("refresh-dashboard").disabled = refreshBusy || taskActive;
  byId("retry-dashboard").disabled = refreshBusy || taskActive;
  const recommendation = byId("recommended-action");
  if (!recommendation.hasAttribute("data-mutation")) {
    recommendation.disabled = recommendation.getAttribute("aria-busy") === "true" || taskActive;
  }
}
function setDashboardBusy(busy) {
  for (const id of ["overview", "upstream-keys", "downstream-tokens", "events"]) byId(id).setAttribute("aria-busy", String(busy));
  byId("refresh-dashboard").disabled = busy;
  byId("retry-dashboard").disabled = busy;
  byId("refresh-dashboard").setAttribute("aria-busy", String(busy));
}
function showProblem(problem, context = "Refreshing administration state", snapshotOnly = false, knownNoSuccess = false) {
  const hasConfirmedResult = snapshotOnly && operationResult?.confirmed;
  const unconfirmedBeforeRefresh = snapshotOnly && operationStatus;
  const knownActionFailure = !snapshotOnly && knownNoSuccess;
  const settling = snapshotOnly && problem.code === "admin_mutation_settling";
  const settlingContext = settling ? unresolvedRecoveryContext() : null;
  const incompatible = snapshotOnly && problem.code === "incompatible_service";
  const message = settling
    ? settlingContext
      ? "This refresh overlapped an administration change, so the current snapshot was not confirmed. The service cannot identify whether that change was the unconfirmed operation in this tab. Refresh now."
      : "This refresh overlapped an administration change, so the current snapshot was not confirmed. The service cannot identify the change or its target from this tab. Do not start another change. Refresh now."
    : incompatible
      ? "This service version does not provide the required coherent dashboard. Upgrade the service; legacy reads are not used as a fallback."
      : unconfirmedBeforeRefresh
    ? `${context} did not complete. The current operation remains unconfirmed: ${operationStatus.message} Fresh state is still required before retrying.`
    : hasConfirmedResult
      ? `${context} did not complete. The last action result remains confirmed, but the full current snapshot is unavailable. Refresh current state before another change.`
    : snapshotOnly
      ? `${context} did not complete. Current administration state is unavailable. ${humanize(problem.code)}. Refresh current state before making a change.`
      : knownActionFailure
        ? `${context} did not complete. The service confirmed no successful result. ${humanize(problem.code)}. Refresh current state before retrying.`
      : `${context} did not complete. Success was not assumed. ${humanize(problem.code)}. Refresh current state before retrying.`;
  const state = settling ? "Refresh overlapped a change" : incompatible ? "Incompatible service" : unconfirmedBeforeRefresh ? "Action not confirmed" : hasConfirmedResult ? "Snapshot refresh not confirmed" : snapshotOnly ? "Current state not confirmed" : knownActionFailure ? "Action failed" : "Action not confirmed";
  setText("global-error-state", state);
  setText("global-error-message", message);
  byId("global-unconfirmed-operation").hidden = !settlingContext;
  if (settlingContext) setText("global-operation-status", `${settlingContext} remains unconfirmed. Do not repeat it.`);
  const globalEvidence = byId("global-error-evidence").closest("details");
  globalEvidence.open = false;
  renderProblemEvidence("global-error-evidence", problem);
  byId("global-error").hidden = false;
  pendingResultAnnouncement = null;
  const showConfirmedResult = Boolean(operationResult?.confirmed);
  byId("global-confirmed-result").hidden = !showConfirmedResult;
  if (showConfirmedResult) setText("global-confirmed-result", `Last confirmed result: ${operationResult.message}`);
  byId("decision-brief").hidden = true;
  markSnapshotStale("The last request did not confirm current administration state.");
  if (!currentSnapshot) renderUnavailableDecision();
  byId("global-error").focus();
}
