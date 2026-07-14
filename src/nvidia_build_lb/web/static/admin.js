const byId = (id) => document.getElementById(id); const apiRoot = "/admin/api/v1";
let adminBearer = null, oneTimeToken = null, copiedCredential = false, requestController = new AbortController(), requestGeneration = 0, hasSafeData = false, upstreamItems = [], lastInvoker = null, pendingAction = null, clipboardWritePending = false, clipboardWritePromise = null;

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/; const timestampPattern = /^(?!0000)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?Z$/;
const oneOf = (...values) => (value) => values.includes(value); const nullable = (check) => (value) => value === null || check(value);
const text = (value) => typeof value === "string" && value.length > 0; const counter = (value) => Number.isSafeInteger(value) && value >= 0 || typeof value === "bigint" && value >= 0n && value <= 9223372036854775807n;
const uuid = (value) => typeof value === "string" && uuidPattern.test(value); const timestamp = (value) => typeof value === "string" && timestampPattern.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
const fingerprint = (value) => typeof value === "string" && /^sha256:[0-9a-f]{64}$/.test(value); const token = (value) => typeof value === "string" && /^nblb_ds_[0-9a-f]{64}$/.test(value);
const label = (value) => text(value) && [...value].length <= 128 && value === value.trim() && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value);
const scopeSets = [["models:read"], ["chat:write"], ["models:read", "chat:write"]]; const scopes = (value) => Array.isArray(value) && scopeSets.some((expected) => expected.length === value.length && expected.every((scope, index) => scope === value[index]));
const healthState = oneOf("unknown", "healthy", "degraded"); const statusClass = oneOf("success", "invalid_credential", "credits_exhausted", "rate_limited", "request_rejected", "timeout", "upstream_unavailable", "upstream_bad_gateway", "upstream_internal_error", "upstream_protocol_error", "delivery_failed", "cancelled");
const safeCode = oneOf("host_forbidden", "origin_forbidden", "unauthorized", "admin_unauthorized", "insufficient_scope", "model_not_found", "resource_not_found", "resource_conflict", "invalid_request", "no_upstream_keys", "database_unavailable", "upstream_auth_error", "upstream_credits_exhausted", "upstream_timeout", "upstream_rate_limited", "upstream_request_rejected", "upstream_internal_error", "upstream_bad_gateway", "upstream_unavailable", "upstream_protocol_error", "poll_timeout");
function matches(schema, value) {
  if (typeof schema === "function") return schema(value);
  if (Array.isArray(schema)) return Array.isArray(value) && value.every((item) => matches(schema[0], item));
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(schema); return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key) && matches(schema[key], value[key]));
}
const upstreamDto = {id: uuid, fingerprint, enabled: (value) => typeof value === "boolean", health_state: healthState, cooldown_until: nullable(timestamp), request_count: counter, success_count: counter, failure_count: counter, last_status_class: nullable(statusClass), last_used_at: nullable(timestamp), created_at: timestamp, updated_at: timestamp};
const downstreamDto = {id: uuid, label, scopes, revoked_at: nullable(timestamp), request_count: counter, last_used_at: nullable(timestamp), created_at: timestamp};
const eventDto = {id: uuid, request_id: text, event_type: oneOf("upstream_key_created", "upstream_key_enabled", "upstream_key_disabled", "upstream_key_deleted", "upstream_probe", "downstream_token_issued", "downstream_token_revoked", "upstream_attempt"), upstream_key_id: nullable(uuid), downstream_token_id: nullable(uuid), outcome_class: oneOf("started", "succeeded", "failed", "cancelled"), status_class: nullable(statusClass), latency_ms: nullable(counter), occurred_at: timestamp};
const overviewDto = {status: oneOf("ok", "degraded"), ready: (value) => typeof value === "boolean", upstream_keys: {total: counter, enabled: counter, eligible: counter, cooling: counter, degraded: counter}, downstream_tokens: {total: counter, active: counter, revoked: counter}, request_count: counter, last_event_at: nullable(timestamp), generated_at: timestamp};
const errorDto = {code: safeCode, message: text, request_id: text};
const dtoSchemas = {overview: overviewDto, upstream: upstreamDto, upstreamList: {items: [upstreamDto]}, downstreamList: {items: [downstreamDto]}, eventList: (value) => matches({items: [eventDto]}, value) && value.items.length <= 100, probe: {id: uuid, enabled: (value) => typeof value === "boolean", probe_status: oneOf("valid", "invalid_credential", "rate_limited", "upstream_unavailable"), observed_at: timestamp}, issued: {...downstreamDto, token}, error: {error: errorDto}, validationError: {error: {code: (value) => value === "invalid_request", message: (value) => value === "request validation failed", request_id: text}}};
function parseAdminDto(kind, value) {
  if (!Object.hasOwn(dtoSchemas, kind) || !matches(dtoSchemas[kind], value)) throw new TypeError("invalid administration response");
  return value;
}
function parseJson(source) { return JSON.parse(source, (key, value, context) => {
    if (typeof value !== "number") return value;
    if (!/^(?:0|[1-9]\d*)$/.test(context.source)) throw new TypeError("invalid administration counter");
    return BigInt(context.source) <= BigInt(Number.MAX_SAFE_INTEGER) ? value : BigInt(context.source);
  }); }

function announce(message) { byId("live-region").textContent = message; }
function setText(id, value) { byId(id).textContent = value ?? "Not available"; }
function displayTime(value) { return value ?? "Not available"; }
function shortFingerprint(value) { return `${value.slice(0, 23)}…`; }
function createCell(label, value, className = "") {
  const cell = document.createElement("td"); cell.dataset.label = label; cell.textContent = value;
  if (className) cell.className = className;
  return cell;
}
function createButton(label, id, handler) {
  const button = document.createElement("button");
  button.className = "control secondary"; button.id = id; button.type = "button"; button.textContent = label;
  button.addEventListener("click", handler); return button;
}
function tableMessage(bodyId, span, message) {
  const row = document.createElement("tr"); const cell = createCell("State", message);
  cell.colSpan = span; row.append(cell); byId(bodyId).replaceChildren(row);
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers ?? {}); headers.set("Accept", "application/json");
  headers.set("Authorization", `Bearer ${adminBearer}`);
  return fetch(`${apiRoot}${path}`, {...options, cache: "no-store", credentials: "omit", headers, redirect: "error", referrerPolicy: "no-referrer", signal: requestController.signal});
}
async function responseProblem(response) {
  const fallback = {code: "request_failed", message: "safe request failed", request_id: "unavailable"};
  try {
    const payload = parseJson(await response.text()); const kind = response.status === 422 ? "validationError" : "error";
    return parseAdminDto(kind, payload).error;
  } catch (error) { if (!(error instanceof SyntaxError || error instanceof TypeError)) throw error; return fallback; }
}
function showProblem(problem) {
  const message = `${problem.code} · ${problem.message} · Request ${problem.request_id}`;
  if (hasSafeData) { byId("stale-warning").hidden = false; announce("Safe data is stale because refresh failed."); return; }
  setText("global-error-message", message); byId("global-error").hidden = false; byId("global-error").focus();
}
function setDashboardBusy(busy) {
  for (const id of ["overview", "upstream-keys", "downstream-tokens", "events"]) byId(id).setAttribute("aria-busy", String(busy));
  byId("refresh-dashboard").disabled = busy; byId("refresh-dashboard").setAttribute("aria-busy", String(busy));
}
function resetRequestController() { requestGeneration += 1; requestController.abort(); requestController = new AbortController(); }
function requestIsCurrent(generation) { return generation === requestGeneration; }
function setControlBusy(id, busy) { const control = byId(id); control.disabled = busy; control.setAttribute("aria-busy", String(busy)); }
function mountLogin(authFailed = false) {
  adminBearer = null; oneTimeToken = null; copiedCredential = false; hasSafeData = false;
  byId("one-time-token").textContent = ""; byId("clipboard-recovery").hidden = true; byId("dashboard").hidden = true; byId("logout").hidden = true; byId("auth-view").hidden = false;
  byId("login-slot").replaceChildren(byId("login-template").content.cloneNode(true));
  const field = byId("admin-bearer");
  if (authFailed) { byId("login-error").hidden = false; field.setAttribute("aria-invalid", "true"); field.setAttribute("aria-describedby", "admin-bearer-help login-error"); byId("login-error").focus(); }
  else field.focus();
  byId("login-form").addEventListener("submit", login);
}
async function login(event) {
  event.preventDefault(); const field = byId("admin-bearer"); adminBearer = field.value; field.value = "";
  const submit = byId("login-submit"); submit.disabled = true; submit.setAttribute("aria-busy", "true");
  try {
    const response = await api("/overview");
    if (response.status === 401) { mountLogin(true); return; }
    byId("login-slot").replaceChildren(); byId("auth-view").hidden = true; byId("dashboard").hidden = false; byId("logout").hidden = false; byId("dashboard-title").focus();
    if (!response.ok) { showProblem(await responseProblem(response)); return; }
    parseAdminDto("overview", parseJson(await response.text())); await refreshDashboard();
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    mountLogin(false); byId("login-error").hidden = false; byId("login-error").focus();
  }
}
function renderOverview(overview) {
  setText("gateway-status", overview.ready ? "OK · Ready" : "WARN · Degraded");
  setText("eligible-count", String(overview.upstream_keys.eligible)); setText("cooling-count", String(overview.upstream_keys.cooling));
  setText("request-count", String(overview.request_count)); setText("active-token-count", String(overview.downstream_tokens.active));
  setText("last-event-at", displayTime(overview.last_event_at)); setText("generated-at", `Generated ${overview.generated_at}`);
}
function renderUpstreams(items) {
  upstreamItems = items;
  if (!items.length) { tableMessage("upstream-body", 6, "No upstream keys registered. Add one to begin safe validation."); return; }
  const rows = items.map((item) => {
    const row = document.createElement("tr"); const keyCell = createCell("Key", "", "machine cell-stack");
    const id = document.createElement("span"); id.textContent = item.id;
    const visible = document.createElement("span"); visible.setAttribute("aria-hidden", "true"); visible.textContent = shortFingerprint(item.fingerprint);
    const full = document.createElement("span"); full.className = "visually-hidden"; full.textContent = item.fingerprint; keyCell.append(id, visible, full);
    const routing = item.enabled ? (item.cooldown_until ? "COOL · Cooling down" : "ON · Enabled") : "OFF · Disabled";
    const routeCell = createCell("Routing", routing, "status"); routeCell.dataset.status = item.cooldown_until ? "cooldown" : item.enabled ? "healthy" : "disabled";
    const healthCell = createCell("Health", `${item.health_state} · ${item.last_status_class ?? "No status"}`, "status"); healthCell.dataset.status = item.health_state;
    const counters = createCell("Counters", `Requests ${item.request_count} · Success ${item.success_count} · Failure ${item.failure_count}`, "machine");
    const observed = createCell("Last observation", `Used ${displayTime(item.last_used_at)} · Updated ${item.updated_at}`, "machine");
    const actions = createCell("Actions", "", "actions");
    const probe = createButton("Probe", `key-${item.id}-probe`, (event) => runKeyAction(item, "probe", event.currentTarget));
    const toggle = createButton(item.enabled ? "Disable" : "Enable", `key-${item.id}-toggle`, (event) => item.enabled ? openConfirmation("disable", item, event.currentTarget) : runKeyAction(item, "enable", event.currentTarget));
    const remove = createButton("Delete", `key-${item.id}-delete`, (event) => openConfirmation("delete", item, event.currentTarget));
    if (item.enabled) {
      remove.disabled = true; remove.setAttribute("aria-describedby", `key-${item.id}-delete-reason`);
      const reason = document.createElement("span"); reason.id = `key-${item.id}-delete-reason`; reason.textContent = "Delete unavailable while enabled."; actions.append(probe, toggle, remove, reason);
    } else actions.append(probe, toggle, remove);
    row.append(keyCell, routeCell, healthCell, counters, observed, actions); return row;
  });
  byId("upstream-body").replaceChildren(...rows);
}
function renderDownstreams(items) {
  if (!items.length) { tableMessage("downstream-body", 5, "No downstream tokens issued. Issue one with the minimum scopes."); return; }
  const rows = items.map((item) => {
    const row = document.createElement("tr"); const client = createCell("Client", "", "cell-stack"); const human = document.createElement("span"); human.className = "human-label"; human.textContent = item.label; const machine = document.createElement("span"); machine.className = "machine-id"; machine.textContent = item.id; client.append(human, machine);
    const scopes = createCell("Scopes", item.scopes.join(" · "), "machine");
    const state = createCell("State", item.revoked_at ? `REVOKED · ${item.revoked_at}` : "ACTIVE · Available", "status"); state.dataset.status = item.revoked_at ? "revoked" : "healthy";
    const usage = createCell("Usage", `Requests ${item.request_count} · Last ${displayTime(item.last_used_at)} · Created ${item.created_at}`, "machine");
    const actions = createCell("Actions", "", "actions"); const actionId = `token-${item.id}-revoke`;
    if (item.revoked_at) {
      const revoked = document.createElement("span"); revoked.id = actionId; revoked.className = "status"; revoked.dataset.status = "revoked"; revoked.tabIndex = -1; revoked.textContent = "REVOKED · No action available"; actions.append(revoked);
    } else actions.append(createButton("Revoke", actionId, (event) => openConfirmation("revoke", item, event.currentTarget)));
    row.append(client, scopes, state, usage, actions); return row;
  });
  byId("downstream-body").replaceChildren(...rows);
}
function renderEvents(items) {
  if (!items.length) { tableMessage("events-body", 5, "No recent events. Safe activity will appear here."); return; }
  const rows = items.map((item) => {
    const row = document.createElement("tr"); const resource = item.upstream_key_id ?? item.downstream_token_id ?? "Not available";
    row.append(createCell("Event", `${item.event_type} · ${item.id}`, "machine"), createCell("Request", item.request_id, "machine"), createCell("Resource", resource, "machine"), createCell("Outcome", `${item.outcome_class} · ${item.status_class ?? "No status"} · ${item.latency_ms ?? "No latency"}`), createCell("Observed", item.occurred_at, "machine")); return row;
  });
  byId("events-body").replaceChildren(...rows);
}
async function refreshDashboard(focusId = null) {
  const generation = requestGeneration; const restoreFocusId = focusId ?? document.activeElement?.id ?? null;
  setDashboardBusy(true); byId("global-error").hidden = true; byId("stale-warning").hidden = true;
  try {
    const responses = await Promise.all(["/overview", "/upstream-keys", "/downstream-tokens", "/events"].map((path) => api(path)));
    if (!requestIsCurrent(generation)) return;
    if (responses.some((response) => response.status === 401)) { mountLogin(true); return; }
    const failed = responses.find((response) => !response.ok);
    if (failed) { showProblem(await responseProblem(failed)); return; }
    const kinds = ["overview", "upstreamList", "downstreamList", "eventList"];
    const [overview, upstreams, downstreams, events] = await Promise.all(responses.map(async (response, index) => parseAdminDto(kinds[index], parseJson(await response.text())))); if (!requestIsCurrent(generation)) return;
    renderOverview(overview); renderUpstreams(upstreams.items); renderDownstreams(downstreams.items); renderEvents(events.items);
    hasSafeData = true; announce("Safe administration state refreshed.");
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    if (requestIsCurrent(generation)) showProblem({code: "offline", message: "local service is unreachable", request_id: "unavailable"});
  } finally { if (requestIsCurrent(generation)) { setDashboardBusy(false); if (hasSafeData && restoreFocusId) { const target = byId(restoreFocusId); (target?.getClientRects().length && !target.disabled ? target : byId("dashboard-title")).focus(); } } }
}
function openDialog(id, invoker, focusId) { lastInvoker = invoker; byId(id).showModal(); byId(focusId).focus(); }
function closeDialog(id, focusId = null, cancelRequest = true) {
  const dialog = byId(id); if (cancelRequest && ["upstream-dialog", "downstream-dialog", "confirm-dialog"].includes(id)) resetRequestController(); dialog.querySelector("form")?.reset(); for (const control of dialog.querySelectorAll('[aria-busy="true"]')) { control.disabled = false; control.setAttribute("aria-busy", "false"); } if (id === "confirm-dialog") pendingAction = null; dialog.close(); const target = focusId ? byId(focusId) : lastInvoker; target?.focus(); lastInvoker = null;
}
async function runKeyAction(item, action, button) { const generation = requestGeneration; button.disabled = true; button.setAttribute("aria-busy", "true"); try {
    const response = await api(`/upstream-keys/${item.id}/${action}`, {method: "POST"});
    if (!requestIsCurrent(generation)) return;
    if (!response.ok) { showProblem(await responseProblem(response)); return; }
    if (action === "probe") { const result = parseAdminDto("probe", parseJson(await response.text())); announce(`Probe result ${result.probe_status}.`); }
    await refreshDashboard(`key-${item.id}-${action === "enable" ? "toggle" : "probe"}`);
  } catch (error) { if (!(error instanceof TypeError || error instanceof DOMException)) throw error; if (requestIsCurrent(generation)) showProblem({code: "offline", message: "local service is unreachable", request_id: "unavailable"}); }
  finally { if (requestIsCurrent(generation) && button.isConnected) { button.disabled = false; button.setAttribute("aria-busy", "false"); } }
}
function openConfirmation(kind, item, invoker) {
  const fingerprint = item.fingerprint ? shortFingerprint(item.fingerprint) : item.label;
  const descriptions = {disable: `Disable key ${item.id} (${fingerprint})? New requests will stop selecting it.`, delete: `Permanently delete disabled key ${item.id} (${fingerprint})?`, revoke: `Revoke downstream token ${item.id} (${fingerprint})? This cannot be undone.`};
  pendingAction = {kind, item}; setText("confirm-title", kind === "delete" ? "Delete upstream key" : kind === "revoke" ? "Revoke downstream token" : "Disable upstream key");
  setText("confirm-description", descriptions[kind]); setText("confirm-action", kind === "delete" ? "Delete key" : kind === "revoke" ? "Revoke token" : "Disable key");
  byId("confirm-error").hidden = true; openDialog("confirm-dialog", invoker, "confirm-title");
}
async function confirmPendingAction() {
  if (!pendingAction) return;
  const generation = requestGeneration; const {kind, item} = pendingAction; const path = kind === "revoke" ? `/downstream-tokens/${item.id}` : kind === "delete" ? `/upstream-keys/${item.id}` : `/upstream-keys/${item.id}/disable`;
  setControlBusy("confirm-action", true);
  try { const response = await api(path, {method: kind === "disable" ? "POST" : "DELETE"}); if (!requestIsCurrent(generation)) return;
    if (!response.ok) { setText("confirm-error", `${(await responseProblem(response)).code} · Safe action failed.`); byId("confirm-error").hidden = false; byId("confirm-error").focus(); return; }
    let focusId = kind === "revoke" ? `token-${item.id}-revoke` : `key-${item.id}-toggle`;
    if (kind === "delete") { const index = upstreamItems.findIndex((candidate) => candidate.id === item.id); const neighbor = upstreamItems[index + 1] ?? upstreamItems[index - 1]; focusId = neighbor ? `key-${neighbor.id}-toggle` : "upstream-heading"; }
    closeDialog("confirm-dialog", null, false); pendingAction = null; await refreshDashboard(focusId);
  } catch (error) { if (!(error instanceof TypeError || error instanceof DOMException)) throw error; if (requestIsCurrent(generation)) { setText("confirm-error", "offline · Safe action failed."); byId("confirm-error").hidden = false; byId("confirm-error").focus(); } }
  finally { if (requestIsCurrent(generation) && byId("confirm-action")) setControlBusy("confirm-action", false); }
}
async function submitUpstream(event) {
  event.preventDefault(); const generation = requestGeneration; let opaqueKey = byId("upstream-key").value; byId("upstream-key").value = ""; let body = JSON.stringify({key: opaqueKey}); setControlBusy("submit-upstream", true);
  try { const response = await api("/upstream-keys", {method: "POST", headers: {"Content-Type": "application/json"}, body}); if (!requestIsCurrent(generation)) return;
    if (!response.ok) { setText("upstream-error", `${(await responseProblem(response)).code} · Safe add failed.`); byId("upstream-error").hidden = false; byId("upstream-error").focus(); return; }
    parseAdminDto("upstream", parseJson(await response.text())); closeDialog("upstream-dialog", "add-upstream", false); await refreshDashboard("add-upstream");
  } catch (error) { if (!(error instanceof TypeError || error instanceof DOMException)) throw error; if (requestIsCurrent(generation) && byId("upstream-dialog").open) { setText("upstream-error", "offline · Safe add failed."); byId("upstream-error").hidden = false; byId("upstream-error").focus(); } }
  finally { opaqueKey = ""; body = ""; if (requestIsCurrent(generation) && byId("submit-upstream")) setControlBusy("submit-upstream", false); }
}
async function submitDownstream(event) {
  event.preventDefault(); const generation = requestGeneration; const scopes = [...document.querySelectorAll("input[name='scope']:checked")].map((input) => input.value); let body = JSON.stringify({label: byId("downstream-label").value, scopes}); setControlBusy("submit-downstream", true);
  try { const response = await api("/downstream-tokens", {method: "POST", headers: {"Content-Type": "application/json"}, body}); if (!requestIsCurrent(generation)) return;
    if (!response.ok) { setText("downstream-error", `${(await responseProblem(response)).code} · Safe issue failed.`); byId("downstream-error").hidden = false; byId("downstream-error").focus(); return; }
    const issued = parseAdminDto("issued", parseJson(await response.text())); oneTimeToken = issued.token; byId("one-time-token").textContent = oneTimeToken; byId("downstream-form").reset();
    closeDialog("downstream-dialog", null, false); openDialog("credential-dialog", byId("issue-downstream"), "credential-title");
  } catch (error) { if (!(error instanceof TypeError || error instanceof DOMException)) throw error; if (requestIsCurrent(generation) && byId("downstream-dialog").open) { setText("downstream-error", "offline · Safe issue failed."); byId("downstream-error").hidden = false; byId("downstream-error").focus(); } }
  finally { body = ""; if (requestIsCurrent(generation) && byId("submit-downstream")) setControlBusy("submit-downstream", false); }
}
async function copyToken() {
  if (!oneTimeToken || clipboardWritePending) return;
  if (!navigator.clipboard) { byId("clipboard-error").hidden = false; byId("clipboard-error").focus(); return; }
  const generation = requestGeneration; clipboardWritePending = true; setControlBusy("copy-token", true);
  try { clipboardWritePromise = navigator.clipboard.writeText(oneTimeToken); await clipboardWritePromise; if (!requestIsCurrent(generation)) { await navigator.clipboard.writeText(""); return; } copiedCredential = true; announce("Credential copied. Dismiss to clear it from the page and clipboard."); }
  catch (error) { if (!(error instanceof DOMException)) throw error; if (requestIsCurrent(generation)) { byId("clipboard-error").hidden = false; byId("clipboard-error").focus(); } }
  finally { clipboardWritePromise = null; clipboardWritePending = false; if (requestIsCurrent(generation)) setControlBusy("copy-token", false); }
}
async function dismissToken() {
  const generation = requestGeneration; const control = byId("dismiss-token"); const recoveredClipboardFailure = !byId("clipboard-error").hidden; if (control.disabled) return; setControlBusy("dismiss-token", true);
  try {
    if (!navigator.clipboard) { byId("clipboard-error").hidden = false; byId("clipboard-error").focus(); return; }
    if (clipboardWritePending) { await Promise.race([clipboardWritePromise, new Promise((resolve) => setTimeout(resolve, 500))]); if (clipboardWritePending) { byId("clipboard-error").hidden = false; byId("clipboard-error").focus(); return; } }
    try { await navigator.clipboard.writeText(""); if ((await navigator.clipboard.readText()).length !== 0) throw new DOMException("clipboard is not empty"); }
    catch (error) { if (!(error instanceof DOMException)) throw error; byId("clipboard-error").hidden = false; byId("clipboard-error").focus(); return; }
    oneTimeToken = null; copiedCredential = false; byId("one-time-token").textContent = ""; byId("clipboard-error").hidden = true;
    closeDialog("credential-dialog", "issue-downstream"); await refreshDashboard("issue-downstream");
    if (recoveredClipboardFailure && requestIsCurrent(generation)) { byId("clipboard-recovery").hidden = false; announce("Clipboard custody recovered and the one-time credential was removed."); }
  } finally { if (requestIsCurrent(generation) && control.isConnected) setControlBusy("dismiss-token", false); }
}
export function credentialState() { return {admin_bearer_present: Boolean(adminBearer), one_time_token_present: Boolean(oneTimeToken), copied_credential: copiedCredential}; }
function openUpstreamDialog(event) { byId("upstream-form").reset(); byId("upstream-error").hidden = true; openDialog("upstream-dialog", event.currentTarget, "upstream-key"); }
function openDownstreamDialog(event) { byId("downstream-form").reset(); byId("downstream-error").hidden = true; byId("clipboard-recovery").hidden = true; openDialog("downstream-dialog", event.currentTarget, "downstream-label"); }
function logout() {
  resetRequestController(); clipboardWritePending = false; clipboardWritePromise = null; oneTimeToken = null; copiedCredential = false; byId("one-time-token").textContent = "";
  for (const dialog of document.querySelectorAll("dialog")) { dialog.querySelector("form")?.reset(); for (const control of dialog.querySelectorAll('[aria-busy="true"]')) { control.disabled = false; control.setAttribute("aria-busy", "false"); } if (dialog.open) dialog.close(); }
  mountLogin(false);
}
byId("add-upstream").addEventListener("click", openUpstreamDialog); byId("issue-downstream").addEventListener("click", openDownstreamDialog);
byId("upstream-form").addEventListener("submit", submitUpstream); byId("downstream-form").addEventListener("submit", submitDownstream);
byId("confirm-action").addEventListener("click", confirmPendingAction); byId("copy-token").addEventListener("click", copyToken); byId("dismiss-token").addEventListener("click", dismissToken);
byId("refresh-dashboard").addEventListener("click", () => refreshDashboard("refresh-dashboard")); byId("retry-dashboard").addEventListener("click", () => refreshDashboard("retry-dashboard")); byId("logout").addEventListener("click", logout);
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => closeDialog(button.dataset.close));
for (const dialog of document.querySelectorAll("dialog:not(#credential-dialog)")) dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDialog(dialog.id); });
byId("credential-dialog").addEventListener("cancel", (event) => { event.preventDefault(); void dismissToken(); });
mountLogin(false);
