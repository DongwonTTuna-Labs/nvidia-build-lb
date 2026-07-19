<script lang="ts">
import {
  actionLabel,
  attentionLabel,
  checkStatusLabel,
  eventKindLabel,
  formatDateTime,
  outcomeLabel,
} from "$lib/admin-format";
import type { AdminEvent, Attention, Check, Evidence, SnapshotState } from "$lib/admin-types";
import type { AdminRouteId } from "$lib/copy";

export let active: AdminRouteId;
export let sessionToken: string;
export let updatedAt: string;
export let evidence: Evidence;
export let adminEvents: AdminEvent[];
export let checks: Check[];
export let attentions: Attention[];
export let state: SnapshotState;
export let eligibleKeys: number;
export let onAttention: (attention: Attention) => void;
export let onEvent: (event: AdminEvent) => void;
</script>

<section id="evidence" class="panel" aria-labelledby="evidence-title" hidden={active !== "evidence"}>
  <h2 id="evidence-title">지속성 확인</h2>
  {#if !sessionToken}
    <p class="muted">관리자 인증 후 지속성 증거를 확인할 수 있습니다.</p>
  {:else}
    <p class="muted">마지막 확인 snapshot과 저장 원본을 표시합니다.</p>
    <div class="facts"><span>확인 시각 <strong>{updatedAt || "없음"}</strong></span><span>저장 원본 <strong>{evidence.source_of_truth === "postgresql" ? "PostgreSQL" : evidence.source_of_truth === "encrypted-file-fallback" ? "암호화 파일 fallback" : evidence.source_of_truth}</strong></span><span>DB 키 <strong>{evidence.persisted_upstream_keys}</strong></span><span>DB 접속 키 <strong>{evidence.persisted_downstream_credentials}</strong></span><span>라우팅 프로필 <strong>{evidence.persisted_routing_profiles}</strong></span><span>요청 시도 <strong>{evidence.persisted_request_attempts ?? 0}</strong></span><span>최근 이벤트 <strong>{adminEvents.length}</strong></span></div>
    {#if checks.length}<div class="check-list"><h3>슬롯별 확인</h3>{#each checks as check}<p><strong>{check.label}</strong><span>{checkStatusLabel(check.status)} · {check.request_count}회 요청 · {check.failure_count}회 실패</span></p>{/each}</div>{/if}
    {#if attentions.length}<div class="attention-list"><h3>먼저 확인할 주의</h3>{#each attentions as attention}<p class="attention"><strong>{attention.label ?? attentionLabel(attention.code)}</strong><span>{actionLabel(attention.next_action)}</span><button class="link-button" type="button" onclick={() => onAttention(attention)}>{attention.next_action === "probe" ? "검증 시작" : `${actionLabel(attention.next_action)} 열기`}</button></p>{/each}</div>{/if}
    {#if adminEvents.length}<div class="check-list"><h3>최근 작업</h3>{#each adminEvents as event}<p><strong>{eventKindLabel(event.kind ?? "request_attempt")}</strong><span>{event.profile_id ?? "프로필 미상"} · {outcomeLabel(event.outcome ?? "")}{#if event.created_at} · {formatDateTime(event.created_at)}{/if}</span><button class="link-button" type="button" onclick={() => onEvent(event)}>상세 보기</button></p>{/each}</div>{/if}
    {#if evidence.source_of_truth === "unavailable"}<p class="attention">지속성 증거를 확인하지 못했습니다. 이 snapshot을 운영 증거로 사용하지 말고 저장소 상태를 다시 확인하세요.</p>{/if}
    {#if (state === "degraded" || state === "stale" || state === "partial") && eligibleKeys === 0}<p class="attention">현재 요청 가능한 키가 없습니다. 라우팅에서 cooldown·중지 원인을 확인하세요.</p>{/if}
  {/if}
</section>

<style>
  .panel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  .muted { color: #aab2ae; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .facts span { padding: 12px; background: #1d2124; border-radius: 6px; }
  .facts strong { display: block; margin-top: 4px; }
  .attention-list, .check-list { display: grid; gap: 8px; }
  .check-list p { display: flex; justify-content: space-between; gap: 12px; margin: 0; padding: 10px 0; border-bottom: 1px solid #6b746f; }
  .check-list p > * { min-width: 0; overflow-wrap: anywhere; }
  .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; color: #ffd166; border: 1px solid #ffd166; }
  .attention > * { min-width: 0; overflow-wrap: anywhere; }
  .link-button { margin-left: auto; color: inherit; background: transparent; border: 1px solid currentColor; border-radius: 6px; padding: 8px 10px; min-height: 44px; cursor: pointer; }
  @media (max-width: 767px) { .panel { padding: 16px; } }
</style>
