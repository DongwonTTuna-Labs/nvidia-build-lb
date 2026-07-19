import type { AdminRouteId } from "$lib/copy";

export const supportedScopes = [
  ["models:read", "모델 조회"],
  ["chat:write", "대화"],
  ["embeddings:write", "임베딩"],
  ["images:write", "이미지"],
  ["audio:write", "음성"],
  ["media:write", "영상·미디어"],
] as const;

export const routeTitle: Record<AdminRouteId, string> = {
  overview: "개요",
  routing: "라우팅",
  clients: "접속 키",
  models: "모델",
  evidence: "증거",
};

export const routeDescription: Record<AdminRouteId, string> = {
  overview: "지금 요청을 받을 수 있는지와 다음 조치만 확인합니다.",
  routing: "두 슬롯의 현재 선택 가능 여부와 장애 전환 상태입니다.",
  clients: "Hermes 등 다운스트림에 필요한 권한만 발급·폐기합니다.",
  models: "지원 모달리티와 실제 요청 경로를 한눈에 확인합니다.",
  evidence: "PostgreSQL 지속성과 마지막 확인 시각을 검증합니다.",
};

export const modelCapabilities: Record<string, { modalities: string; route: string }> = {
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
    route: "/v1/videos/generations",
  },
  "nvidia/magpie-tts-multilingual": { modalities: "음성", route: "/v1/audio/speech" },
  "nvidia/parakeet-ctc-1.1b": { modalities: "음성 전사", route: "/v1/audio/transcriptions" },
};
