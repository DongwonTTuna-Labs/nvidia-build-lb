import { describe, expect, test } from "bun:test";
import {
  credentialProbeNextAction,
  isModelProbeFailure,
  isProfileProbeFailure,
  leaseRecheckMessage,
  parseCredentialProbeMutation,
  parseModelProbeResponse,
  parseProfileProbeResponse,
  probeErrorClass,
  staleClientActionMessage,
  staleClientActionRefreshFailedMessage,
  withoutProbeResult,
} from "./operations";

const probe = {
  id: "probe-1",
  kind: "credential",
  upstream_id: "upstream-1",
  profile_id: null,
  status: "failed",
  status_code: 401,
  latency_ms: 8,
  error_class: "upstream_auth_error",
  billable: false,
  requested_by: "local_admin",
  created_at: "2026-07-21T00:00:00Z",
  started_at: "2026-07-21T00:00:00Z",
  finished_at: "2026-07-21T00:00:01Z",
};

describe("owner lease recheck", () => {
  test("distinguishes recovery, persistent stale, and refresh failure", () => {
    expect(
      leaseRecheckMessage({
        freshOverview: true,
        ownerLeaseReady: true,
        lastSuccessLabel: "방금",
      }),
    ).toContain("정상으로 돌아왔습니다");
    expect(
      leaseRecheckMessage({
        freshOverview: true,
        ownerLeaseReady: false,
        lastSuccessLabel: "방금",
      }),
    ).toContain("계속 STALE");
    const failed = leaseRecheckMessage({
      freshOverview: false,
      ownerLeaseReady: false,
      lastSuccessLabel: "2026. 7. 21. 09:00",
    });
    expect(failed).toContain("재확인에 실패");
    expect(failed).toContain("마지막 알려진 lease 상태는 STALE");
    expect(failed).toContain("마지막 성공 2026. 7. 21. 09:00");

    const failedAfterKnownRecovery = leaseRecheckMessage({
      freshOverview: false,
      ownerLeaseReady: true,
      lastSuccessLabel: "2026. 7. 21. 09:01",
    });
    expect(failedAfterKnownRecovery).toContain("재확인에 실패");
    expect(failedAfterKnownRecovery).toContain("마지막 알려진 lease 상태는 정상");
  });
});

describe("profile and model probe evidence", () => {
  const profileFailure = { ...probe, kind: "profile", profile_id: "model-1" };

  test("preserves bound profile evidence and rejects a generic 422", () => {
    const evidence = { items: [profileFailure], failed_profile: "model-1" };
    expect(isProfileProbeFailure(evidence, "upstream-1", ["model-1"])).toBe(true);
    expect(
      parseProfileProbeResponse(evidence, "upstream-1", ["model-1"]).runs[0]?.status_code,
    ).toBe(401);
    expect(
      isProfileProbeFailure({ error: { code: "invalid_profile" } }, "upstream-1", ["model-1"]),
    ).toBe(false);
    expect(() =>
      parseProfileProbeResponse({ error: { code: "invalid_profile" } }, "upstream-1", ["model-1"]),
    ).toThrow("저장된 evidence가 없어");
  });

  test("preserves model evidence for each selected slot and rejects a generic 422", () => {
    const evidence = {
      model: { id: "model-1" },
      probe_runs: [
        profileFailure,
        { ...profileFailure, id: "probe-2", upstream_id: "upstream-2", status_code: 500 },
      ],
      audit_event_id: "audit-1",
    };
    expect(isModelProbeFailure(evidence, "model-1", ["upstream-1", "upstream-2"])).toBe(true);
    expect(
      parseModelProbeResponse(evidence, "model-1", ["upstream-1", "upstream-2"]).runs.map(
        (run) => run.upstream_id,
      ),
    ).toEqual(["upstream-1", "upstream-2"]);
    expect(
      isModelProbeFailure({ error: { code: "invalid_upstream_ids" } }, "model-1", ["upstream-1"]),
    ).toBe(false);
    expect(() =>
      parseModelProbeResponse({ error: { code: "invalid_upstream_ids" } }, "model-1", [
        "upstream-1",
      ]),
    ).toThrow("저장된 evidence가 없어");
  });
});

describe("credential probe evidence", () => {
  test("accepts persisted success and 422 failure evidence", () => {
    const failed = parseCredentialProbeMutation(
      { item: probe, audit_event_id: "audit-1" },
      "upstream-1",
    );
    expect(failed.item.status_code).toBe(401);
    expect(probeErrorClass(failed.item)).toBe("upstream_auth_error");
    expect(credentialProbeNextAction(failed.item)).toContain("새 credential");

    const passed = parseCredentialProbeMutation(
      {
        item: { ...probe, status: "passed", status_code: 200, error_class: null },
        audit_event_id: "audit-2",
      },
      "upstream-1",
    );
    expect(credentialProbeNextAction(passed.item)).toContain("profile proof");
    expect(probeErrorClass(passed.item)).toBe("없음");
  });

  test("rejects a generic validation 422 and mismatched upstream evidence", () => {
    expect(() =>
      parseCredentialProbeMutation(
        { error: { code: "validation_error", message: "invalid" } },
        "upstream-1",
      ),
    ).toThrow("저장된 evidence가 없어");
    expect(() =>
      parseCredentialProbeMutation(
        { item: probe, audit_event_id: "audit-1" },
        "different-upstream",
      ),
    ).toThrow("저장된 evidence가 없어");
  });

  test("removes stale evidence when a replacement probe starts", () => {
    const previous = { "upstream-1": probe, "upstream-2": { ...probe, id: "probe-2" } };
    const next = withoutProbeResult(previous, "upstream-1");
    expect(next["upstream-1"]).toBeUndefined();
    expect(next["upstream-2"]?.id).toBe("probe-2");
    expect(previous["upstream-1"]?.id).toBe("probe-1");
  });
});

describe("stale client action recovery", () => {
  test("explains why a stale rotate target disappeared and what recovered", () => {
    expect(staleClientActionMessage("Hermes production")).toBe(
      "Hermes production: 이미 폐기되었거나 상태가 변경되어 최신 목록으로 갱신했습니다.",
    );
  });

  test("does not claim a refresh succeeded when the latest list is unavailable", () => {
    expect(staleClientActionRefreshFailedMessage("Hermes production", "HTTP 503")).toBe(
      "Hermes production: 이미 폐기되었거나 상태가 변경되었습니다. 최신 목록 갱신에도 실패했습니다: HTTP 503",
    );
  });
});
