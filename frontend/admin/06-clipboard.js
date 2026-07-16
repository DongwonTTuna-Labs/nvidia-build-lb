function clipboardUsable() {
  return typeof navigator.clipboard?.writeText === "function" && typeof navigator.clipboard?.readText === "function";
}
function showClipboardError(message) {
  setText("clipboard-error-message", message);
  byId("clipboard-error").hidden = false;
  byId("clipboard-error").focus();
}
function setCredentialStatus(message) {
  setText("credential-status", message);
  byId("credential-status").hidden = false;
}
function setCredentialCopySurfaceLocked(locked) {
  const credential = byId("one-time-token");
  credential.inert = locked;
  credential.tabIndex = locked ? -1 : 0;
  credential.toggleAttribute("data-cleanup-locked", locked);
  if (locked) window.getSelection()?.removeAllRanges();
}
function resetCredentialCopySurface() {
  for (const id of ["copy-token", "dismiss-token"]) {
    const control = byId(id);
    control.disabled = false;
    control.setAttribute("aria-busy", "false");
    if (control.dataset.idleLabel) control.textContent = control.dataset.idleLabel;
    delete control.dataset.idleLabel;
  }
  setCredentialCopySurfaceLocked(false);
}
function blockCopyDuringCredentialCleanup(event) {
  if (credentialBusyOwner === "dismiss-token" && oneTimeToken) event.preventDefault();
}
function selectCredential(event) {
  if (!(event.key.toLowerCase() === "a" && (event.ctrlKey || event.metaKey))) return;
  event.preventDefault();
  byId("one-time-token").select();
  setCredentialStatus("Credential selected. Copy it with your browser command, then Dismiss to remove it and verify clipboard cleanup.");
}
function noteManualCredentialCopy() {
  copiedCredential = true;
  setCredentialStatus("Credential copied manually. Dismiss removes it from the page and verifies clipboard cleanup.");
}
function startCredentialTask(control, progressLabel, statusMessage) {
  credentialBusyOwner = control.id;
  setText("credential-busy", statusMessage);
  byId("credential-busy").hidden = false;
  byId("credential-dialog").setAttribute("aria-busy", "true");
  byId("credential-busy").focus();
  setControlBusy(control, true, progressLabel);
  if (control.id === "dismiss-token") {
    byId("copy-token").disabled = true;
    setCredentialCopySurfaceLocked(true);
  }
}
function finishCredentialTask(control) {
  setControlBusy(control, false);
  if (credentialBusyOwner !== control.id) {
    if (credentialBusyOwner === "dismiss-token") control.disabled = true;
    return;
  }
  credentialBusyOwner = null;
  byId("credential-busy").hidden = true;
  byId("credential-dialog").setAttribute("aria-busy", "false");
  if (control.id === "dismiss-token") {
    byId("copy-token").disabled = false;
    setCredentialCopySurfaceLocked(false);
  }
  if (byId("credential-dialog").open && byId("clipboard-error").hidden) control.focus();
}
async function copyToken() {
  if (!oneTimeToken || clipboardWritePending) return;
  if (!clipboardUsable()) {
    showClipboardError("Copy is unavailable. Store the selected credential manually. If clipboard access was available when this dialog opened, restore it before dismissal so cleanup can be verified.");
    return;
  }
  const generation = requestGeneration;
  clipboardWritePending = true;
  startCredentialTask(byId("copy-token"), "Copying…", "Copying the one-time credential…");
  try {
    clipboardWritePromise = navigator.clipboard.writeText(oneTimeToken);
    await clipboardWritePromise;
    if (!requestIsCurrent(generation)) {
      await navigator.clipboard.writeText("");
      return;
    }
    copiedCredential = true;
    setCredentialStatus("Credential copied. Dismiss removes it from the page and verifies clipboard cleanup.");
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    if (requestIsCurrent(generation)) showClipboardError("Copy or clipboard access failed. Restore clipboard access before dismissal so cleanup can be verified.");
  } finally {
    clipboardWritePromise = null;
    clipboardWritePending = false;
    if (requestIsCurrent(generation)) finishCredentialTask(byId("copy-token"));
    if (requestIsCurrent(generation) && copiedCredential) byId("copy-token").textContent = "Copy again";
  }
}

async function finishCredentialDismissal(recoveredClipboardFailure, clipboardUntouched) {
  const context = issuedTokenContext;
  oneTimeToken = null;
  copiedCredential = false;
  clipboardAvailableWhenIssued = false;
  issuedTokenContext = null;
  byId("one-time-token").value = "";
  setText("credential-title", "Store this credential now");
  setText("credential-target", "");
  byId("copy-token").textContent = "Copy credential";
  byId("credential-status").hidden = true;
  byId("credential-status").textContent = "";
  byId("clipboard-error").hidden = true;
  if (context) {
    const result = clipboardUntouched
      ? `Token for ${context.label} was issued once and removed from this page. The app never accessed the clipboard.`
      : `Token for ${context.label} was issued once, removed from this page, and the clipboard was verified empty.`;
    setOperationResult(context.id, recoveredClipboardFailure ? `Clipboard custody recovered. ${result}` : result);
    pendingResultAnnouncement = operationResult.message;
  }
  closeDialog("credential-dialog", "issue-downstream", false);
  await refreshDashboard("issue-downstream");
}

async function dismissToken() {
  const generation = requestGeneration;
  const control = byId("dismiss-token");
  const recoveredClipboardFailure = !byId("clipboard-error").hidden;
  if (control.disabled) return;
  startCredentialTask(control, "Removing credential…", "Removing the one-time credential and verifying clipboard cleanup…");
  try {
    if (!clipboardUsable() && !clipboardAvailableWhenIssued && !copiedCredential && !clipboardWritePending) {
      await finishCredentialDismissal(false, true);
      return;
    }
    if (!clipboardUsable()) {
      showClipboardError("Clipboard cleanup cannot be verified. Restore clipboard access, then Dismiss again. The credential remains visible and no success is assumed.");
      return;
    }
    if (clipboardWritePending) {
      try {
        await Promise.race([clipboardWritePromise, new Promise((resolve) => window.setTimeout(resolve, 500))]);
      } catch (error) {
        if (!(error instanceof DOMException)) throw error;
        if (byId("clipboard-error").hidden) {
          showClipboardError("Copy failed. The credential remains visible. Restore clipboard access, then Dismiss again.");
        }
        return;
      }
      if (clipboardWritePending) {
        showClipboardError("Copy is still in progress. Wait for it to finish, then Dismiss again.");
        return;
      }
    }
    try {
      await navigator.clipboard.writeText("");
      if ((await navigator.clipboard.readText()).length !== 0) throw new DOMException("clipboard is not empty");
    } catch (error) {
      if (!(error instanceof DOMException)) throw error;
      showClipboardError("Clipboard cleanup failed. Restore access or clear it manually, then Dismiss again. The credential remains visible.");
      return;
    }
    await finishCredentialDismissal(recoveredClipboardFailure, false);
  } finally {
    if (requestIsCurrent(generation) && control.isConnected) finishCredentialTask(control);
  }
}
