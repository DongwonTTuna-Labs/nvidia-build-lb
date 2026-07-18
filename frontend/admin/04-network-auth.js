function clearSensitiveUi(preserveRecovery = false) {
  resetSessionController();
  clearSnapshotTimer();
  adminBearer = null;
  oneTimeToken = null;
  copiedCredential = false;
  clipboardAvailableWhenIssued = false;
  clipboardWritePending = false;
  clipboardWritePromise = null;
  credentialBusyOwner = null;
  hasSafeData = false;
  snapshotCurrent = false;
  refreshInProgress = false;
  snapshotStaleReason = null;
  runtimeMutationBlocked = false;
  currentSnapshot = null;
  unconfirmedProbeKeyIds = new Set();
  if (!preserveRecovery) { operationResult = null; operationStatus = null; pendingResultAnnouncement = null; }
  recommendedPlan = null;
  issuedTokenContext = null;
  activeMutationContext = null;
  if (!preserveRecovery) {
    unresolvedAdd = null;
    unresolvedIssue = null;
    unresolvedMutation = null;
    orphanedToken = null;
    replacementContext = null;
  }
  pendingReplacementSourceId = null;
  if (!preserveRecovery) { enableReadyKeyEvidence = new Map(); deliberatelyPausedKeyIds = new Map(); } mutationRecoveryFocusId = null;
  pendingAction = null; deleteFocusCandidateIds = [];
  lastInvoker = null;
  upstreamItems = [];
  downstreamItems = [];
  upstreamHandleHistory = new Map();
  downstreamHandleHistory = new Map();
  byId("one-time-token").value = "";
  setText("credential-title", "Store this credential now");
  setText("credential-target", "");
  byId("credential-id").value = "";
  byId("copy-token").textContent = "Copy one-time bearer";
  byId("credential-status").hidden = true;
  byId("credential-status").textContent = "";
  byId("credential-busy").hidden = true;
  byId("refresh-status").hidden = true;
  byId("global-error").hidden = true;
  byId("decision-brief").hidden = false;
  byId("navigation-disclosure").open = false;
  byId("navigation-disclosure").hidden = true;
  for (const link of document.querySelectorAll("#navigation-disclosure nav a")) link.removeAttribute("aria-current");
  document.querySelector("#navigation-disclosure nav a[href='#overview']")?.setAttribute("aria-current", "location");
  document.body.classList.remove("authenticated");
  for (const dialog of document.querySelectorAll("dialog")) {
    clearDialogStaleState(dialog);
    dialog.querySelector("form")?.reset();
    for (const status of dialog.querySelectorAll("[data-dialog-busy]")) {
      status.hidden = true;
      setDialogDescription(dialog, status.id, false);
    }
    for (const control of dialog.querySelectorAll('[aria-busy="true"]')) {
      control.setAttribute("aria-busy", "false");
      control.disabled = false;
      if (control.dataset.idleLabel) control.textContent = control.dataset.idleLabel;
    }
    for (const field of dialog.querySelectorAll('[data-busy-disabled="true"]')) {
      field.disabled = false;
      delete field.dataset.busyDisabled;
    }
    if (dialog.open) dialog.close();
  }
  resetCredentialCopySurface();
}
function mountLogin(authFailed = false, serviceOffline = false, interrupted = null, recovery = null) {
  clearSensitiveUi(Boolean(interrupted || recovery));
  byId("dashboard").hidden = true;
  byId("logout").hidden = true;
  byId("auth-view").hidden = false;
  byId("login-slot").replaceChildren(loginTemplate.cloneNode(true));
  const field = byId("admin-bearer");
  const submit = byId("login-submit");
  field.disabled = false;
  submit.disabled = false;
  const hasRecovery = Boolean(interrupted || recovery);
  if (hasRecovery) {
    setText("login-interrupted", [interrupted ? `${interrupted} was interrupted. Its result is unknown; reauthenticate and refresh before retrying.` : null, recovery].filter(Boolean).join(" "));
    byId("login-interrupted").hidden = false;
  }
  if (authFailed) {
    setText("login-error-message", "Authentication expired or failed. Enter the current admin bearer.");
    byId("login-error").hidden = false;
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", hasRecovery ? "admin-bearer-help login-error login-interrupted" : "admin-bearer-help login-error");
    byId("login-error").focus();
  } else if (serviceOffline) {
    byId("login-offline").hidden = false;
    field.setAttribute("aria-describedby", hasRecovery ? "admin-bearer-help login-offline login-interrupted" : "admin-bearer-help login-offline");
    byId("login-offline").focus();
  } else {
    field.focus();
  }
  if (hasRecovery) {
    if (!authFailed && !serviceOffline) {
      setText("login-error-message", "Recovery is pending. Enter the current admin bearer to verify fresh state.");
      byId("login-error").hidden = false;
      field.setAttribute("aria-describedby", "admin-bearer-help login-error login-interrupted");
      byId("login-error").focus();
    }
  }
  field.addEventListener("input", () => {
    field.removeAttribute("aria-invalid");
    byId("login-error").hidden = true;
    field.setAttribute("aria-describedby", hasRecovery ? "admin-bearer-help login-interrupted" : "admin-bearer-help");
  });
  byId("login-form").addEventListener("submit", login);
  const loginFocusTarget = document.activeElement;
  window.requestAnimationFrame(() => { if (loginFocusTarget?.isConnected && document.activeElement === document.body) loginFocusTarget.focus(); });
}
function mountLoginWithRecovery(authFailed, serviceOffline, interrupted = null) {
  const confirmedContext = operationResult?.confirmed ? "Verifying current state after the last confirmed action" : null;
  const unresolvedContext = activeMutationContext ?? unresolvedRecoveryContext();
  const context = unresolvedContext ? (interrupted && interrupted !== unresolvedContext ? `${interrupted} for the unconfirmed operation: ${unresolvedContext}` : unresolvedContext) : (interrupted && confirmedContext ? `${interrupted} after the last confirmed action` : interrupted ?? confirmedContext);
  mountLogin(authFailed, serviceOffline, context, orphanRecoveryMessage());
}

function showDashboardShell() {
  byId("login-slot").replaceChildren();
  byId("auth-view").hidden = true;
  byId("dashboard").hidden = false;
  byId("logout").hidden = false;
  byId("navigation-disclosure").hidden = false;
  syncNavigationDisclosure();
  resetAdminNavigation();
  document.body.classList.add("authenticated");
  byId("dashboard-title").focus();
}

function applyDashboard(dashboard, requestStartedAt) {
  if (!snapshotIsCoherent(dashboard)) {
    throw new AdminResponseError("dashboard invariants do not match the strict administration contract");
  }
  snapshotCurrent = true;
  snapshotStaleReason = null;
  runtimeMutationBlocked = ["runtime_unavailable", "ledger_capacity_exhausted"].includes(dashboard.readiness_cause);
  hasSafeData = true;
  const elapsedDuringRequest = Math.max(0, Date.now() - requestStartedAt);
  const referenceAt = new Date(Date.parse(dashboard.overview.generated_at) + elapsedDuringRequest).toISOString();
  currentSnapshot = {
    runtimeState: dashboard.runtime_state,
    readinessCause: dashboard.readiness_cause,
    ledger: dashboard.ledger,
    overview: dashboard.overview,
    upstreams: dashboard.upstream_keys.items,
    downstreams: dashboard.downstream_tokens.items,
    events: dashboard.events.items,
    referenceAt,
  };
  for (const dialog of document.querySelectorAll("dialog")) clearDialogStaleState(dialog);
  reconcileUnresolvedAdd(currentSnapshot.upstreams);
  reconcileUnresolvedIssue(currentSnapshot.downstreams);
  reconcileUnresolvedMutation(currentSnapshot.upstreams, currentSnapshot.downstreams);
  reconcileOrphanedToken(currentSnapshot.downstreams);
  byId("global-error").hidden = true;
  byId("decision-brief").hidden = false;
  renderSnapshot(currentSnapshot);
  const announcedResult = Boolean(pendingResultAnnouncement);
  if (pendingResultAnnouncement) announce(pendingResultAnnouncement);
  scheduleSnapshotExpiry(requestStartedAt);
  pendingResultAnnouncement = null;
  return announcedResult;
}
