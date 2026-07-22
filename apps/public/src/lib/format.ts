export function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(value >= 0.1 ? 1 : 2)}%`;
}

export function milliseconds(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString("ko-KR")} ms`;
}

export function observed(value: string | null | undefined): string {
  if (!value) return "아직 확인되지 않음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "확인 시각 알 수 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function age(seconds: number | null): string {
  if (seconds === null) return "검증 기록 없음";
  if (seconds < 60) return "방금 검증";
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}분 전 검증`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}시간 전 검증`;
  return `${Math.floor(seconds / 86_400)}일 전 검증`;
}
