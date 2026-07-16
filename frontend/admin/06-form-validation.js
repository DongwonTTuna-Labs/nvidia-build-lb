function clearFieldError(control, helpId) {
  control.removeAttribute("aria-invalid");
  control.setAttribute("aria-describedby", helpId);
}
function showUpstreamError(message, fieldInvalid = true, problem = null) {
  const field = byId("upstream-key");
  byId("upstream-error").dataset.owner = fieldInvalid ? "field" : "action";
  setText("upstream-error-message", message); setProblemEvidence("upstream-error", problem);
  byId("upstream-error").hidden = false;
  if (fieldInvalid) {
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", "upstream-key-help upstream-error");
    field.focus();
  } else {
    clearFieldError(field, "upstream-key-help");
    byId("upstream-error").focus();
  }
}
function clearUpstreamError(force = true) {
  if (!force && byId("upstream-error").dataset.owner === "action") return;
  byId("upstream-error").hidden = true; setProblemEvidence("upstream-error");
  delete byId("upstream-error").dataset.owner;
  clearFieldError(byId("upstream-key"), "upstream-key-help");
}
function scopeControls() { return [...document.querySelectorAll("input[name='scope']")]; }
function showDownstreamError(message, labelInvalid = true, scopesInvalid = true, focusInvalid = true, problem = null) {
  const labelField = byId("downstream-label");
  const scopesField = byId("downstream-scopes");
  const scopeFields = scopeControls();
  const error = byId("downstream-error");
  error.dataset.owner = labelInvalid || scopesInvalid ? "field" : "action";
  setText("downstream-error-message", message); setProblemEvidence("downstream-error", problem);
  error.hidden = false;
  if (labelInvalid) {
    labelField.setAttribute("aria-invalid", "true");
    labelField.setAttribute("aria-describedby", "downstream-label-help downstream-error");
  } else clearFieldError(labelField, "downstream-label-help");
  if (scopesInvalid) {
    scopesField.setAttribute("aria-invalid", "true");
    scopesField.setAttribute("aria-describedby", "scope-help downstream-error");
    for (const scope of scopeFields) {
      scope.setAttribute("aria-invalid", "true");
      scope.setAttribute("aria-describedby", "scope-help downstream-error");
    }
  } else {
    clearFieldError(scopesField, "scope-help");
    for (const scope of scopeFields) clearFieldError(scope, "scope-help");
  }
  if (focusInvalid) {
    const target = labelInvalid ? labelField : scopesInvalid ? scopeFields[0] : byId("downstream-error");
    target.focus();
  }
}
function clearDownstreamError(force = true) {
  if (!force && byId("downstream-error").dataset.owner === "action") return;
  byId("downstream-error").hidden = true; setProblemEvidence("downstream-error");
  delete byId("downstream-error").dataset.owner;
  clearFieldError(byId("downstream-label"), "downstream-label-help");
  clearFieldError(byId("downstream-scopes"), "scope-help");
  for (const scope of scopeControls()) clearFieldError(scope, "scope-help");
}
function revalidateDownstreamError() {
  if (byId("downstream-error").hidden) return;
  if (byId("downstream-error").dataset.owner === "action") return;
  const labelValue = byId("downstream-label").value;
  const labelInvalid = !labelValue.trim() || labelValue !== labelValue.trim();
  const scopesInvalid = document.querySelectorAll("input[name='scope']:checked").length === 0;
  if (!labelInvalid && !scopesInvalid) {
    clearDownstreamError();
    return;
  }
  const message = labelInvalid && scopesInvalid
    ? "Enter a unique label without surrounding spaces and choose at least one scope."
    : labelInvalid
      ? "Enter a unique label without surrounding spaces."
      : "Choose at least one scope.";
  if (byId("downstream-error-message").textContent === message) return;
  showDownstreamError(message, labelInvalid, scopesInvalid, false);
}
