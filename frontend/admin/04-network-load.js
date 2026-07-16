async function login(event) {
  event.preventDefault();
  const field = byId("admin-bearer");
  if (!field.value) {
    setText("login-error-message", "Enter the current admin bearer.");
    byId("login-error").hidden = false;
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", "admin-bearer-help login-error");
    field.focus();
    return;
  }
  adminBearer = field.value;
  field.value = "";
  const submit = byId("login-submit");
  byId("login-busy").hidden = false;
  byId("login-busy").focus();
  field.disabled = true;
  submit.disabled = true;
  submit.setAttribute("aria-busy", "true");
  submit.textContent = "Authenticating…";
  const requestStartedAt = Date.now();
  try {
    const result = await api("/dashboard", {}, "dashboard", readDeadlineMs);
    if (result.status === 401) {
      mountLoginWithRecovery(true, false);
      return;
    }
    showDashboardShell();
    if (!result.ok) {
      showProblem(result.problem, "Loading administration state", true);
      return;
    }
    applyDashboard(result.value, requestStartedAt);
  } catch (error) {
    showDashboardShell();
    if (error instanceof AdminRequestTimeoutError) {
      showProblem({code: "admin_read_timeout", message: "current snapshot not confirmed", request_id: "unavailable"}, "Loading administration state", true);
    } else if (error instanceof AdminResponseError) {
      showProblem({code: "invalid_response", message: "response did not match the strict administration contract", request_id: "unavailable"}, "Loading administration state", true);
    } else if (error instanceof TypeError || error instanceof DOMException) {
      showProblem({code: "offline", message: "local service is unreachable", request_id: "unavailable"}, "Loading administration state", true);
    }
    else throw error;
  } finally {
    if (!byId("dashboard").hidden) {
      setDashboardBusy(false);
      refreshMutationLocks();
    }
  }
}

async function refreshDashboard(focusId = null) {
  if (refreshInProgress || activeMutationContext !== null) return;
  const refreshStartedAt = Date.now();
  refreshInProgress = true;
  const generation = requestGeneration;
  const restoreFocusId = focusId ?? document.activeElement?.id ?? null;
  const refreshControl = ["refresh-dashboard", "retry-dashboard", "recommended-action"].includes(focusId) ? byId(focusId) : null;
  const refreshIdleLabel = refreshControl?.textContent ?? null;
  const resultBeforeRefresh = operationResult;
  let userRefreshFocus = null;
  if (focusId && !["refresh-dashboard", "retry-dashboard", "recommended-action"].includes(focusId)) mutationRecoveryFocusId = focusId;
  setText("refresh-status", "Refreshing current administration state…");
  byId("refresh-status").hidden = false;
  byId("refresh-status").focus();
  if (refreshControl) setControlBusy(refreshControl, true, "Refreshing…");
  refreshMutationLocks();
  setDashboardBusy(true);
  byId("global-error").hidden = true;
  byId("decision-brief").hidden = false;
  try {
    const result = await api("/dashboard", {}, "dashboard", readDeadlineMs);
    if (!requestIsCurrent(generation)) return;
    if (result.status === 401) {
      mountLoginWithRecovery(true, false, "Refreshing administration state");
      return;
    }
    if (!result.ok) {
      showProblem(result.problem, "Refreshing administration state", true);
      return;
    }
    userRefreshFocus = captureRefreshFocus(refreshControl);
    const announcedResult = applyDashboard(result.value, refreshStartedAt);
    if (refreshControl && operationResult === resultBeforeRefresh && !announcedResult) {
      announce("Current administration state refreshed.");
    }
  } catch (error) {
    if (requestIsCurrent(generation) && error instanceof AdminRequestTimeoutError) {
      showProblem({code: "admin_read_timeout", message: "current snapshot not confirmed", request_id: "unavailable"}, "Refreshing administration state", true);
    } else if (requestIsCurrent(generation) && error instanceof AdminResponseError) {
      showProblem({code: "invalid_response", message: "response did not match the strict administration contract", request_id: "unavailable"}, "Refreshing administration state", true);
    } else if (requestIsCurrent(generation) && (error instanceof TypeError || error instanceof DOMException)) {
      showProblem({code: "offline", message: "local service is unreachable", request_id: "unavailable"}, "Refreshing administration state", true);
    } else if (!(error instanceof TypeError || error instanceof DOMException)) throw error;
  } finally {
    if (requestIsCurrent(generation)) {
      refreshInProgress = false;
      setDashboardBusy(false);
      if (refreshControl) {
        if (refreshControl.id === "recommended-action") {
          const nextLabel = refreshControl.textContent === "Refreshing…" ? refreshIdleLabel : refreshControl.textContent;
          refreshControl.dataset.idleLabel = nextLabel || refreshIdleLabel;
        }
        setControlBusy(refreshControl, false);
      }
      byId("refresh-status").hidden = true;
      refreshMutationLocks();
      if (hasSafeData && restoreFocusId && byId("global-error").hidden) {
        restoreRefreshFocus(userRefreshFocus, restoreFocusId);
        mutationRecoveryFocusId = null;
      }
    }
  }
}
