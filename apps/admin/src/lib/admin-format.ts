import { modelCapabilities } from "$lib/admin-config";
import type { Attention, Client, Key } from "$lib/admin-types";
import type { AdminRouteId } from "$lib/copy";

type ModelInfo = { modalities: string; route: string };

export function responseMessage(body: unknown, fallback: string): string {
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

export function validKey(value: unknown): value is Key {
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

export function validClient(value: unknown): value is Client {
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

export function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? "확인할 수 없음"
    : parsed.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
}

export function actionLabel(action: string): string {
  return (
    {
      probe: "제공자 검증",
      inspect_routing: "라우팅 원인 확인",
      verify_public_route: "공개 경로 확인",
      add_upstream_key: "NVIDIA 키 추가",
      probe_upstream_key: "NVIDIA 키 검증",
      routing: "라우팅 상태 확인",
      evidence: "증거 상세 확인",
      missing_upstream_slot: "부족한 슬롯 구성",
      probe_required: "제공자 검증 실행",
      slot_unavailable: "슬롯 장애 원인 확인",
      readiness_snapshot_unavailable: "준비 상태 다시 확인",
      snapshot_stale: "최신 상태 다시 확인",
      offline: "네트워크 복구 후 다시 확인",
    }[action] ?? "상태 확인"
  );
}

export function attentionLabel(code: string): string {
  return (
    {
      upstream_cooldown: "제공자 대기 중",
      probe_required: "제공자 검증 필요",
      upstream_disabled: "라우팅에서 중지됨",
    }[code] ?? "확인 필요한 상태"
  );
}

export function attentionTarget(attention: Attention): AdminRouteId {
  return attention.next_action === "evidence" ? "evidence" : "routing";
}

export function checkStatusLabel(status: string): string {
  return (
    {
      eligible: "사용 가능",
      cooldown: "일시 대기",
      disabled: "중지됨",
      probe_required: "검증 필요",
    }[status] ?? "확인 필요"
  );
}

export function eventKindLabel(kind: string): string {
  return kind === "request_attempt" ? "요청 시도" : "운영 이벤트";
}

export function outcomeLabel(outcome: string): string {
  return (
    {
      succeeded: "성공",
      failed: "실패",
      started: "진행 중",
      cancelled: "취소됨",
      abandoned_after_restart: "재시작 후 종료",
    }[outcome] ?? "확인 필요"
  );
}

export function modelInfo(model: string): ModelInfo {
  return modelCapabilities[model] ?? { modalities: "제공자 프로필", route: "/v1/nvidia/inference" };
}
