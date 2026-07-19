<script lang="ts">
import { onMount, tick } from "svelte";
import { type AdminRouteId, adminRoutes } from "$lib/copy";

type SnapshotState =
  | "loading"
  | "empty"
  | "ready"
  | "degraded"
  | "stale"
  | "offline"
  | "error"
  | "success"
  | "partial"
  | "conflict"
  | "recovery";
type MutationState = "idle" | "pending" | "unknown" | "reconcile";
type Key = {
  id: string;
  label: string;
  fingerprint: string;
  enabled: boolean;
  verified: boolean;
  cooldown_until: string | null;
  request_count: number;
  failure_count: number;
};
type Client = {
  id: string;
  label: string;
  scopes: string[];
  active: boolean;
  request_count: number;
  revoked_at: string | null;
};
type Evidence = {
  source_of_truth: string;
  persisted_upstream_keys: number;
  persisted_downstream_credentials: number;
  persisted_routing_profiles: number;
  persisted_request_attempts?: number;
};

const supportedScopes = [
  ["models:read", "모델 조회"],
  ["chat:write", "대화"],
  ["embeddings:write", "임베딩"],
  ["images:write", "이미지"],
  ["audio:write", "음성"],
  ["media:write", "영상·미디어"],
] as const;
const routeTitle: Record<AdminRouteId, string> = {
  overview: "개요",
  routing: "라우팅",
  clients: "접속 키",
  models: "모델",
  evidence: "증거",
};
const routeDescription: Record<AdminRouteId, string> = {
  overview: "지금 요청을 받을 수 있는지와 다음 조치만 확인합니다.",
  routing: "두 슬롯의 현재 선택 가능 여부와 장애 전환 상태입니다.",
  clients: "Hermes 등 다운스트림에 필요한 권한만 발급·폐기합니다.",
  models: "지원 모달리티와 실제 요청 경로를 한눈에 확인합니다.",
  evidence: "PostgreSQL 지속성과 마지막 확인 시각을 검증합니다.",
};
const modelCapabilities: Record<string, { modalities: string; route: string }> = {
  "z-ai/glm-5.2": { modalities: "텍스트·대화", route: "/v1/chat/completions" },
  "microsoft/phi-4-multimodal-instruct": {
    modalities: "텍스트·이미지·오디오",
    route: "/v1/chat/completions",
  },
  "nvidia/vila": { modalities: "텍스트·이미지·영상", route: "/v1/chat/completions" },
  "nvidia/nvclip": { modalities: "이미지·임베딩", route: "/v1/embeddings" },
  "black-forest-labs/flux.1-kontext-dev": {
    modalities: "이미지 생성",
    route: "/v1/images/generations",
  },
  "stabilityai/stable-video-diffusion": {
    modalities: "영상 생성",
    route: "/v1/nvidia/inference",
  },
  "nvidia/magpie-tts-multilingual": { modalities: "음성", route: "/v1/audio/speech" },
  "nvidia/parakeet-ctc-1.1b": { modalities: "음성 전사", route: "/v1/audio/transcriptions" },
};

// Secrets are deliberately kept outside Svelte's reactive state.
const session = { token: "" };
const custody = { token: "", credentialId: "" };
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
let notice = "";
let updatedAt = "";
let upstreamLabel = "";
let clientLabel = "";
let clientScopes: string[] = ["models:read", "chat:write"];
let formRevision = 0;
let announcement = "";
let adminInput: HTMLInputElement;
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
  notice = "로그아웃했습니다.";
  announcement = "관리자 인증이 필요합니다.";
}

function responseMessage(body: unknown, fallback: string) {
  if (!body || typeof body !== "object") return fallback;
  const errorBody = (body as { error?: unknown }).error;
  const message =
    errorBody &&
    typeof errorBody === "object" &&
    typeof (errorBody as { message?: unknown }).message === "string"
      ? (errorBody as { message: string }).message
      : typeof errorBody === "string"
        ? errorBody
        : "";
  if (!message) return fallback;
  return message.replace(/nvapi-[A-Za-z0-9_-]+/g, "[redacted]").slice(0, 300);
}

function requestAvailable() {
  return !loading && eligibleKeys > 0 && !["offline", "stale", "error", "recovery"].includes(state);
}

function mutationAllowed() {
  return !loading && !["offline", "stale", "error", "recovery"].includes(state);
}

function validKey(value: unknown): value is Key {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<Key>;
  return (
    typeof item.id === "string" &&
    typeof item.label === "string" &&
    typeof item.fingerprint === "string" &&
    typeof item.enabled === "boolean" &&
    typeof item.verified === "boolean" &&
    (item.cooldown_until === null || typeof item.cooldown_until === "string") &&
    Number.isSafeInteger(item.request_count) &&
    Number.isSafeInteger(item.failure_count)
  );
}

function validClient(value: unknown): value is Client {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<Client>;
  return (
    typeof item.id === "string" &&
    typeof item.label === "string" &&
    Array.isArray(item.scopes) &&
    item.scopes.every((scope) => typeof scope === "string") &&
    typeof item.active === "boolean" &&
    Number.isSafeInteger(item.request_count) &&
    (item.revoked_at === null || typeof item.revoked_at === "string")
  );
}

function formatDateTime(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? "확인할 수 없음"
    : parsed.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
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
    clearSession();
    error = "관리자 토큰이 만료되었거나 올바르지 않습니다. 다시 인증하세요.";
    state = "error";
    announcement = "인증이 만료되어 로그인 화면으로 돌아왔습니다.";
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
    structuralReady = healthReady;
    trafficReady = healthTrafficReady;
    eligibleKeys = healthEligibleKeys;
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
    };
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
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
    structuralReady = healthReady && snapshot.runtime?.ready === true;
    trafficReady = healthTrafficReady && snapshot.runtime?.traffic_ready === true;
    eligibleKeys = Math.min(
      healthEligibleKeys,
      Number.isInteger(snapshot.runtime?.eligible_keys)
        ? Number(snapshot.runtime?.eligible_keys)
        : 0,
    );
    state = keys.length === 0 ? "empty" : trafficReady ? "ready" : "degraded";
    updatedAt = formatDateTime(new Date().toISOString());
    announcement = "상태를 새로 확인했습니다.";
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") {
      if (epoch !== refreshEpoch) return;
      error = "상태 확인 시간이 초과되었습니다. 다시 확인하세요.";
      state = navigator.onLine === false ? "offline" : keys.length > 0 ? "stale" : "error";
      trafficReady = false;
      announcement = "상태 확인이 시간 초과되어 다시 확인이 필요합니다.";
      return;
    }
    if (epoch !== refreshEpoch || currentAuthEpoch !== authEpoch) return;
    const message = caught instanceof Error ? caught.message : "상태를 읽지 못했습니다.";
    error = message;
    state = navigator.onLine === false ? "offline" : keys.length > 0 ? "stale" : "error";
    trafficReady = false;
  } finally {
    clearTimeout(timeout);
    if (epoch === refreshEpoch) loading = false;
  }
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
    error = "라벨과 NVIDIA API 키를 모두 입력하세요.";
    return;
  }
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  error = "";
  let body = JSON.stringify({ label, credential });
  try {
    const response = await request("/admin/api/v1/upstream-keys", {
      method: "POST",
      body,
      signal: controller.signal,
    });
    if (operation !== mutationEpoch) return;
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseMessage(payload, "NVIDIA 키를 저장하지 못했습니다."));
    upstreamLabel = "";
    notice = "NVIDIA 키를 암호화해 저장했습니다. 키 원문은 다시 표시되지 않습니다.";
    state = "success";
    await refresh();
  } catch (caught) {
    if (controller.signal.aborted || operation !== mutationEpoch) return;
    error = caught instanceof Error ? caught.message : "NVIDIA 키 저장 결과를 확인하지 못했습니다.";
    state = caught instanceof TypeError ? "recovery" : "error";
    mutationState = caught instanceof TypeError ? "unknown" : "reconcile";
  } finally {
    body = "";
    mutating = false;
    if (mutationController === controller) mutationController = null;
    if (mutationState === "pending") mutationState = error ? "reconcile" : "idle";
  }
}

async function issueClient(event: SubmitEvent) {
  event.preventDefault();
  if (mutating || clientScopes.length === 0 || !mutationAllowed()) return;
  const label = clientLabel.trim();
  if (!label) {
    error = "접속 키 라벨을 입력하세요.";
    return;
  }
  mutating = true;
  const operation = ++mutationEpoch;
  mutationController?.abort();
  const controller = new AbortController();
  mutationController = controller;
  mutationState = "pending";
  error = "";
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
    notice = `${key.label}을(를) 라우팅에서 사용 중지했습니다. 필요하면 다시 포함할 수 있습니다.`;
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

function modelInfo(model: string) {
  return modelCapabilities[model] ?? { modalities: "제공자 프로필", route: "/v1/nvidia/inference" };
}

function profileReadiness() {
  const total = models.length || 8;
  return `${requestAvailable() ? models.length || total : 0}/${total}`;
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
  const onPageHide = () => {
    const authSnapshot = session.token;
    const credentialSnapshot = custody.credentialId;
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
    if (document.visibilityState === "visible" && session.token && !loading && !mutating) {
      void refresh();
    }
  };
  const onOnline = () => {
    if (session.token) void refresh();
  };
  const onOffline = () => {
    trafficReady = false;
    state = "offline";
    announcement = "오프라인 상태입니다. 연결되면 다시 확인하세요.";
  };
  pollingHandle = window.setInterval(() => {
    if (document.visibilityState === "visible" && session.token && !loading && !mutating) {
      void refresh();
    }
  }, 10_000);
  addEventListener("hashchange", onHash);
  addEventListener("popstate", onHash);
  addEventListener("pagehide", onPageHide);
  addEventListener("pageshow", onPageShow);
  addEventListener("visibilitychange", onVisibility);
  addEventListener("online", onOnline);
  addEventListener("offline", onOffline);
  return () => {
    refreshController?.abort();
    removeEventListener("hashchange", onHash);
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
    <div><p class="eyebrow">NVIDIA BUILD LB</p><p class="brand">관리 콘솔</p></div>
    <div class="top-actions"><span class:good={requestAvailable()} class="status"><span aria-hidden="true">{requestAvailable() ? "●" : "○"}</span> {stateCopy()}</span><button class="secondary" type="button" onclick={() => void refresh()} disabled={!session.token || loading || mutating}>상태 새로고침</button><button class="secondary" type="button" onclick={logout} disabled={!session.token || secretOpen}>로그아웃</button></div>
  </header>

  {#if !authenticated}
  <form class="auth" aria-label="관리자 인증" onsubmit={login}>
    <label for="admin-token">관리자 토큰<input id="admin-token" bind:this={adminInput} type="password" autocomplete="off" aria-describedby={error ? "error-message" : undefined} aria-invalid={error ? "true" : undefined} placeholder="토큰은 확인 후 화면에서 지워집니다" /></label>
    <button class="secondary" type="submit" disabled={loading}>{loading ? "확인 중…" : "인증하고 확인"}</button>
  </form>
  {/if}

  {#if authenticated}
  <nav aria-label="관리 메뉴" class="nav">
    {#each adminRoutes as route}
      <a href={route.href} aria-current={active === route.id ? "page" : undefined} onclick={(event) => { event.preventDefault(); void selectRoute(route.id); }}>{route.label}</a>
    {/each}
  </nav>

  <main id="main-content" aria-busy={loading}>
    <div class="route-heading"><p class="eyebrow">{routeTitle[active]}</p><h1 id="route-heading" bind:this={routeHeading} tabindex="-1">{routeTitle[active]}</h1><p class="muted">{routeDescription[active]}</p></div>
    <p class="live" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    {#if mutationState === "pending" && mutating}<p class="attention" role="status" aria-live="polite">변경 사항을 저장하는 중입니다. 완료될 때까지 조작하지 마세요.</p>{/if}
    {#if mutationState === "unknown"}<p class="attention" role="alert">이전 작업의 결과를 확인하지 못했습니다. 현재 화면은 오래된 상태일 수 있어 다시 확인한 뒤 재시도하세요.<button class="link-button" type="button" onclick={() => void refresh()}>상태 다시 확인</button></p>{/if}
    {#if mutationState === "reconcile"}<p class="attention" role="alert">작업은 전송됐지만 최신 상태 확인에 실패했습니다. 재시도하지 말고 먼저 상태를 다시 확인하세요.<button class="link-button" type="button" onclick={() => void refresh()}>상태 다시 확인</button></p>{/if}
    {#if error}<p id="error-message" class="alert" role="alert">{error}<button class="link-button" type="button" onclick={() => void refresh()}>다시 확인</button></p>{/if}
    {#if notice}<p class="notice" role="status">{notice}</p>{/if}

    <section id="overview" class:panel-hidden={active !== "overview"} class="panel" aria-labelledby="overview-title" hidden={active !== "overview"}>
        <h2 id="overview-title">지금의 판단</h2>
        <div class="judgments">
          <article><span>구조 상태</span><strong>{structuralReady ? "검증 완료" : "확인 필요"}</strong><small>{structuralReady ? "게이트웨이와 저장소가 연결돼 있습니다." : "관리자 인증과 키 구성이 필요합니다."}</small></article>
          <article><span>현재 요청 가능 모델</span><strong>{profileReadiness()} 모델</strong><small>{requestAvailable() ? "현재 등록된 모든 프로필이 동일한 eligible 슬롯을 사용합니다." : "모든 프로필이 중지·cooldown이거나 없습니다."}</small></article>
          <article><span>구성된 슬롯</span><strong>{keys.length}/2 슬롯</strong><small>두 개의 서로 다른 키를 암호화해 저장합니다.</small></article>
          <article><span>사용 가능한 키</span><strong>{eligibleKeys}/2 키</strong><small>{requestAvailable() ? "rate-aware round-robin으로 분산합니다." : "라우팅에서 중지·cooldown 원인을 확인하세요."}</small></article>
          <article><span>다음 조치</span><strong>{recommendedAction()}</strong><small>{notice || error || (updatedAt ? `마지막 확인 ${updatedAt}` : "아직 확인하지 않았습니다.")}</small></article>
        </div>
    </section>
    <section id="routing" class:panel-hidden={active !== "routing"} class="panel" aria-labelledby="routing-title" hidden={active !== "routing"}>
        <h2 id="routing-title">두 슬롯의 상태</h2>
        <p class="muted">활성·cooldown 키만 새 요청을 받을 수 있습니다. 원문 키는 표시하지 않습니다.</p>
        <div class="table-wrap">
          <table><caption class="sr-only">NVIDIA upstream 슬롯</caption><thead><tr><th scope="col">키</th><th scope="col">상태</th><th scope="col">누적</th><th scope="col"><span class="sr-only">조작</span></th></tr></thead><tbody>
            {#each [0, 1] as index}
              {@const key = keys[index]}
              {#if key}
                {@const probe = key.verified ? "valid" : probeState[key.id]}
                <tr><th scope="row"><span id={`key-${key.id}`}>슬롯 {index + 1} · {key.label}</span><small>{key.fingerprint.slice(0, 15)}…</small></th><td data-label="상태"><span class:good={key.enabled && !key.cooldown_until} class="status"><span aria-hidden="true">{key.enabled && !key.cooldown_until ? "●" : "○"}</span> {key.enabled ? (key.cooldown_until ? "일시 대기" : "활성") : (probe === "invalid" ? "검증 실패" : probe === "valid" ? "검증 완료" : "검증 필요")}</span>{#if key.cooldown_until}<small>{formatDateTime(key.cooldown_until)}까지</small>{/if}{#if !key.enabled && probe === "invalid"}<small>키를 교체하거나 다시 검증하세요.</small>{/if}</td><td data-label="누적">{key.request_count}회 요청 · {key.failure_count}회 실패</td><td data-label="조작" class="actions">{#if !key.enabled}<button class="secondary" type="button" aria-label={`${key.label} 제공자 검증`} onclick={() => void probeKey(key)} disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed()}>{probe === "pending" ? "검증 중…" : "검증"}</button>{/if}<button class="secondary" type="button" aria-label={`${key.label} ${key.enabled ? "라우팅 제외" : "라우팅 포함"}`} onclick={() => void toggleKey(key)} disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed() || (!key.enabled && probe !== "valid")}>{key.enabled ? "제외" : "포함"}</button><button class="danger" type="button" aria-label={`${key.label} 라우팅에서 사용 중지`} onclick={(event) => openConfirmation("delete", key.id, key.label, event)} disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed()}>사용 중지</button></td></tr>
              {:else}
                <tr><th scope="row">슬롯 {index + 1}</th><td data-label="상태">구성 필요</td><td data-label="누적">아직 등록된 키가 없습니다.</td><td data-label="조작"></td></tr>
              {/if}
            {/each}
          </tbody></table>
        </div>
        {#if keys.length < 2 || keys.some((key) => !key.enabled)}<div class="subpanel"><h3>{keys.length === 0 ? "첫 번째 키 추가" : keys.length < 2 ? "두 번째 키 추가" : "중지된 슬롯 교체"}</h3><p>서로 다른 두 키만 저장할 수 있습니다. 저장 후 원문은 즉시 지워집니다.</p><form bind:this={upstreamForm} class="form" onsubmit={addUpstream}><label for="upstream-label">라벨<input id="upstream-label" bind:value={upstreamLabel} maxlength="128" required placeholder="예: nvidia-primary" /></label><label for="upstream-credential">NVIDIA API 키<input id="upstream-credential" bind:this={upstreamCredentialInput} type="password" required autocomplete="off" oninput={() => (formRevision += 1)} placeholder="nvapi-…" /></label><span class="sr-only">{formRevision}</span><button class="primary" type="submit" disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed()}>암호화 저장</button></form></div>{/if}
    </section>
    <section id="clients" class:panel-hidden={active !== "clients"} class="panel" aria-labelledby="clients-title" hidden={active !== "clients"}>
        <h2 id="clients-title">필요한 권한만 발급</h2><p class="muted">새 접속 키는 발급 직후 native dialog에서 한 번만 보입니다.</p>
        <form bind:this={clientForm} class="form" onsubmit={issueClient}><label for="client-label">라벨<input id="client-label" bind:this={clientLabelInput} bind:value={clientLabel} maxlength="128" required placeholder="예: hermes" /></label><fieldset><legend>권한 범위</legend>{#each supportedScopes as [scope, label]}<label class="check"><input type="checkbox" value={scope} bind:group={clientScopes} /> <span>{label}</span><small>{scope}</small></label>{/each}</fieldset><button class="primary" type="submit" disabled={!session.token || !clientLabel.trim() || clientScopes.length === 0 || mutating || mutationState !== "idle" || !mutationAllowed()}>접속 키 발급</button></form>
        <div class="table-wrap"><table><caption class="sr-only">다운스트림 접속 키</caption><thead><tr><th scope="col">라벨</th><th scope="col">권한</th><th scope="col">상태</th><th scope="col"><span class="sr-only">조작</span></th></tr></thead><tbody>{#each clients as client}<tr><th scope="row" id={`client-${client.id}`}>{client.label}<small>{client.request_count}회 사용</small></th><td data-label="권한">{client.scopes.join(", ")}</td><td data-label="상태">{client.active ? "사용 중" : "폐기됨"}</td><td data-label="조작">{#if client.active}<button class="danger" type="button" aria-label={`${client.label} 접속 키 폐기`} onclick={(event) => openConfirmation("revoke", client.id, client.label, event)} disabled={!session.token || mutating || mutationState !== "idle" || !mutationAllowed()}>폐기</button>{/if}</td></tr>{:else}<tr><td colspan="4">발급된 접속 키가 없습니다.</td></tr>{/each}</tbody></table></div>
    </section>
    <section id="models" class="panel" aria-labelledby="models-title" hidden={active !== "models"}><h2 id="models-title">모달리티별 모델</h2><div class="model-list">{#each models as model}<article><h3>{model}</h3><p>{modelInfo(model).modalities}</p><code>{modelInfo(model).route}</code><small>{requestAvailable() ? "현재 라우팅 가능" : "현재 요청 가능 여부는 라우팅에서 확인"}</small></article>{:else}<p class="muted">관리자 인증 후 모델을 확인하세요.</p>{/each}</div></section>
    <section id="evidence" class="panel" aria-labelledby="evidence-title" hidden={active !== "evidence"}><h2 id="evidence-title">지속성 확인</h2>{#if !session.token}<p class="muted">관리자 인증 후 지속성 증거를 확인할 수 있습니다.</p>{:else}<p class="muted">마지막 성공 snapshot과 PostgreSQL source-of-truth를 표시합니다.</p><div class="facts"><span>확인 시각 <strong>{updatedAt || "없음"}</strong></span><span>저장 원본 <strong>{evidence.source_of_truth}</strong></span><span>DB 키 <strong>{evidence.persisted_upstream_keys}</strong></span><span>DB 접속 키 <strong>{evidence.persisted_downstream_credentials}</strong></span><span>라우팅 프로필 <strong>{evidence.persisted_routing_profiles}</strong></span><span>요청 시도 <strong>{evidence.persisted_request_attempts ?? 0}</strong></span></div>{#if evidence.source_of_truth === "unavailable"}<p class="attention">지속성 증거를 확인하지 못했습니다. 이 snapshot을 운영 증거로 사용하지 말고 저장소 상태를 다시 확인하세요.</p>{/if}{#if (state === "degraded" || state === "stale") && eligibleKeys === 0}<p class="attention">현재 요청 가능한 키가 없습니다. 라우팅에서 cooldown·중지 원인을 확인하세요.</p>{/if}{/if}</section>
  </main>
  {/if}
</div>

<dialog bind:this={secretDialog} aria-labelledby="secret-title" aria-describedby="secret-description" oncancel={(event) => { event.preventDefault(); }}>
  <form method="dialog" class="dialog-card" onsubmit={(event) => { event.preventDefault(); clearIssuedToken(); }}>
    <h2 id="secret-title" bind:this={secretTitle} tabindex="-1">지금만 표시되는 접속 키</h2><p id="secret-description">안전한 비밀 저장소에 복사한 뒤 아래 버튼으로 화면에서 지우고 닫으세요. 키는 다시 표시되지 않습니다.</p><textarea bind:this={secretField} readonly aria-label="일회성 접속 키"></textarea>{#if secretCleanup === "manual"}<p class="attention" role="alert">클립보드를 자동으로 비우지 못했습니다. 클립보드를 직접 비운 뒤 확인란을 선택하세요.</p><label class="check" for="manual-cleanup"><input id="manual-cleanup" type="checkbox" bind:checked={manualCleanupConfirmed} /> 클립보드를 직접 비웠습니다</label>{/if}<div class="actions"><button class="secondary" type="button" onclick={() => void copyIssuedToken()}>클립보드에 복사</button>{#if secretCleanup === "manual"}<button class="secondary" type="button" onclick={() => void clearIssuedToken(true)} disabled={!manualCleanupConfirmed}>수동 확인 후 닫기</button>{:else}<button class="primary" type="submit" disabled={secretCleanup === "pending"}>{secretCleanup === "pending" ? "클립보드 정리 중…" : "지우고 닫기"}</button>{/if}</div>
  </form>
</dialog>

<dialog bind:this={confirmDialog} aria-labelledby="confirm-title" onclose={() => { if (!confirmPending) pendingAction = null; void tick().then(() => confirmReturnFocus?.focus()); }}>
  <form method="dialog" class="dialog-card" onsubmit={confirmDestructive}>
    <h2 id="confirm-title" bind:this={confirmTitle} tabindex="-1">사용 중지 확인</h2>
    <p>{pendingAction?.label ?? "이 항목"}을(를) 라우팅에서 사용 중지합니다. 나중에 다시 포함할 수 있습니다. 계속할까요?</p>
    <div class="actions"><button class="secondary" type="button" onclick={() => confirmDialog?.close()} disabled={confirmPending}>{confirmPending ? "처리 중…" : "취소"}</button><button class="danger" type="submit" disabled={confirmPending}>{confirmPending ? "사용 중지 중…" : "사용 중지 확인"}</button></div>
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
  .topbar, .auth, .nav, .route-heading { display: flex; gap: 16px; align-items: center; } .topbar { justify-content: space-between; min-height: 56px; } .top-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; } .brand { margin: 0; font-size: 1.1rem; font-weight: 800; } .eyebrow { margin: 0; color: #76b900; font-size: .72rem; font-weight: 800; letter-spacing: .12em; }
  .status { display: inline-flex; align-items: center; gap: 5px; width: fit-content; border: 1px solid #6b746f; border-radius: 6px; padding: 8px 10px; color: #d0d5d2; font-weight: 700; white-space: nowrap; } .status.good { color: #76b900; border-color: #76b900; }
  .auth { align-items: end; margin: 16px 0; } .auth label { flex: 1; } label { display: grid; gap: 6px; color: #d0d5d2; font-size: .9rem; font-weight: 700; } input, textarea { width: 100%; min-height: 44px; border: 1px solid #6b746f; border-radius: 6px; padding: 10px 12px; color: #f5f7f6; background: #171a1d; font: inherit; } textarea { min-height: 76px; resize: vertical; }
  .nav { display: flex; flex-wrap: wrap; overflow: visible; border-bottom: 1px solid #6b746f; } .nav a { min-height: 44px; padding: 10px 12px; color: #d0d5d2; text-decoration: none; white-space: nowrap; } .nav a[aria-current="page"] { color: #76b900; border-bottom: 3px solid #76b900; font-weight: 800; }
  main { padding-top: 32px; } .route-heading { align-items: baseline; flex-wrap: wrap; margin-bottom: 24px; } .route-heading .eyebrow { flex-basis: 100%; } h1, h2, h3, p { margin: 0; } h1 { font-size: clamp(1.6rem, 3vw, 2rem); } h2 { font-size: 1.35rem; } h3 { font-size: 1rem; } .muted, small { color: #aab2ae; } .live { min-height: 24px; color: #d0d5d2; }
  .panel, article, .subpanel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; } .judgments { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; } article { background: #1d2124; } article span { color: #aab2ae; font-size: .85rem; } article strong { font-size: 1.25rem; }
  .alert, .notice, .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; } .alert { color: #ff8a8a; border: 1px solid #ff8a8a; } .notice { color: #76b900; border: 1px solid #76b900; } .attention { color: #ffd166; border: 1px solid #ffd166; }
  .link-button { margin-left: auto; color: inherit; background: transparent; border: 1px solid currentColor; border-radius: 6px; padding: 8px 10px; min-height: 44px; cursor: pointer; }
  button { min-height: 44px; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; font: inherit; font-weight: 800; } button:disabled { opacity: .5; cursor: not-allowed; } .primary { background: #76b900; color: #091006; } .secondary { background: #262b2e; color: #f5f7f6; border: 1px solid #6b746f; } .danger { background: transparent; color: #ff8a8a; border: 1px solid #ff8a8a; }
  .table-wrap { overflow-x: auto; } table { width: 100%; border-collapse: collapse; min-width: 680px; } th, td { padding: 12px 10px; border-bottom: 1px solid #6b746f; text-align: left; vertical-align: middle; } th { color: #f5f7f6; } td, th small { display: table-cell; } tbody th { display: table-cell; } tbody th small { display: block; margin-top: 3px; } .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .form { display: grid; gap: 12px; max-width: 560px; } fieldset { display: grid; gap: 8px; border: 1px solid #6b746f; border-radius: 6px; padding: 14px; } legend { padding: 0 5px; color: #d0d5d2; font-weight: 800; } .check { display: flex; align-items: center; gap: 8px; min-height: 44px; } .check input { width: 20px; min-height: 20px; } .check small { margin-left: auto; }
  .subpanel { margin-top: 20px; } .facts, .model-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; } .facts span { padding: 12px; background: #1d2124; border-radius: 6px; } .facts strong { display: block; margin-top: 4px; } .model-list { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); } code { color: #b8a1ff; overflow-wrap: anywhere; }
  dialog { width: min(560px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); padding: 0; border: 1px solid #6b746f; border-radius: 8px; background: #171a1d; color: #f5f7f6; box-shadow: 0 8px 24px rgb(0 0 0 / .28); } dialog::backdrop { background: rgb(0 0 0 / .7); } .dialog-card { display: grid; gap: 16px; padding: 24px; } .dialog-card textarea { color: #76b900; font-family: ui-monospace, monospace; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  @media (max-width: 767px) { .shell { padding-inline: 16px; } .auth, .route-heading { align-items: stretch; flex-direction: column; } .topbar { align-items: flex-start; flex-direction: column; } .top-actions { width: 100%; justify-content: stretch; } .top-actions button, .top-actions .status { flex: 1; } .auth button, .form button { width: 100%; } .judgments { grid-template-columns: 1fr; } .panel { padding: 16px; } .nav { gap: 4px; } .nav a { flex: 1 1 calc(50% - 4px); text-align: center; } table { min-width: 0; } thead { display: none; } table, tbody, tr, th, td { display: block; width: 100%; } tr { padding: 12px 0; border-bottom: 1px solid #6b746f; } th, td { border: 0; padding: 5px 0; } td::before { content: attr(data-label); display: block; color: #aab2ae; font-size: .8rem; } .actions { justify-content: stretch; } .actions button { flex: 1; } }
  @media (forced-colors: active) { .status, .panel, article, input, textarea, button, fieldset, dialog { border: 1px solid ButtonText; } .primary, .secondary, .danger { background: Canvas; color: ButtonText; } }
  @media (prefers-reduced-motion: reduce) { :global(*) { scroll-behavior: auto !important; transition: none !important; } }
</style>
