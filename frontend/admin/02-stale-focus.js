function showDialogStaleState(reason) {
  for (const dialog of document.querySelectorAll("dialog[open]")) {
    const warning = dialog.querySelector("[data-dialog-stale]");
    if (!warning) continue;
    warning.hidden = false;
    warning.textContent = `Current state expired. ${reason} Cancel this dialog, then refresh before deciding what to do next.`;
    setDialogDescription(dialog, warning.id, true);
  }
}
function clearDialogStaleState(dialog) {
  for (const warning of dialog.querySelectorAll("[data-dialog-stale]")) {
    warning.hidden = true;
    warning.textContent = "";
    setDialogDescription(dialog, warning.id, false);
  }
}
function clearSnapshotTimer() {
  if (snapshotTimer !== null) window.clearTimeout(snapshotTimer);
  snapshotTimer = null;
}
function captureOpenEvidenceIds() {
  return new Set([...document.querySelectorAll("details.resource-details[open] > summary[id]")]
    .map((summary) => summary.id));
}
function restoreOpenEvidence(ids) {
  for (const id of ids) byId(id)?.closest("details")?.setAttribute("open", "");
}
function captureRefreshFocus(refreshControl) {
  const active = document.activeElement;
  const taskOwned = active === byId("refresh-status") || active === refreshControl || active === document.body;
  return taskOwned ? null : {element: active, id: active?.id ?? null};
}
function restoreRefreshFocus(captured, restoreFocusId) {
  if (captured?.element?.isConnected) return;
  const replacement = captured?.id ? byId(captured.id) : null;
  if (replacement?.getClientRects().length && !replacement.disabled) { replacement.focus(); return; }
  const targetId = restoreFocusId === "retry-dashboard" && mutationRecoveryFocusId
    ? mutationRecoveryFocusId : restoreFocusId;
  const target = deleteFocusCandidateIds.length ? consumeDeleteFocusTarget() : byId(targetId);
  (target?.getClientRects().length && !target.disabled ? target : byId("dashboard-title")).focus();
}
function markSnapshotStale(reason, showDialogWarning = true) {
  const focusedElement = document.activeElement;
  const focusedMutation = focusedElement?.matches?.("[data-mutation]") ?? false;
  const focusedDialog = focusedElement?.closest?.("dialog[open]") ?? null;
  const focusedId = focusedElement?.id ?? null;
  const focusedDetailsOpen = focusedElement?.tagName === "SUMMARY"
    && Boolean(focusedElement.closest("details")?.open);
  const focusedBodyId = focusedElement?.closest?.("tbody")?.id ?? null;
  clearSnapshotTimer();
  snapshotCurrent = false;
  snapshotStaleReason = reason;
  runtimeMutationBlocked = false;
  refreshMutationLocks();
  if (showDialogWarning) showDialogStaleState(reason);
  if (currentSnapshot) {
    renderSnapshot(currentSnapshot);
  }
  if (focusedMutation) {
    const localRecovery = focusedDialog?.querySelector("[data-dialog-stale]:not([hidden])");
    const recovery = localRecovery ?? (!byId("recommended-action").hidden
      ? byId("recommended-action")
      : !byId("retry-dashboard").hidden ? byId("retry-dashboard") : byId("refresh-dashboard"));
    recovery.focus();
  } else if (focusedId) {
    const replacement = byId(focusedId);
    if (replacement?.getClientRects().length && !replacement.disabled) {
      if (focusedDetailsOpen) replacement.closest("details").open = true;
      replacement.focus();
    } else if (focusedBodyId) {
      const headingId = focusedBodyId === "upstream-body"
        ? "upstream-heading"
        : focusedBodyId === "downstream-body" ? "downstream-heading" : "events-heading";
      byId(headingId).focus();
    } else {
      const recovery = !byId("recommended-action").hidden
        ? byId("recommended-action")
        : !byId("retry-dashboard").hidden ? byId("retry-dashboard") : byId("refresh-dashboard");
      recovery.focus();
    }
  }
}
