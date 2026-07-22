let credential = "";
let credentialGeneration = 0;

type CursorPage<T> = {
  snapshot: { observed_at: string; generated_at: string; stale: boolean };
  items: T[];
  next_before?: string | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly requestId?: string,
    readonly retryable = false,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function token(): string {
  return credential;
}

export function authGeneration(): number {
  return credentialGeneration;
}

export function saveToken(value: string): void {
  credentialGeneration += 1;
  credential = value;
  window.dispatchEvent(new CustomEvent("nblb-auth", { detail: { hasToken: Boolean(value) } }));
}

export function acceptsHttpStatus(
  responseOk: boolean,
  status: number,
  acceptedStatuses: readonly number[],
): boolean {
  return responseOk || acceptedStatuses.includes(status);
}

export function acceptsHttpResponse(
  responseOk: boolean,
  status: number,
  acceptedStatuses: readonly number[],
  payload: unknown,
  acceptedPayload?: (value: unknown) => boolean,
): boolean {
  return responseOk || (acceptedStatuses.includes(status) && acceptedPayload?.(payload) === true);
}

export async function api<T>(
  path: string,
  init: RequestInit = {},
  acceptedStatuses: readonly number[] = [],
  acceptedPayload?: (value: unknown) => boolean,
): Promise<T> {
  const credential = token();
  const requestGeneration = credentialGeneration;
  const response = await fetch(`/admin/api/v2${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...(credential ? { authorization: `Bearer ${credential}` } : {}),
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });
  let parsed: unknown;
  try {
    parsed = response.status === 204 ? undefined : await response.json();
  } catch (error) {
    if (response.ok) throw error;
    parsed = {};
  }
  const value = parsed as T & {
    error?: {
      message?: string;
      code?: string;
      request_id?: string;
      retryable?: boolean;
      details?: Record<string, unknown>;
    };
  };
  if (
    !acceptsHttpResponse(response.ok, response.status, acceptedStatuses, parsed, acceptedPayload)
  ) {
    if (response.status === 401 && requestGeneration === credentialGeneration) {
      window.dispatchEvent(
        new CustomEvent("nblb-auth-invalid", {
          detail: { generation: requestGeneration },
        }),
      );
    }
    throw new ApiError(
      value.error?.message ?? value.error?.code ?? `HTTP ${response.status}`,
      response.status,
      value.error?.code ?? "api_error",
      value.error?.request_id,
      value.error?.retryable ?? response.status >= 500,
      value.error?.details,
    );
  }
  return value;
}

export function adminErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 401) {
    return "관리 token이 없거나 만료되었습니다. 상단에서 다시 인증하세요.";
  }
  return error instanceof Error ? error.message : fallback;
}

export function startPolling(
  load: () => Promise<void>,
  intervalMs = 30_000,
  runImmediately = true,
): () => void {
  let stopped = false;
  let running = false;
  let rerunRequested = false;
  let timer: number | undefined;

  const schedule = (): void => {
    if (!stopped) timer = window.setTimeout(run, intervalMs);
  };
  const run = async (): Promise<void> => {
    if (stopped) return;
    if (running) {
      rerunRequested = true;
      return;
    }
    if (document.visibilityState !== "visible") {
      schedule();
      return;
    }
    running = true;
    window.dispatchEvent(new CustomEvent("nblb-poll-state", { detail: { state: "refreshing" } }));
    try {
      await load();
    } finally {
      running = false;
      if (!stopped) {
        window.dispatchEvent(
          new CustomEvent("nblb-poll-state", { detail: { state: "refreshed" } }),
        );
      }
    }
    if (rerunRequested) {
      rerunRequested = false;
      void run();
      return;
    }
    schedule();
  };
  const onVisibilityChange = (): void => {
    if (document.visibilityState !== "visible") return;
    if (timer !== undefined) window.clearTimeout(timer);
    void run();
  };

  document.addEventListener("visibilitychange", onVisibilityChange);
  if (runImmediately) void run();
  else schedule();
  return () => {
    stopped = true;
    rerunRequested = false;
    if (timer !== undefined) window.clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}

export async function loadCursorWindow<T>(
  path: string,
  loadedCount: number,
  key: (item: T) => string,
): Promise<CursorPage<T>> {
  const target = Math.max(50, loadedCount);
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let snapshot: CursorPage<T>["snapshot"] | null = null;
  do {
    const separator = path.includes("?") ? "&" : "?";
    const page: CursorPage<T> = await api<CursorPage<T>>(
      `${path}${separator}limit=50${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`,
    );
    snapshot ??= page.snapshot;
    for (const item of page.items) {
      const id = key(item);
      if (!seen.has(id)) {
        seen.add(id);
        items.push(item);
      }
    }
    cursor = page.next_before ?? null;
  } while (cursor && items.length < target);
  return {
    snapshot: snapshot ?? {
      observed_at: new Date().toISOString(),
      generated_at: new Date().toISOString(),
      stale: false,
    },
    items,
    next_before: cursor,
  };
}

export function displayTime(value: unknown): string {
  if (typeof value !== "string") return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("ko-KR");
}

export function datetimeLocalValue(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}
