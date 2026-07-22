import type {
  PublicIncidentDetail,
  PublicIncidents,
  PublicMetrics,
  PublicModels,
  PublicSummary,
} from "$lib/types";

export class PublicApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  let parsed: unknown;
  try {
    parsed = await response.json();
  } catch (error) {
    if (response.ok) throw error;
    parsed = null;
  }
  const body = parsed as T | { error?: { code?: string; message?: string } } | null;
  if (!response.ok) {
    const error = body && typeof body === "object" && "error" in body ? body.error : undefined;
    throw new PublicApiError(
      response.status,
      error?.code ?? "status_unavailable",
      error?.message ?? "현재 상태를 불러오지 못했습니다.",
    );
  }
  if (body === null) {
    throw new PublicApiError(response.status, "invalid_response", "서버 응답을 읽지 못했습니다.");
  }
  return body as T;
}

export const publicApi = {
  summary: () => request<PublicSummary>("/api/public/v1/summary"),
  metrics: (window = "24h", step = "1h") =>
    request<PublicMetrics>(`/api/public/v1/metrics?window=${window}&step=${step}`),
  models: () => request<PublicModels>("/api/public/v1/models"),
  incidents: () => request<PublicIncidents>("/api/public/v1/incidents"),
  incident: (slug: string) =>
    request<PublicIncidentDetail>(`/api/public/v1/incidents/${encodeURIComponent(slug)}`),
};

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
    try {
      await load();
    } finally {
      running = false;
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
