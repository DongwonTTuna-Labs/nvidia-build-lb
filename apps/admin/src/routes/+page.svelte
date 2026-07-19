<script lang="ts">
import { onMount, tick } from "svelte";
import { routeDescription, routeTitle, supportedScopes } from "$lib/admin-config";
import {
  actionLabel,
  attentionTarget,
  formatDateTime,
  outcomeLabel,
  responseMessage,
  validClient,
  validKey,
} from "$lib/admin-format";
import type {
  AdminEvent,
  Attention,
  Check,
  Client,
  Evidence,
  Key,
  MutationState,
  ProfileCapability,
  PublicHealth,
  Recommendation,
  SlotProjection,
  SnapshotState,
} from "$lib/admin-types";
import ClientsPanel from "$lib/components/ClientsPanel.svelte";
import EvidencePanel from "$lib/components/EvidencePanel.svelte";
import ModelsPanel from "$lib/components/ModelsPanel.svelte";
import OverviewPanel from "$lib/components/OverviewPanel.svelte";
import RouteNavigation from "$lib/components/RouteNavigation.svelte";
import RoutingStatusPanel from "$lib/components/RoutingStatusPanel.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import { type AdminRouteId, adminRoutes } from "$lib/copy";

// Secrets are deliberately kept outside Svelte's reactive state.
const session = { token: "" };
const custody = { token: "", credentialId: "", authToken: "" };
let authenticated = false;
let active: AdminRouteId = "overview";
let state: SnapshotState = "empty";
let structuralReady = false;
let trafficReady = false;
let eligibleKeys = 0;
let models: string[] = [];
let keys: Key[] = [];
let probeState: Record<string, "idle" | "pending" | "valid" | "invalid"> = {};
let clients: Client[] = [];
let loading = false;
let mutating = false;
let mutationState: MutationState = "idle";
let error = "";
let clientError = "";
let notice = "";
let updatedAt = "";
let upstreamLabel = "";
let upstreamError = "";
let clientLabel = "";
let clientScopes: string[] = ["models:read", "chat:write"];
let formRevision = 0;
let announcement = "";
let adminInput: HTMLInputElement;
let upstreamLabelInput: HTMLInputElement;
let upstreamCredentialInput: HTMLInputElement;
let clientForm: HTMLFormElement;
let clientLabelInput: HTMLInputElement;
let upstreamForm: HTMLFormElement;
let routeHeading: HTMLHeadingElement;
let secretDialog: HTMLDialogElement;
let secretField: HTMLTextAreaElement;
let secretTitle: HTMLHeadingElement;
let confirmDialog: HTMLDialogElement;
let confirmTitle: HTMLHeadingElement;
let secretOpen = false;
let secretCleanup: "idle" | "pending" | "manual" = "idle";
let manualCleanupConfirmed = false;
let secretReturnFocus: HTMLElement | null = null;
let confirmReturnFocus: HTMLElement | null = null;
let pendingAction: { kind: "revoke" | "delete"; id: string; label: string } | null = null;
let confirmPending = false;
let evidence: Evidence = {
  source_of_truth: "확인 전",
  persisted_upstream_keys: 0,
  persisted_downstream_credentials: 0,
  persisted_routing_profiles: 0,
  persisted_request_attempts: 0,
};
let refreshController: AbortController | null = null;
let mutationController: AbortController | null = null;
let authEpoch = 0;
let refreshEpoch = 0;
let clipboardEpoch = 0;
let mutationEpoch = 0;
let routeHistoryIndex = 0;
let historyCompensationPending = false;
let pendingHistoryTarget: number | null = null;
let pollingHandle: number | null = null;
type ClipboardWriteOwner = { canceled: boolean; shouldClear: boolean };
let clipboardWriteOwner: ClipboardWriteOwner | null = null;
let lifecycleKeyId = "";
let recommendation: Recommendation = {
  action: "add_upstream_key",
  label: "라우팅에서 키 구성",
  route: "routing",
  reason: "현재 서버 추천을 확인하지 못했습니다.",
};
let attentions: Attention[] = [];
let checks: Check[] = [];
let publicHealth: PublicHealth = {
  hostname: "nvidia-lb.dongwontuna.net",
  status: "not_verified",
  next_action: "verify_public_route",
};
let lastOperation = "확인 전";
let profileCapabilities: ProfileCapability[] = [];
let slotProjections: SlotProjection[] = [];
let adminEvents: AdminEvent[] = [];
let readinessReasons: string[] = [];
let selectedEvent: AdminEvent | null = null;
let eventDialog: HTMLDialogElement;
let eventReturnFocus: HTMLElement | null = null;
let eventTitle: HTMLHeadingElement;

function routeFromHash() {
  const raw = location.hash.slice(1);
  active = normalizedRoute(raw);
  if (typeof history.state?.nvidiaRouteIndex === "number") {
    routeHistoryIndex = history.state.nvidiaRouteIndex;
  }
  if (raw !== active || typeof history.state?.nvidiaRouteIndex !== "number") {
    history.replaceState(
      { nvidiaRouteIndex: routeHistoryIndex, nvidiaScroll: 0 },
      "",
      `#${active}`,
    );
  }
}

function normalizedRoute(value: string): AdminRouteId {
  return adminRoutes.find((route) => route.id === value)?.id ?? "overview";
}

async function selectRoute(id: AdminRouteId, replace = false) {
  if (secretOpen) {
    notice = "일회성 접속 키를 먼저 지우고 닫아야 화면을 이동할 수 있습니다.";
    return;
  }
  if (upstreamCredentialInput) upstreamCredentialInput.value = "";
  active = id;
  const targetScroll =
    replace && typeof history.state?.nvidiaScroll === "number" ? history.state.nvidiaScroll : 0;
  const method = replace ? "replaceState" : "pushState";
  if (!replace) routeHistoryIndex += 1;
  history[method](
    { nvidiaRouteIndex: routeHistoryIndex, nvidiaScroll: targetScroll },
    "",
    `#${id}`,
  );
  await tick();
  routeHeading?.focus({ preventScroll: true });
  announcement = `${routeTitle[id]} 화면`;
  window.scrollTo({ top: targetScroll, behavior: "auto" });
}

function clearSession() {
  authEpoch += 1;
  refreshEpoch += 1;
  refreshController?.abort();
  mutationController?.abort();
  mutationEpoch += 1;
  mutating = false;
  mutationState = "idle";
  loading = false;
  session.token = "";
  authenticated = false;
  if (adminInput) adminInput.value = "";
  structuralReady = false;
  trafficReady = false;
  eligibleKeys = 0;
  keys = [];
  probeState = {};
  clients = [];
  models = [];
  lifecycleKeyId = "";
  recommendation = {
    action: "add_upstream_key",
    label: "라우팅에서 키 구성",
    route: "routing",
    reason: "현재 서버 추천을 확인하지 못했습니다.",
  };
  attentions = [];
  checks = [];
  publicHealth = {
    hostname: "nvidia-lb.dongwontuna.net",
    status: "not_verified",
    next_action: "verify_public_route",
  };
  lastOperation = "확인 전";
  profileCapabilities = [];
  slotProjections = [];
  adminEvents = [];
  readinessReasons = [];
  updatedAt = "";
  evidence = {
    source_of_truth: "확인 전",
    persisted_upstream_keys: 0,
    persisted_downstream_credentials: 0,
    persisted_routing_profiles: 0,
    persisted_request_attempts: 0,
  };
}

function logout() {
  if (secretOpen) {
    notice = "일회성 접속 키를 먼저 지우고 닫아야 로그아웃할 수 있습니다.";
    return;
  }
  clearSession();
  state = "empty";
  error = "";
  clientError = "";
  notice = "로그아웃했습니다.";
  announcement = "관리자 인증이 필요합니다.";
}

function requestAvailable() {
  return !loading && trafficReady && eligibleKeys > 0 && ["ready", "success"].includes(state);
}

function mutationAllowed() {
  return !loading && !["offline", "stale", "partial", "error", "recovery"].includes(state);
}

function showInlineUpstreamError() {
  return Boolean(upstreamError && upstreamError !== error);
}

async function handleAttention(attention: Attention) {
  const keyId = attention.resource?.id ?? attention.id;
  if (attention.next_action === "probe" && keyId) {
    const key = keys.find((candidate) => candidate.id === keyId);
    if (key) {
      await probeKey(key);
      return;
    }
  }
  await selectRoute(attentionTarget(attention));
}

async function openEvent(event: AdminEvent) {
  eventReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  selectedEvent = event;
  if (session.token && event.id) {
    try {
      const response = await request(`/admin/api/v1/events/${event.id}`);
      const payload = (await response.json().catch(() => ({}))) as { operation?: AdminEvent };
      if (response.ok && payload.operation) selectedEvent = { ...event, ...payload.operation };
    } catch {
      // The list row is still useful when the optional detail request fails.
    }
  }
  eventDialog?.showModal();
  await tick();
  eventTitle?.focus({ preventScroll: true });
}

async function request(
  path: string,
  init: RequestInit = {},
  authToken = session.token,
  expectedAuthEpoch = authEpoch,
) {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${authToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers, cache: "no-store" });
  if (response.status === 401 && expectedAuthEpoch === authEpoch && authToken === session.token) {
    const custodyOpen = Boolean(custody.token && custody.credentialId);
    clearSession();
    error = custodyOpen
      ? "인증이 만료되었지만 일회성 접속 키를 폐기하기 전까지 보안 잠금 상태로 유지합니다. 다시 인증해 정리하세요."
      : "관리자 토큰이 만료되었거나 올바르지 않습니다. 다시 인증하세요.";
    state = custodyOpen ? "recovery" : "error";
    secretCleanup = custodyOpen ? "manual" : secretCleanup;
    announcement = custodyOpen
      ? "접속 키 폐기 확인이 필요합니다."
      : "인증이 만료되어 로그인 화면으로 돌아왔습니다.";
    void tick().then(() => adminInput?.focus({ preventScroll: true }));
  }
  return response;
}

async function refresh() {
  refreshController?.abort();
  const controller = new AbortController();
  refreshController = controller;
  const epoch = ++refreshEpoch;
  const currentAuthEpoch = authEpoch;
  loading = true;
  state = "loading";
  error = "";
  upstreamError = "";
  clientError = "";
  notice = "";
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const healthResponse = await fetch("/health", { cache: "no-store", signal: controller.signal });
    const health = (await healthResponse.json().catch(() => ({}))) as {
      ready?: boolean;
      traffic_ready?: boolean;
      eligible_keys?: number;
    };
    const healthReady = health.ready === true;
    const healthTrafficReady = health.traffic_ready === true;
    const healthEligibleKeys = Number.isInteger(health.eligible_keys)
      ? Number(health.eligible_keys)
      : 0;
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
    const response = await request(
      "/admin/api/v1/overview",
      { signal: controller.signal },
      session.token,
      currentAuthEpoch,
    );
    if (response.status === 401)
      throw new Error("관리자 토큰이 없거나 올바르지 않습니다. 다시 인증하세요.");
    if (!response.ok) throw new Error("게이트웨이 상태를 읽지 못했습니다.");
    const snapshot = (await response.json()) as {
      runtime?: { ready?: boolean; traffic_ready?: boolean; eligible_keys?: number };
      upstream_keys?: { items?: Key[] };
      downstream_credentials?: { items?: Client[] };
      models?: string[];
      evidence?: Evidence;
      server_recommendation?: Recommendation;
      attentions?: Attention[];
      checks?: Check[];
      public_health?: PublicHealth;
      snapshot_observed_at?: string;
    };
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
    structuralReady = healthReady;
    trafficReady = healthTrafficReady;
    eligibleKeys = healthEligibleKeys;
    keys = Array.isArray(snapshot.upstream_keys?.items)
      ? snapshot.upstream_keys.items.filter(validKey)
      : [];
    probeState = Object.fromEntries(keys.map((key) => [key.id, probeState[key.id] ?? "idle"]));
    clients = Array.isArray(snapshot.downstream_credentials?.items)
      ? snapshot.downstream_credentials.items.filter(validClient)
      : [];
    models = Array.isArray(snapshot.models)
      ? snapshot.models.filter((model): model is string => typeof model === "string")
      : [];
    evidence = snapshot.evidence ?? evidence;
    if (snapshot.server_recommendation?.route) recommendation = snapshot.server_recommendation;
    attentions = Array.isArray(snapshot.attentions) ? snapshot.attentions : [];
    checks = Array.isArray(snapshot.checks) ? snapshot.checks : [];
    if (snapshot.public_health) publicHealth = snapshot.public_health;
    lastOperation = snapshot.snapshot_observed_at
      ? formatDateTime(snapshot.snapshot_observed_at)
      : updatedAt;
    structuralReady = healthReady && snapshot.runtime?.ready === true;
    trafficReady = healthTrafficReady && snapshot.runtime?.traffic_ready === true;
    eligibleKeys = Math.min(
      healthEligibleKeys,
      Number.isInteger(snapshot.runtime?.eligible_keys)
        ? Number(snapshot.runtime?.eligible_keys)
        : 0,
    );
    const [capabilitiesResponse, slotsResponse, readinessResponse, eventsResponse] =
      await Promise.all([
        request(
          "/admin/api/v1/model-capabilities",
          { signal: controller.signal },
          session.token,
          currentAuthEpoch,
        ),
        request(
          "/admin/api/v1/upstream-slots",
          { signal: controller.signal },
          session.token,
          currentAuthEpoch,
        ),
        request(
          "/admin/api/v1/generation-readiness",
          { signal: controller.signal },
          session.token,
          currentAuthEpoch,
        ),
        request(
          "/admin/api/v1/events?limit=8",
          { signal: controller.signal },
          session.token,
          currentAuthEpoch,
        ),
      ]);
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
    let partial = false;
    if (capabilitiesResponse.ok) {
      const payload = (await capabilitiesResponse.json()) as { models?: ProfileCapability[] };
      profileCapabilities = Array.isArray(payload.models) ? payload.models : [];
    } else {
      profileCapabilities = [];
      partial = true;
    }
    if (slotsResponse.ok) {
      const payload = (await slotsResponse.json()) as { slots?: SlotProjection[] };
      slotProjections = Array.isArray(payload.slots) ? payload.slots : [];
    } else {
      slotProjections = [];
      partial = true;
    }
    if (readinessResponse.ok) {
      const payload = (await readinessResponse.json()) as { reasons?: string[] };
      readinessReasons = Array.isArray(payload.reasons) ? payload.reasons : [];
    } else {
      readinessReasons = ["readiness_snapshot_unavailable"];
      partial = true;
    }
    if (eventsResponse.ok) {
      const payload = (await eventsResponse.json()) as { events?: AdminEvent[] };
      adminEvents = Array.isArray(payload.events) ? payload.events : [];
    } else {
      adminEvents = [];
      partial = true;
    }
    mutationState = "idle";
    state = partial ? "partial" : keys.length === 0 ? "empty" : trafficReady ? "ready" : "degraded";
    updatedAt = formatDateTime(new Date().toISOString());
    announcement = partial
      ? "일부 운영 정보를 확인하지 못했습니다. 표시된 정보만 현재 값으로 취급하세요."
      : "상태를 새로 확인했습니다.";
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") {
      if (epoch !== refreshEpoch) return;
      const hadSnapshot =
        keys.length > 0 || models.length > 0 || clients.length > 0 || Boolean(updatedAt);
      error = "상태 확인 시간이 초과되었습니다. 다시 확인하세요.";
      resetReadData();
      state = navigator.onLine === false ? "offline" : hadSnapshot ? "stale" : "error";
      structuralReady = false;
      eligibleKeys = 0;
      profileCapabilities = [];
      slotProjections = [];
      readinessReasons = ["snapshot_stale"];
      publicHealth = {
        ...publicHealth,
        status: "not_verified",
        next_action: "verify_public_route",
      };
      lastOperation = "확인 필요";
      trafficReady = false;
      announcement = "상태 확인이 시간 초과되어 다시 확인이 필요합니다.";
      return;
    }
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
    const hadSnapshot =
      keys.length > 0 || models.length > 0 || clients.length > 0 || Boolean(updatedAt);
    const message = caught instanceof Error ? caught.message : "상태를 읽지 못했습니다.";
    error = message;
    resetReadData();
    state = navigator.onLine === false ? "offline" : hadSnapshot ? "stale" : "error";
    structuralReady = false;
    eligibleKeys = 0;
    profileCapabilities = [];
    slotProjections = [];
    readinessReasons = ["snapshot_stale"];
    publicHealth = { ...publicHealth, status: "not_verified", next_action: "verify_public_route" };
    lastOperation = "확인 필요";
    trafficReady = false;
  } finally {
    clearTimeout(timeout);
    if (epoch === refreshEpoch) loading = false;
  }
}

function resetReadData() {
  structuralReady = false;
  trafficReady = false;
  eligibleKeys = 0;
  keys = [];
  models = [];
  clients = [];
  evidence = {
    source_of_truth: "확인 필요",
    persisted_upstream_keys: 0,
    persisted_downstream_credentials: 0,
    persisted_routing_profiles: 0,
    persisted_request_attempts: 0,
  };
  attentions = [];
  checks = [];
  profileCapabilities = [];
  slotProjections = [];
  adminEvents = [];
  readinessReasons = ["snapshot_stale"];
  publicHealth = { ...publicHealth, status: "not_verified", next_action: "verify_public_route" };
  lastOperation = "확인 필요";
  recommendation = {
    action: "routing",
    label: "라우팅 상태 다시 확인",
    route: "routing",
    reason: "최신 서버 추천을 확인하지 못했습니다. 상태를 다시 확인하세요.",
  };
}

async function login(event: SubmitEvent) {
  event.preventDefault();
  session.token = adminInput?.value.trim() ?? "";
  if (adminInput) adminInput.value = "";
  if (!session.token) {
    error = "관리자 토큰을 입력하세요.";
    state = "error";
    return;
  }
  await refresh();
  if (!error) {
    authenticated = true;
    await tick().then(() => routeHeading?.focus({ preventScroll: true }));
  } else await tick().then(() => adminInput?.focus({ preventScroll: true }));
}

async function addUpstream(event: SubmitEvent) {
  event.preventDefault();
  if (mutating || (keys.length >= 2 && !keys.some((key) => !key.enabled)) || !mutationAllowed())
    return;
  const credential = upstreamCredentialInput?.value ?? "";
  upstreamCredentialInput.value = "";
  const label = upstreamLabel.trim();
  if (!label || !credential) {
    upstreamError = "라벨과 NVIDIA API 키를 모두 입력하세요.";
    error = upstreamError;
    await tick();
    (label ? upstreamCredentialInput : upstreamLabelInput)?.focus({ preventScroll: true });
    return;
  }
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  error = "";
  upstreamError = "";
  let body = JSON.stringify({ label, credential });
  try {
    const response = await request("/admin/api/v1/upstream-keys", {
      method: "POST",
      body,
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = (await response.json().catch(() => ({}))) as Key | { id?: string };
    if (!response.ok) throw new Error(responseMessage(payload, "NVIDIA 키를 저장하지 못했습니다."));
    lifecycleKeyId = typeof payload.id === "string" ? payload.id : "";
    upstreamLabel = "";
    notice =
      "NVIDIA 키를 암호화해 저장했습니다. 다음 단계는 제공자 검증이며, 검증을 통과한 뒤 라우팅에 포함할 수 있습니다.";
    announcement = "새 슬롯이 저장되었습니다. 제공자 검증을 시작하세요.";
    state = "success";
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    upstreamError =
      caught instanceof Error ? caught.message : "NVIDIA 키 저장 결과를 확인하지 못했습니다.";
    error = upstreamError;
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
    await tick();
    upstreamCredentialInput?.focus({ preventScroll: true });
  } finally {
    body = "";
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

async function issueClient(event: SubmitEvent) {
  event.preventDefault();
  if (mutating || clientScopes.length === 0 || eligibleKeys !== 2 || !mutationAllowed()) return;
  const label = clientLabel.trim();
  if (!label) {
    error = "접속 키 라벨을 입력하세요.";
    clientError = error;
    return;
  }
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  error = "";
  clientError = "";
  let body = JSON.stringify({ label, scopes: clientScopes });
  try {
    const response = await request("/admin/api/v1/downstream-credentials", {
      method: "POST",
      body,
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = (await response.json().catch(() => ({}))) as { token?: string; id?: string };
    if (!response.ok || typeof payload.token !== "string")
      throw new Error(responseMessage(payload, "접속 키를 발급하지 못했습니다."));
    custody.token = payload.token;
    custody.credentialId = payload.id ?? "";
    custody.authToken = session.token;
    clientLabel = "";
    notice = "접속 키는 한 번만 표시됩니다. 저장한 뒤 지우고 닫으세요.";
    state = "success";
    secretOpen = true;
    secretCleanup = "idle";
    const activeElement = document.activeElement;
    secretReturnFocus =
      activeElement instanceof HTMLElement &&
      activeElement !== document.body &&
      activeElement !== document.documentElement
        ? activeElement
        : (clientLabelInput ?? clientForm);
    await tick();
    secretField.value = custody.token;
    secretDialog.showModal();
    secretTitle?.focus({ preventScroll: true });
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error = caught instanceof Error ? caught.message : "접속 키 발급 결과를 확인하지 못했습니다.";
    clientError = error;
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
  } finally {
    body = "";
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

function openConfirmation(kind: "revoke" | "delete", id: string, label: string, event: MouseEvent) {
  if (mutating || !mutationAllowed()) return;
  pendingAction = { kind, id, label };
  confirmReturnFocus = event.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  confirmDialog?.showModal();
  void tick().then(() => confirmTitle?.focus({ preventScroll: true }));
}

async function revokeClient(id: string) {
  if (!mutationAllowed()) return;
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  try {
    const response = await request(`/admin/api/v1/downstream-credentials/${id}/revoke`, {
      method: "POST",
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseMessage(payload, "접속 키를 폐기하지 못했습니다."));
    notice = "접속 키를 폐기했습니다.";
    state = "success";
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error = caught instanceof Error ? caught.message : "접속 키 폐기 결과를 확인하지 못했습니다.";
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
  } finally {
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

async function toggleKey(key: Key) {
  if (
    mutating ||
    !mutationAllowed() ||
    (!key.enabled && !key.verified && probeState[key.id] !== "valid")
  )
    return;
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  try {
    const response = await request(`/admin/api/v1/upstream-keys/${key.id}/state`, {
      method: "POST",
      body: JSON.stringify({ enabled: !key.enabled }),
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseMessage(payload, "라우팅 상태를 바꾸지 못했습니다."));
    notice = key.enabled
      ? `${key.label}을(를) 라우팅에서 제외했습니다.`
      : `${key.label}을(를) 라우팅에 포함했습니다.`;
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error =
      caught instanceof Error ? caught.message : "라우팅 상태 변경 결과를 확인하지 못했습니다.";
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
  } finally {
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

async function probeKey(key: Key) {
  if (mutating || !mutationAllowed()) return;
  mutating = true;
  probeState = { ...probeState, [key.id]: "pending" };
  mutationState = "pending";
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  try {
    const response = await request(`/admin/api/v1/upstream-keys/${key.id}/probe`, {
      method: "POST",
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = (await response.json().catch(() => ({}))) as { probe_status?: string };
    if (!response.ok || payload.probe_status !== "valid") {
      probeState = { ...probeState, [key.id]: "invalid" };
      throw new Error(
        payload.probe_status === "rate_limited"
          ? "제공자 요청 한도로 검증하지 못했습니다. 잠시 뒤 다시 시도하세요."
          : payload.probe_status === "unavailable"
            ? "제공자에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도하세요."
            : "키 검증에 실패했습니다. 원문을 다시 확인하거나 교체하세요.",
      );
    }
    probeState = { ...probeState, [key.id]: "valid" };
    notice = `${key.label} 검증을 통과했습니다. 이제 라우팅에 포함할 수 있습니다.`;
    error = "";
    state = "success";
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error = caught instanceof Error ? caught.message : "키 검증 결과를 확인하지 못했습니다.";
    state = "error";
  } finally {
    mutating = false;
    if (mutationController === controller) mutationController = null;
    mutationState = error ? "reconcile" : "idle";
  }
}

async function deleteKey(key: Key) {
  if (!mutationAllowed()) return;
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  try {
    const response = await request(`/admin/api/v1/upstream-keys/${key.id}`, {
      method: "DELETE",
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(responseMessage(payload, "NVIDIA 키를 사용 중지하지 못했습니다."));
    }
    notice = `${key.label}을(를) 영구 폐기했습니다. 감사 이력은 보존되며, 교체하려면 새 키를 등록하세요.`;
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error = caught instanceof Error ? caught.message : "NVIDIA 키 폐기 결과를 확인하지 못했습니다.";
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
  } finally {
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

async function confirmDestructive(event: SubmitEvent) {
  event.preventDefault();
  const action = pendingAction;
  if (!action || confirmPending) return;
  if (!mutationAllowed()) {
    error = "상태가 바뀌어 작업을 적용하지 않았습니다. 먼저 상태를 다시 확인하세요.";
    mutationState = "reconcile";
    return;
  }
  confirmPending = true;
  try {
    // Polling can update the row while the dialog is open. Reconcile once
    // immediately before the mutation and refuse a stale or partial result.
    await refresh();
    if (!mutationAllowed()) {
      error = "최신 상태를 확인하지 못해 작업을 적용하지 않았습니다. 다시 확인하세요.";
      mutationState = "reconcile";
      return;
    }
    if (action.kind === "revoke") await revokeClient(action.id);
    else {
      const key = keys.find((candidate) => candidate.id === action.id);
      if (key) await deleteKey(key);
      else error = "이미 사라진 항목입니다. 최신 상태를 다시 확인하세요.";
    }
  } finally {
    confirmPending = false;
    pendingAction = null;
    if (confirmDialog?.open) confirmDialog.close();
  }
}

async function copyIssuedToken() {
  if (!custody.token) return;
  const token = custody.token;
  const operation = ++clipboardEpoch;
  if (clipboardWriteOwner) {
    clipboardWriteOwner.canceled = true;
    clipboardWriteOwner.shouldClear = false;
  }
  const owner: ClipboardWriteOwner = { canceled: false, shouldClear: false };
  clipboardWriteOwner = owner;
  let write: Promise<void> | null = null;
  try {
    write = navigator.clipboard.writeText(token);
    void write
      .then(() => {
        if (owner.canceled && owner.shouldClear)
          void navigator.clipboard.writeText("").catch(() => undefined);
      })
      .catch(() => undefined);
    await clipboardWithTimeout(write);
    const copied = await clipboardWithTimeout(navigator.clipboard.readText());
    if (operation !== clipboardEpoch) return;
    if (copied !== token) throw new Error("clipboard verification failed");
    notice = "접속 키를 클립보드에 복사했습니다. 저장한 뒤 ‘지우고 닫기’를 누르세요.";
  } catch {
    if (operation !== clipboardEpoch) return;
    ++clipboardEpoch;
    owner.canceled = true;
    owner.shouldClear = true;
    if (clipboardWriteOwner === owner) clipboardWriteOwner = null;
    error = "클립보드에 복사하지 못했습니다. 표시된 키를 안전한 곳에 직접 저장하세요.";
  }
}

function clipboardWithTimeout<T>(operation: Promise<T>, timeoutMs = 1500): Promise<T> {
  return Promise.race([
    operation,
    new Promise<T>((_, reject) =>
      setTimeout(() => reject(new Error("clipboard timeout")), timeoutMs),
    ),
  ]);
}

async function clearIssuedToken(
  manual = false,
  revokeOnFailure = false,
  authToken = session.token,
) {
  const issuedCredentialId = custody.credentialId;
  if (!custody.token && !secretOpen) return;
  if (manual && !manualCleanupConfirmed) {
    error = "클립보드를 직접 비운 뒤 확인란을 선택하세요.";
    return;
  }
  if (clipboardWriteOwner) {
    clipboardWriteOwner.canceled = true;
    clipboardWriteOwner.shouldClear = true;
    clipboardWriteOwner = null;
  }
  const epoch = ++clipboardEpoch;
  secretCleanup = "pending";
  let clipboardCleared = false;
  let revokedDueToClipboard = false;
  if (!manual && navigator.clipboard) {
    try {
      await clipboardWithTimeout(navigator.clipboard.writeText(""));
      clipboardCleared = (await clipboardWithTimeout(navigator.clipboard.readText())) === "";
    } catch {
      clipboardCleared = false;
    }
  }
  if (epoch !== clipboardEpoch) return;
  if (manual && navigator.clipboard) {
    try {
      clipboardCleared = (await clipboardWithTimeout(navigator.clipboard.readText())) === "";
    } catch {
      clipboardCleared = false;
    }
  }
  if (!clipboardCleared && (manual || revokeOnFailure) && issuedCredentialId) {
    mutating = true;
    const operation = ++mutationEpoch;
    mutationController?.abort();
    const controller = new AbortController();
    mutationController = controller;
    mutationState = "pending";
    try {
      const response = await request(
        `/admin/api/v1/downstream-credentials/${issuedCredentialId}/revoke`,
        {
          method: "POST",
          signal: controller.signal,
          keepalive: revokeOnFailure,
        },
        authToken,
      );
      if (operation !== mutationEpoch) return;
      if (!response.ok) throw new Error("발급된 접속 키를 폐기하지 못했습니다.");
      clipboardCleared = true;
      revokedDueToClipboard = true;
      notice = "클립보드 정리를 확인하지 못해 접속 키를 폐기했습니다.";
    } catch (caught) {
      if (controller.signal.aborted || operation !== mutationEpoch) return;
      error = caught instanceof Error ? caught.message : "접속 키 폐기 결과를 확인하지 못했습니다.";
      mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
      secretCleanup = "manual";
      return;
    } finally {
      mutating = false;
      if (mutationController === controller) mutationController = null;
      if (mutationState === "pending") mutationState = "idle";
    }
  }
  if (!clipboardCleared && (manual || revokeOnFailure)) {
    secretCleanup = "manual";
    error = "클립보드 정리를 확인하지 못했고 접속 키도 폐기하지 못했습니다. 다시 확인하세요.";
    return;
  }
  if (!clipboardCleared && !manual) {
    secretCleanup = "manual";
    error = "클립보드를 자동으로 비우지 못했습니다. 직접 비운 뒤 수동 확인을 누르세요.";
    return;
  }
  if (secretField) secretField.value = "";
  custody.token = "";
  custody.credentialId = "";
  custody.authToken = "";
  secretOpen = false;
  error = "";
  manualCleanupConfirmed = false;
  secretCleanup = "idle";
  if (secretDialog?.open) secretDialog.close();
  notice = revokedDueToClipboard
    ? "클립보드 정리를 확인하지 못해 접속 키를 폐기했습니다."
    : "화면에서 접속 키를 지웠습니다.";
  const focusTarget = secretReturnFocus ?? clientLabelInput ?? clientForm;
  await tick();
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  focusTarget?.focus({ preventScroll: true });
  if (focusTarget && document.activeElement !== focusTarget) {
    setTimeout(() => focusTarget.focus({ preventScroll: true }), 0);
  }
  secretReturnFocus = null;
}

function stateCopy() {
  if (requestAvailable()) return "요청 가능";
  return {
    loading: "확인 중",
    empty: "구성 필요",
    ready: "요청 가능",
    degraded: "현재 요청 불가",
    stale: "확인 지연",
    offline: "오프라인",
    error: "확인 실패",
    success: "최근 작업 완료",
    partial: "일부만 확인",
    conflict: "상태 충돌",
    recovery: "복구 필요",
  }[state];
}

function recommendedAction() {
  if (["offline", "stale", "error", "recovery"].includes(state)) return "새로고침 후 상태 확인";
  if (keys.length < 2) return "라우팅에서 두 키 구성";
  if (!trafficReady) return "라우팅 상태 확인";
  return "지금 필요한 작업이 없습니다.";
}

function profileReadiness() {
  if (!models.length) return "모델 목록 미확인";
  return requestAvailable() ? "공통 슬롯 선택 가능" : "선택 가능한 슬롯 없음";
}

onMount(() => {
  history.scrollRestoration = "manual";
  routeFromHash();
  const onHash = () => {
    const targetIndex = history.state?.nvidiaRouteIndex;
    if (secretOpen) {
      if (typeof targetIndex === "number" && targetIndex !== routeHistoryIndex) {
        pendingHistoryTarget = targetIndex;
        if (!historyCompensationPending) {
          historyCompensationPending = true;
          history.go(routeHistoryIndex - targetIndex);
        } else {
          // A second Back/Forward can arrive while the first compensation is
          // still travelling. Continue until the browser is back at the
          // custody-protected entry instead of silently accepting the latest
          // route.
          history.go(routeHistoryIndex - targetIndex);
        }
      } else if (historyCompensationPending) {
        historyCompensationPending = false;
        const compensatedTarget = pendingHistoryTarget;
        pendingHistoryTarget = null;
        history.replaceState(
          { nvidiaRouteIndex: routeHistoryIndex, nvidiaScroll: window.scrollY },
          "",
          `#${active}`,
        );
        if (compensatedTarget !== null) {
          announcement = "일회성 접속 키가 열려 있어 이동을 되돌렸습니다.";
        }
      } else {
        const compensatedTarget = pendingHistoryTarget;
        historyCompensationPending = false;
        pendingHistoryTarget = null;
        history.replaceState(
          { nvidiaRouteIndex: routeHistoryIndex, nvidiaScroll: window.scrollY },
          "",
          `#${active}`,
        );
        if (compensatedTarget !== null) {
          announcement = "일회성 접속 키가 열려 있어 이동을 되돌렸습니다.";
        }
      }
      notice = "일회성 접속 키를 먼저 지우고 닫아야 화면을 이동할 수 있습니다.";
      return;
    }
    if (typeof targetIndex === "number") routeHistoryIndex = targetIndex;
    historyCompensationPending = false;
    pendingHistoryTarget = null;
    void selectRoute(normalizedRoute(location.hash.slice(1)), true);
  };
  const onScroll = () => {
    history.replaceState(
      { ...(history.state ?? {}), nvidiaScroll: window.scrollY },
      "",
      location.href,
    );
  };
  const onPageHide = () => {
    const authSnapshot = session.token || custody.authToken;
    const credentialSnapshot = custody.credentialId;
    if (credentialSnapshot) {
      // pagehide is not a reliable place to await network I/O. Keep a
      // recovery-visible custody marker synchronously so a bfcache restore
      // cannot present an apparently clean session when revoke was unproved.
      state = "recovery";
      error = "페이지를 떠나는 동안 접속 키 폐기를 확인 중입니다. 복귀 후 상태를 다시 확인하세요.";
      announcement = "접속 키 폐기 확인이 필요합니다.";
    }
    refreshController?.abort();
    mutationController?.abort();
    mutationEpoch += 1;
    refreshEpoch += 1;
    authEpoch += 1;
    mutating = false;
    mutationState = "idle";
    clipboardEpoch += 1;
    const cleanup =
      authSnapshot && credentialSnapshot
        ? clearIssuedToken(false, true, authSnapshot)
        : Promise.resolve();
    void cleanup.finally(() => {
      clearSession();
      if (secretField) secretField.value = "";
      if (upstreamCredentialInput) upstreamCredentialInput.value = "";
      if (secretCleanup === "idle" || !custody.credentialId) {
        custody.token = "";
        custody.credentialId = "";
        custody.authToken = "";
        secretOpen = false;
      } else {
        state = "recovery";
        error =
          "페이지를 떠나는 동안 접속 키 폐기를 확인하지 못했습니다. 다시 인증해 상태를 확인하세요.";
        announcement = "접속 키 폐기 확인이 필요합니다.";
      }
    });
    manualCleanupConfirmed = false;
    pendingAction = null;
    historyCompensationPending = false;
    pendingHistoryTarget = null;
    secretDialog?.close();
    confirmDialog?.close();
  };
  const onPageShow = () => {
    clearSession();
    if (custody.credentialId) {
      state = "recovery";
      error =
        "페이지 이동 중 접속 키 폐기를 확인하지 못했습니다. 다시 인증한 뒤 수동으로 폐기하세요.";
      announcement = "접속 키 폐기 확인이 필요합니다.";
      return;
    }
    state = "empty";
    announcement = "페이지를 다시 확인하려면 관리자 인증이 필요합니다.";
  };
  const onVisibility = () => {
    if (
      document.visibilityState === "visible" &&
      session.token &&
      !loading &&
      !mutating &&
      !confirmPending
    ) {
      void refresh();
    }
  };
  const onOnline = () => {
    if (session.token && !confirmPending) void refresh();
  };
  const onOffline = () => {
    structuralReady = false;
    eligibleKeys = 0;
    profileCapabilities = [];
    slotProjections = [];
    readinessReasons = ["offline"];
    publicHealth = { ...publicHealth, status: "not_verified", next_action: "verify_public_route" };
    lastOperation = "확인 필요";
    recommendation = {
      action: "routing",
      label: "라우팅 상태 다시 확인",
      route: "routing",
      reason: "연결이 복구되기 전에는 이전 서버 추천을 사용할 수 없습니다.",
    };
    trafficReady = false;
    state = "offline";
    announcement = "오프라인 상태입니다. 연결되면 다시 확인하세요.";
  };
  pollingHandle = window.setInterval(() => {
    if (
      document.visibilityState === "visible" &&
      session.token &&
      !loading &&
      !mutating &&
      !confirmPending
    ) {
      void refresh();
    }
  }, 10_000);
  addEventListener("hashchange", onHash);
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("popstate", onHash);
  addEventListener("pagehide", onPageHide);
  addEventListener("pageshow", onPageShow);
  addEventListener("visibilitychange", onVisibility);
  addEventListener("online", onOnline);
  addEventListener("offline", onOffline);
  return () => {
    refreshController?.abort();
    removeEventListener("hashchange", onHash);
    removeEventListener("scroll", onScroll);
    removeEventListener("popstate", onHash);
    removeEventListener("pagehide", onPageHide);
    removeEventListener("pageshow", onPageShow);
    removeEventListener("visibilitychange", onVisibility);
    removeEventListener("online", onOnline);
    removeEventListener("offline", onOffline);
    if (pollingHandle !== null) window.clearInterval(pollingHandle);
    pollingHandle = null;
  };
});
</script>

<svelte:head><title>{authenticated ? `${routeTitle[active]} · NVIDIA Build LB` : "관리 로그인 · NVIDIA Build LB"}</title></svelte:head>

<div class="shell">
  <a class="skip" href={authenticated ? "#route-heading" : "#admin-token"}>본문으로 건너뛰기</a>
  <header class="topbar">
    <div><p class="eyebrow">NVIDIA BUILD LB</p>{#if authenticated}<p class="brand">관리 콘솔</p>{:else}<h1 class="brand">관리자 로그인</h1>{/if}</div>
    {#if authenticated}<div class="top-actions"><StatusBadge good={requestAvailable()} label={stateCopy()} /><button class="secondary" type="button" onclick={() => void refresh()} disabled={!session.token || loading || mutating}>상태 새로고침</button><button class="secondary" type="button" onclick={logout} disabled={!session.token || secretOpen}>로그아웃</button></div>{/if}
  </header>

  {#if !authenticated}
  <form class="auth" aria-label="관리자 인증" onsubmit={login}>
    <label for="admin-token">관리자 토큰<input id="admin-token" bind:this={adminInput} type="password" autocomplete="off" aria-describedby={error ? "error-message" : undefined} aria-invalid={error ? "true" : undefined} placeholder="토큰은 확인 후 화면에서 지워집니다" /></label>
    <button class="secondary" type="submit" disabled={loading}>{loading ? "확인 중…" : "인증하고 확인"}</button>
  </form>
  {#if error}<p id="error-message" class="alert auth-error" role="alert" aria-live="assertive">{error}<button class="link-button" type="button" onclick={() => adminInput?.focus({ preventScroll: true })}>다시 인증</button></p>{/if}
  {/if}

  {#if authenticated}
  <RouteNavigation active={active} onSelect={(route) => void selectRoute(route)} />
  <p id="nav-hint" class="nav-hint">모바일에서는 좌우로 메뉴를 더 볼 수 있습니다.</p>

  <main id="main-content" aria-busy={loading}>
    <div class="route-heading"><p class="eyebrow">{routeTitle[active]}</p><h1 id="route-heading" bind:this={routeHeading} tabindex="-1">{routeTitle[active]}</h1><p class="muted">{routeDescription[active]}</p></div>
    <p class="live" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    {#if mutationState === "pending" && mutating}<p class="attention" role="status" aria-live="polite">변경 사항을 저장하는 중입니다. 완료될 때까지 조작하지 마세요.</p>{/if}
    {#if mutationState === "unknown"}<p class="attention" role="alert">이전 작업의 결과를 확인하지 못했습니다. 현재 화면은 오래된 상태일 수 있어 다시 확인한 뒤 재시도하세요.<button class="link-button" type="button" onclick={() => void refresh()}>상태 다시 확인</button></p>{/if}
    {#if mutationState === "reconcile"}<p class="attention" role="alert">작업은 전송됐지만 최신 상태 확인에 실패했습니다. 재시도하지 말고 먼저 상태를 다시 확인하세요.<button class="link-button" type="button" onclick={() => void refresh()}>상태 다시 확인</button></p>{/if}
    {#if error}<p id="error-message" class="alert" role="alert">{error}<button class="link-button" type="button" onclick={() => void refresh()}>다시 확인</button></p>{/if}
    {#if ["stale", "offline", "partial"].includes(state)}<p class="attention" role="status">이 화면의 일부 정보는 마지막 성공 확인보다 오래됐습니다. 상태를 다시 확인하기 전에는 변경할 수 없습니다.</p>{/if}
    {#if notice}<p class="notice" role="status">{notice}</p>{/if}

    <OverviewPanel
      active={active}
      structuralReady={structuralReady}
      profileReadiness={profileReadiness()}
      requestAvailable={requestAvailable()}
      keysCount={keys.length}
      eligibleKeys={eligibleKeys}
      recommendation={recommendation}
      attentions={attentions}
      checks={checks}
      publicHealth={publicHealth}
      lastOperation={lastOperation}
      loading={loading}
      mutating={mutating}
      onSelectRoute={(route) => void selectRoute(route)}
      onAttention={(attention) => void handleAttention(attention)}
    />
    <RoutingStatusPanel
      active={active}
      keys={keys}
      probeState={probeState}
      lifecycleKeyId={lifecycleKeyId}
      sessionToken={session.token}
      state={state}
      mutating={mutating}
      mutationState={mutationState}
      readinessReasons={readinessReasons}
      profileCapabilities={profileCapabilities}
      slotProjections={slotProjections}
      onProbe={(key) => void probeKey(key)}
      onToggle={(key) => void toggleKey(key)}
      onDelete={(kind, id, label, event) => openConfirmation(kind, id, label, event)}
    >
      {#if keys.length < 2 || keys.some((key) => !key.enabled)}
        <div class="subpanel"><h3>{keys.length === 0 ? "첫 번째 키 추가" : keys.length < 2 ? "두 번째 키 추가" : "중지된 슬롯 교체"}</h3><p>서로 다른 두 키만 저장할 수 있습니다. 저장 후 원문은 즉시 지워집니다.</p><form bind:this={upstreamForm} class="form" onsubmit={addUpstream}><label for="upstream-label">라벨<input id="upstream-label" bind:this={upstreamLabelInput} bind:value={upstreamLabel} maxlength="128" required aria-describedby={showInlineUpstreamError() ? "upstream-error" : error ? "error-message" : undefined} aria-invalid={upstreamError ? "true" : undefined} placeholder="예: nvidia-primary" /></label><label for="upstream-credential">NVIDIA API 키<input id="upstream-credential" bind:this={upstreamCredentialInput} type="password" required autocomplete="off" aria-describedby={showInlineUpstreamError() ? "upstream-error" : error ? "error-message" : undefined} aria-invalid={upstreamError ? "true" : undefined} oninput={() => (formRevision += 1)} placeholder="nvapi-…" /></label>{#if showInlineUpstreamError()}<p id="upstream-error" class="alert" role="alert">{upstreamError}</p>{/if}<span class="sr-only">{formRevision}</span><button class="primary" type="submit" disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed()}>암호화 저장</button></form></div>
      {/if}
    </RoutingStatusPanel>
    <ClientsPanel
      active={active}
      clients={clients}
      sessionToken={session.token}
      state={state}
      mutating={mutating}
      mutationState={mutationState}
      onRevoke={(id, label, event) => openConfirmation("revoke", id, label, event)}
    >
      <form bind:this={clientForm} class="form" onsubmit={issueClient}><label for="client-label">라벨<input id="client-label" bind:this={clientLabelInput} bind:value={clientLabel} maxlength="128" required aria-describedby={clientError ? "error-message" : undefined} aria-invalid={clientError ? "true" : undefined} placeholder="예: hermes" /></label><fieldset><legend>권한 범위</legend>{#each supportedScopes as [scope, label]}<label class="check"><input type="checkbox" value={scope} bind:group={clientScopes} /> <span>{label}</span><small>{scope}</small></label>{/each}</fieldset><button class="primary" type="submit" disabled={!session.token || eligibleKeys !== 2 || !clientLabel.trim() || clientScopes.length === 0 || mutating || mutationState !== "idle" || !mutationAllowed()}>접속 키 발급</button></form>
    </ClientsPanel>
    <ModelsPanel active={active} state={state} profileCapabilities={profileCapabilities} models={models} />
    <EvidencePanel
      active={active}
      sessionToken={session.token}
      updatedAt={updatedAt}
      evidence={evidence}
      adminEvents={adminEvents}
      checks={checks}
      attentions={attentions}
      state={state}
      eligibleKeys={eligibleKeys}
      onAttention={(attention) => void handleAttention(attention)}
      onEvent={(event) => void openEvent(event)}
    />
  </main>
  {/if}
</div>

<dialog bind:this={secretDialog} aria-labelledby="secret-title" aria-describedby="secret-description" oncancel={(event) => { event.preventDefault(); }}>
  <form method="dialog" class="dialog-card" onsubmit={(event) => { event.preventDefault(); clearIssuedToken(); }}>
    <h2 id="secret-title" bind:this={secretTitle} tabindex="-1">지금만 표시되는 접속 키</h2><p id="secret-description">안전한 비밀 저장소에 복사한 뒤 아래 버튼으로 화면에서 지우고 닫으세요. 키는 다시 표시되지 않습니다.</p><textarea bind:this={secretField} readonly aria-label="일회성 접속 키"></textarea>{#if secretCleanup === "manual"}<p class="attention" role="alert">클립보드를 자동으로 비우지 못했습니다. 클립보드를 직접 비운 뒤 확인란을 선택하세요.</p><label class="check" for="manual-cleanup"><input id="manual-cleanup" type="checkbox" bind:checked={manualCleanupConfirmed} /> 클립보드를 직접 비웠습니다</label>{/if}<div class="actions"><button class="secondary" type="button" onclick={() => void copyIssuedToken()}>클립보드에 복사</button>{#if secretCleanup === "manual"}<button class="secondary" type="button" onclick={() => void clearIssuedToken(true)} disabled={!manualCleanupConfirmed}>수동 확인 후 닫기</button>{:else}<button class="primary" type="submit" disabled={secretCleanup === "pending"}>{secretCleanup === "pending" ? "클립보드 정리 중…" : "지우고 닫기"}</button>{/if}</div>
  </form>
</dialog>

<dialog bind:this={confirmDialog} aria-labelledby="confirm-title" aria-describedby="confirm-description" onclose={() => { if (!confirmPending) pendingAction = null; void tick().then(() => confirmReturnFocus?.focus()); }}>
  <form method="dialog" class="dialog-card" onsubmit={confirmDestructive}>
    <h2 id="confirm-title" bind:this={confirmTitle} tabindex="-1">영구 폐기 확인</h2>
    <p id="confirm-description">{pendingAction?.label ?? "이 항목"}을(를) 영구 폐기합니다. 감사 이력은 남지만 이 키는 다시 라우팅에 포함할 수 없습니다. 교체하려면 새 키를 등록해야 합니다. 계속할까요?</p>
    <div class="actions"><button class="secondary" type="button" onclick={() => confirmDialog?.close()} disabled={confirmPending}>{confirmPending ? "처리 중…" : "취소"}</button><button class="danger" type="submit" disabled={confirmPending}>{confirmPending ? "폐기 중…" : "영구 폐기 확인"}</button></div>
  </form>
</dialog>

<dialog bind:this={eventDialog} aria-labelledby="event-title" onclose={() => { selectedEvent = null; void tick().then(() => { eventReturnFocus?.focus({ preventScroll: true }); eventReturnFocus = null; }); }}>
  <form method="dialog" class="dialog-card">
    <h2 id="event-title" bind:this={eventTitle} tabindex="-1">작업 상세</h2>
    {#if selectedEvent}
      <dl class="event-detail">
        <div><dt>결과</dt><dd>{outcomeLabel(selectedEvent.outcome ?? "")}</dd></div>
        <div><dt>프로필</dt><dd>{selectedEvent.profile_id ?? "확인할 수 없음"}</dd></div>
        <div><dt>요청 ID</dt><dd>{selectedEvent.request_id ?? "확인할 수 없음"}</dd></div>
        <div><dt>슬롯 ID</dt><dd>{selectedEvent.key_id ?? "확인할 수 없음"}</dd></div>
        {#if selectedEvent.created_at}<div><dt>시각</dt><dd>{formatDateTime(selectedEvent.created_at)}</dd></div>{/if}
      </dl>
    {/if}
    <button class="secondary" type="submit">닫기</button>
  </form>
</dialog>

<style>
  :global(*) { box-sizing: border-box; }
  :global([hidden]) { display: none !important; }
  :global(html) { background: #0b0d0e; }
  :global(body) { margin: 0; background: #0b0d0e; color: #f5f7f6; font-family: Inter, ui-sans-serif, system-ui, sans-serif; line-height: 1.5; }
  :global(button:focus-visible), :global(a:focus-visible), :global(input:focus-visible), :global(textarea:focus-visible), :global([tabindex]:focus-visible) { outline: 3px solid #76b900; outline-offset: 3px; }
  .shell { min-height: 100vh; max-width: 1120px; margin: 0 auto; padding: max(16px, env(safe-area-inset-top)) 32px calc(32px + env(safe-area-inset-bottom)); }
  .skip { position: absolute; left: 16px; top: 8px; transform: translateY(-180%); background: #1d2124; color: #f5f7f6; padding: 10px 14px; z-index: 3; border-radius: 6px; } .skip:focus { transform: translateY(0); }
  .topbar, .auth, .route-heading { display: flex; gap: 16px; align-items: center; } .topbar { justify-content: space-between; min-height: 56px; } .top-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; } .brand { margin: 0; font-size: 1.1rem; font-weight: 800; } .eyebrow { margin: 0; color: #76b900; font-size: .72rem; font-weight: 800; letter-spacing: .12em; }
  .auth { align-items: end; margin: 16px 0; } .auth label { flex: 1; } label { display: grid; gap: 6px; color: #d0d5d2; font-size: .9rem; font-weight: 700; } input, textarea { width: 100%; min-height: 44px; border: 1px solid #6b746f; border-radius: 6px; padding: 10px 12px; color: #f5f7f6; background: #171a1d; font: inherit; } textarea { min-height: 76px; resize: vertical; }
  main { padding-top: 32px; } .route-heading { align-items: baseline; flex-wrap: wrap; margin-bottom: 24px; } .route-heading .eyebrow { flex-basis: 100%; } h1, h2, h3, p { margin: 0; } h1 { font-size: clamp(1.6rem, 3vw, 2rem); } h2 { font-size: 1.35rem; } h3 { font-size: 1rem; } .muted, small { color: #aab2ae; } .live { min-height: 24px; color: #d0d5d2; } .nav-hint { display: none; }
  .subpanel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  .alert, .notice, .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; } .alert { color: #ff8a8a; border: 1px solid #ff8a8a; } .auth-error { max-width: 760px; } .notice { color: #76b900; border: 1px solid #76b900; } .attention { color: #ffd166; border: 1px solid #ffd166; } .attention > * { min-width: 0; overflow-wrap: anywhere; }
  .link-button { margin-left: auto; color: inherit; background: transparent; border: 1px solid currentColor; border-radius: 6px; padding: 8px 10px; min-height: 44px; cursor: pointer; }
  button { min-height: 44px; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; font: inherit; font-weight: 800; } button:disabled { opacity: .5; cursor: not-allowed; } .primary { background: #76b900; color: #091006; } .secondary { background: #262b2e; color: #f5f7f6; border: 1px solid #6b746f; } .danger { background: transparent; color: #ff8a8a; border: 1px solid #ff8a8a; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .form { display: grid; gap: 12px; max-width: 560px; } fieldset { display: grid; gap: 8px; border: 1px solid #6b746f; border-radius: 6px; padding: 14px; } legend { padding: 0 5px; color: #d0d5d2; font-weight: 800; } .check { display: flex; align-items: center; gap: 8px; min-height: 44px; } .check input { width: 20px; min-height: 20px; } .check small { margin-left: auto; }
  .subpanel { margin-top: 20px; } .event-detail { display: grid; gap: 10px; margin: 0; } .event-detail div { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 12px; } .event-detail dt { color: #aab2ae; } .event-detail dd { margin: 0; overflow-wrap: anywhere; }
  dialog { width: min(560px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); padding: 0; border: 1px solid #6b746f; border-radius: 8px; background: #171a1d; color: #f5f7f6; box-shadow: 0 8px 24px rgb(0 0 0 / .28); } dialog::backdrop { background: rgb(0 0 0 / .7); } .dialog-card { display: grid; gap: 16px; padding: 24px; } .dialog-card textarea { color: #76b900; font-family: ui-monospace, monospace; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  @media (max-width: 767px) { .shell { padding-inline: 16px; } .auth, .route-heading { align-items: stretch; flex-direction: column; } .topbar { align-items: flex-start; flex-direction: column; } .top-actions { width: 100%; justify-content: stretch; } .top-actions button { flex: 1; } .auth button, .form button { width: 100%; } .nav-hint { display: block; margin: 6px 0 0; color: #aab2ae; font-size: .8rem; } .subpanel { padding: 16px; } }
  @media (forced-colors: active) { input, textarea, button, fieldset, dialog { border: 1px solid ButtonText; } .primary, .secondary, .danger { background: Canvas; color: ButtonText; } }
  @media (prefers-reduced-motion: reduce) { :global(*) { scroll-behavior: auto !important; transition: none !important; } }
</style>
