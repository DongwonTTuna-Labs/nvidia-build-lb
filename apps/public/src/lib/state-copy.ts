import type { PublicSummary } from "./types";

export type PublicAction =
  | { kind: "link"; label: string; href: string }
  | { kind: "retry"; label: string };

export type PublicStateCopy = {
  title: string;
  message: string;
  action: PublicAction;
};

const COPY_BY_REASON: Record<string, PublicStateCopy> = {
  database_unavailable: {
    title: "상태 확인 불가",
    message: "게이트웨이 저장소 연결을 확인하고 있습니다.",
    action: { kind: "retry", label: "상태 새로 고침" },
  },
  no_eligible_upstream: {
    title: "운영 준비 중",
    message: "현재 요청을 전달할 provider 용량을 준비하고 있습니다.",
    action: { kind: "link", label: "지원 모델 보기", href: "/models" },
  },
  pair_not_ready: {
    title: "제한된 용량으로 운영 중",
    message: "요청은 가능하지만 이중화 용량을 준비하고 있습니다.",
    action: { kind: "link", label: "상세 상태 보기", href: "/status" },
  },
};

export function publicStateCopy(state: PublicSummary["state"] | null | undefined): PublicStateCopy {
  if (state?.reason_code && COPY_BY_REASON[state.reason_code]) {
    return COPY_BY_REASON[state.reason_code];
  }
  if (state?.status === "maintenance") {
    return {
      title: "점검 중",
      message: "계획된 점검이 끝날 때까지 새 요청을 잠시 멈춥니다.",
      action: { kind: "link", label: "점검 이력 보기", href: "/incidents" },
    };
  }
  if (state?.traffic_ready) {
    return {
      title: "정상 운영 중",
      message: "검증된 provider 용량으로 요청을 전달하고 있습니다.",
      action: { kind: "link", label: "연결 시작하기", href: "/docs" },
    };
  }
  return {
    title: "운영 상태 확인 중",
    message: "확인된 상태 정보가 갱신될 때까지 잠시 기다려 주세요.",
    action: { kind: "retry", label: "상태 새로 고침" },
  };
}

export function publicErrorCopy(code: string, fallback: string): PublicStateCopy {
  const known = COPY_BY_REASON[code];
  return {
    title: known?.title ?? "연결 확인 필요",
    message: known?.message ?? fallback,
    action: { kind: "retry", label: "다시 시도" },
  };
}
