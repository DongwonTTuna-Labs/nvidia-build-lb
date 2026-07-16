export function credentialState() {
  return {
    admin_bearer_present: Boolean(adminBearer),
    one_time_token_present: Boolean(oneTimeToken),
    copied_credential: copiedCredential,
  };
}

const desktopNavigation = typeof globalThis.matchMedia === "function"
  ? globalThis.matchMedia("(min-width: 1280px)")
  : {matches: false, addEventListener() {}};
let adminNavigationOwnsFocus = false;
let adminNavigationOwnsNextHashChange = false;
function syncNavigationDisclosure(event = desktopNavigation) {
  const disclosure = byId("navigation-disclosure");
  if (disclosure.hidden) return;
  const focusInside = disclosure.contains(document.activeElement) || adminNavigationOwnsFocus;
  disclosure.open = event.matches;
  if (focusInside) {
    const target = event.matches
      ? disclosure.querySelector("nav a[aria-current]")
      : disclosure.querySelector(":scope > summary");
    target?.focus();
  }
}

function trackAdminNavigationFocus(event) {
  const target = event.target;
  const disclosure = byId("navigation-disclosure");
  if (disclosure.contains(target)) adminNavigationOwnsFocus = true;
  else if (target !== document.body && target !== document.documentElement) adminNavigationOwnsFocus = false;
}

const adminHeadingIds = {
  overview: "overview-heading",
  "upstream-keys": "upstream-heading",
  "downstream-tokens": "downstream-heading",
  events: "events-heading",
};

function selectAdminSection(section, moveFocus) {
  const normalized = Object.hasOwn(adminHeadingIds, section) ? section : "overview";
  const links = document.querySelectorAll("#navigation-disclosure nav a");
  for (const candidate of links) candidate.removeAttribute("aria-current");
  document.querySelector(`#navigation-disclosure nav a[href='#${normalized}']`)?.setAttribute("aria-current", "location");
  if (moveFocus) window.requestAnimationFrame(() => byId(adminHeadingIds[normalized])?.focus());
}

function resetAdminNavigation() {
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#overview`);
  selectAdminSection("overview", false);
}

function syncAdminHash() {
  if (adminNavigationOwnsNextHashChange) {
    adminNavigationOwnsNextHashChange = false;
    return;
  }
  const section = window.location.hash.slice(1);
  if (Object.hasOwn(adminHeadingIds, section)) {
    selectAdminSection(section, !byId("dashboard").hidden);
  }
}

function openUpstreamDialog(event) {
  byId("upstream-form").reset();
  clearUpstreamError();
  const source = upstreamItems.find((item) => item.id === pendingReplacementSourceId);
  setText("upstream-title", source ? `Replace ${keyHandle(source)}` : "Add upstream key");
  setText("upstream-description", source ? `Add a new credential to replace ${keyHandle(source)}. It is encrypted and created disabled.` : "The key is encrypted and created disabled. It will not be shown again.");
  setText("submit-upstream", source ? "Add replacement" : "Add disabled key");
  openDialog("upstream-dialog", event.currentTarget, "upstream-key");
}
function openDownstreamDialog(event) {
  byId("downstream-form").reset();
  clearDownstreamError();
  openDialog("downstream-dialog", event.currentTarget, "downstream-label");
}
async function runRecommendedAction() {
  if (!recommendedPlan) return;
  const {kind, targetId, targetAction} = recommendedPlan;
  if (kind === "refresh") {
    await refreshDashboard("recommended-action");
    return;
  }
  if (kind === "recovery") {
    const recovery = byId("ledger-recovery");
    recovery.hidden = false;
    recovery.open = true;
    byId("ledger-recovery-summary").focus();
    return;
  }
  if (kind === "review") {
    const target = targetAction === "revoke"
      ? byId(`token-${targetId}-revoke`)
      : byId(`key-${targetId}-${targetAction === "delete" ? "delete" : "toggle"}`);
    if (target?.getClientRects().length && !target.disabled) target.focus();
    return;
  }
  const targets = {
    add: "add-upstream",
    probe: `key-${targetId}-probe`,
    enable: `key-${targetId}-toggle`,
    issue: "issue-downstream",
  };
  if (kind === "add") pendingReplacementSourceId = targetId;
  byId(targets[kind])?.click();
}
function logout() { mountLogin(false, false, activeMutationContext ?? unresolvedRecoveryContext(), orphanRecoveryMessage()); }

function navigateToSection(link) {
  const section = link.getAttribute("href").slice(1);
  adminNavigationOwnsNextHashChange = window.location.hash !== `#${section}`;
  selectAdminSection(section, true);
  if (!desktopNavigation.matches) byId("navigation-disclosure").open = false;
}

function initializeAdmin() {
  document.addEventListener("focusin", trackAdminNavigationFocus, {capture: true});
  for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("keydown", trapDialogTab);
  byId("add-upstream").addEventListener("click", openUpstreamDialog);
  byId("issue-downstream").addEventListener("click", openDownstreamDialog);
  byId("recommended-action").addEventListener("click", () => void runRecommendedAction());
  byId("upstream-form").addEventListener("submit", submitUpstream);
  byId("downstream-form").addEventListener("submit", submitDownstream);
  byId("upstream-key").addEventListener("input", () => clearUpstreamError(false));
  byId("downstream-label").addEventListener("input", revalidateDownstreamError);
  for (const scope of document.querySelectorAll("input[name='scope']")) scope.addEventListener("change", revalidateDownstreamError);
  byId("confirm-action").addEventListener("click", confirmPendingAction);
  byId("copy-token").addEventListener("click", copyToken);
  byId("one-time-token").addEventListener("keydown", selectCredential);
  byId("one-time-token").addEventListener("copy", noteManualCredentialCopy);
  document.addEventListener("copy", blockCopyDuringCredentialCleanup, true);
  byId("dismiss-token").addEventListener("click", dismissToken);
  byId("refresh-dashboard").addEventListener("click", () => refreshDashboard("refresh-dashboard"));
  byId("retry-dashboard").addEventListener("click", () => refreshDashboard("retry-dashboard"));
  byId("logout").addEventListener("click", logout);
  for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => closeDialog(button.dataset.close));
  for (const dialog of document.querySelectorAll("dialog:not(#credential-dialog)")) dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeDialog(dialog.id);
  });
  byId("credential-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    void dismissToken();
  });
  for (const link of document.querySelectorAll(".navigation-disclosure nav a")) link.addEventListener("click", () => navigateToSection(link));
  desktopNavigation.addEventListener("change", syncNavigationDisclosure);
  window.addEventListener("hashchange", syncAdminHash);
  window.addEventListener("beforeunload", (event) => {
    if (!activeMutationContext && !unresolvedRecoveryContext() && !orphanedToken && !oneTimeToken && !copiedCredential && !clipboardWritePending) return;
    event.preventDefault();
    event.returnValue = "";
  });
  refreshMutationLocks();
  mountLogin(false);
}

initializeAdmin();
