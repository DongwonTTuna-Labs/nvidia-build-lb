import type { ProbeRun } from "./types";

type LeaseRecheck = {
  freshOverview: boolean;
  ownerLeaseReady: boolean;
  lastSuccessLabel: string;
};

type AuditMutation<T> = {
  item: T;
  audit_event_id: string;
};

export type ProfileProbeResponse = {
  runs: ProbeRun[];
  failedProfile: string | null;
};

export type ModelProbeResponse = {
  modelId: string;
  runs: ProbeRun[];
};

const probeStatuses = new Set(["queued", "running", "passed", "failed", "cancelled"]);

const record = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;

function parseProbeRun(
  value: unknown,
  expected: { kind: "credential" | "profile"; upstreamIds: Set<string>; profileIds: Set<string> },
): ProbeRun {
  const item = record(value);
  const valid =
    typeof item?.id === "string" &&
    item.kind === expected.kind &&
    typeof item.upstream_id === "string" &&
    expected.upstreamIds.has(item.upstream_id) &&
    (expected.kind === "credential"
      ? item.profile_id === null
      : typeof item.profile_id === "string" && expected.profileIds.has(item.profile_id)) &&
    typeof item.status === "string" &&
    probeStatuses.has(item.status) &&
    (item.status_code === null || typeof item.status_code === "number") &&
    (item.error_class === null || typeof item.error_class === "string") &&
    typeof item.billable === "boolean" &&
    typeof item.requested_by === "string" &&
    typeof item.created_at === "string";
  if (!valid) throw new Error("Probe response is not persisted evidence for the requested target.");
  return value as ProbeRun;
}

export function leaseRecheckMessage(result: LeaseRecheck): string {
  if (!result.freshOverview) {
    const lastKnownStatus = result.ownerLeaseReady ? "정상" : "STALE";
    return `lease 재확인에 실패했습니다. 마지막 알려진 lease 상태는 ${lastKnownStatus}입니다. 마지막 성공 ${result.lastSuccessLabel}. gateway 연결을 확인한 뒤 다시 시도하세요.`;
  }
  return result.ownerLeaseReady
    ? "gateway lease가 정상으로 돌아왔습니다."
    : "lease가 계속 STALE입니다. 아래 복구 순서를 진행한 뒤 다시 확인하세요.";
}

export function parseCredentialProbeMutation(
  value: unknown,
  upstreamId: string,
): AuditMutation<ProbeRun> {
  const mutation = record(value);
  if (typeof mutation?.audit_event_id !== "string") {
    throw new Error("Credential probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
  try {
    return {
      item: parseProbeRun(mutation.item, {
        kind: "credential",
        upstreamIds: new Set([upstreamId]),
        profileIds: new Set(),
      }),
      audit_event_id: mutation.audit_event_id,
    };
  } catch {
    throw new Error("Credential probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
}

export function isCredentialProbeFailure(value: unknown, upstreamId: string): boolean {
  try {
    return parseCredentialProbeMutation(value, upstreamId).item.status !== "passed";
  } catch {
    return false;
  }
}

export function parseProfileProbeResponse(
  value: unknown,
  upstreamId: string,
  requestedProfiles: readonly string[],
): ProfileProbeResponse {
  const payload = record(value);
  const failedProfile = typeof payload?.failed_profile === "string" ? payload.failed_profile : null;
  const rawRuns = failedProfile === null ? payload?.runs : payload?.items;
  if (
    !Array.isArray(rawRuns) ||
    rawRuns.length === 0 ||
    (failedProfile === null &&
      (typeof payload?.audit_event_id !== "string" ||
        rawRuns.length !== requestedProfiles.length)) ||
    (failedProfile !== null && !requestedProfiles.includes(failedProfile))
  ) {
    throw new Error("Profile probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
  try {
    const runs = rawRuns.map((run) =>
      parseProbeRun(run, {
        kind: "profile",
        upstreamIds: new Set([upstreamId]),
        profileIds: new Set(requestedProfiles),
      }),
    );
    const uniqueProfiles = new Set(runs.map((run) => run.profile_id));
    const validSuccess =
      failedProfile === null &&
      uniqueProfiles.size === requestedProfiles.length &&
      runs.every((run) => run.status === "passed");
    const validFailure =
      failedProfile !== null &&
      runs.some((run) => run.profile_id === failedProfile && run.status !== "passed");
    if (!validSuccess && !validFailure) throw new Error("invalid profile evidence");
    return { runs, failedProfile };
  } catch {
    throw new Error("Profile probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
}

export function isProfileProbeFailure(
  value: unknown,
  upstreamId: string,
  requestedProfiles: readonly string[],
): boolean {
  try {
    return parseProfileProbeResponse(value, upstreamId, requestedProfiles).failedProfile !== null;
  } catch {
    return false;
  }
}

export function parseModelProbeResponse(
  value: unknown,
  modelId: string,
  upstreamIds: readonly string[],
): ModelProbeResponse {
  const payload = record(value);
  const model = record(payload?.model);
  const rawRuns = payload?.probe_runs;
  if (
    model?.id !== modelId ||
    typeof payload?.audit_event_id !== "string" ||
    !Array.isArray(rawRuns) ||
    rawRuns.length !== upstreamIds.length ||
    rawRuns.length === 0
  ) {
    throw new Error("Model probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
  try {
    const runs = rawRuns.map((run) =>
      parseProbeRun(run, {
        kind: "profile",
        upstreamIds: new Set(upstreamIds),
        profileIds: new Set([modelId]),
      }),
    );
    if (new Set(runs.map((run) => run.upstream_id)).size !== upstreamIds.length) {
      throw new Error("duplicate upstream evidence");
    }
    return { modelId, runs };
  } catch {
    throw new Error("Model probe 응답에 저장된 evidence가 없어 결과를 확인할 수 없습니다.");
  }
}

export function isModelProbeFailure(
  value: unknown,
  modelId: string,
  upstreamIds: readonly string[],
): boolean {
  try {
    return parseModelProbeResponse(value, modelId, upstreamIds).runs.some(
      (run) => run.status !== "passed",
    );
  } catch {
    return false;
  }
}

export function credentialProbeNextAction(run: ProbeRun): string {
  if (run.status === "passed") return "다음: profile proof를 검증하세요.";
  if (run.error_class === "upstream_auth_error") {
    return "다음: 거부된 credential을 폐기하고 새 credential을 저장하세요.";
  }
  if (run.error_class === "upstream_rate_limited") {
    return "다음: provider 제한이 풀린 뒤 credential probe를 다시 실행하세요.";
  }
  return "다음: Probes에서 상세 evidence를 확인하고 원인을 조치한 뒤 다시 실행하세요.";
}

export function probeErrorClass(run: ProbeRun): string {
  return run.error_class ?? "없음";
}

export function staleClientActionMessage(label: string): string {
  return `${label}: 이미 폐기되었거나 상태가 변경되어 최신 목록으로 갱신했습니다.`;
}

export function staleClientActionRefreshFailedMessage(label: string, reason: string): string {
  return `${label}: 이미 폐기되었거나 상태가 변경되었습니다. 최신 목록 갱신에도 실패했습니다: ${reason}`;
}

export function withoutProbeResult<T>(
  results: Record<string, T>,
  upstreamId: string,
): Record<string, T> {
  return Object.fromEntries(Object.entries(results).filter(([id]) => id !== upstreamId));
}
