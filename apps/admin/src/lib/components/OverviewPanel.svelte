<script lang="ts">
import { actionLabel, attentionLabel, checkStatusLabel, formatDateTime } from "$lib/admin-format";
import type { Attention, Check, PublicHealth, Recommendation } from "$lib/admin-types";
import type { AdminRouteId } from "$lib/copy";

export let active: AdminRouteId;
export let structuralReady: boolean;
export let profileReadiness: string;
export let requestAvailable: boolean;
export let keysCount: number;
export let eligibleKeys: number;
export let recommendation: Recommendation;
export let attentions: Attention[];
export let checks: Check[];
export let publicHealth: PublicHealth;
export let lastOperation: string;
export let loading: boolean;
export let mutating: boolean;
export let onSelectRoute: (route: AdminRouteId) => void;
export let onAttention: (attention: Attention) => void;
</script>

<section id="overview" class:panel-hidden={active !== "overview"} class="panel" aria-labelledby="overview-title" hidden={active !== "overview"}>
  <h2 id="overview-title">지금의 판단</h2>
  <div class="judgments">
    <article><span>구조 상태</span><strong>{structuralReady ? "검증 완료" : "확인 필요"}</strong><small>{structuralReady ? "게이트웨이와 저장소가 연결돼 있습니다." : "관리자 인증과 키 구성이 필요합니다."}</small></article>
    <article><span>현재 모델 라우팅</span><strong>{profileReadiness}</strong><small>{requestAvailable ? "공통 슬롯을 선택할 수 있습니다. 모델별 제공자 검증 결과는 모델 화면에서 따로 확인하세요." : "모든 슬롯이 중지·cooldown이거나 아직 구성되지 않았습니다."}</small></article>
    <article><span>구성된 슬롯</span><strong>{keysCount}/2 슬롯</strong><small>두 개의 서로 다른 키를 암호화해 저장합니다.</small></article>
    <article><span>사용 가능한 키</span><strong>{eligibleKeys}/2 키</strong><small>{requestAvailable ? "rate-aware round-robin으로 분산합니다." : "라우팅에서 중지·cooldown 원인을 확인하세요."}</small></article>
    <article class="recommendation"><span>다음 조치</span><strong>{recommendation.label}</strong><small>{recommendation.reason}</small><button class="primary" type="button" onclick={() => onSelectRoute(recommendation.route)} disabled={loading || mutating}>{recommendation.route === "evidence" ? "증거 확인" : "조치 화면 열기"}</button></article>
  </div>
  {#if attentions.length}<div class="attention-list" role="status" aria-live="polite" aria-labelledby="attention-title"><h3 id="attention-title">지금 확인할 주의</h3>{#each attentions as attention}<p class="attention"><span><strong>{attention.label ?? attentionLabel(attention.code)}</strong> · 다음 조치: {actionLabel(attention.next_action)}{#if attention.expires_at} · {formatDateTime(attention.expires_at)}까지{/if}</span><button class="link-button" type="button" onclick={() => onAttention(attention)}>{attention.next_action === "probe" ? "검증 시작" : `${actionLabel(attention.next_action)} 열기`}</button></p>{/each}</div>{/if}
  {#if checks.length}<div class="check-list" aria-labelledby="check-title"><h3 id="check-title">슬롯별 상태 점검</h3>{#each checks as check}<p><strong>{check.label}</strong><span>{checkStatusLabel(check.status)} · {check.request_count}회 요청 · {check.failure_count}회 실패</span></p>{/each}</div>{/if}
  <div class="facts"><span>공개 경로 <strong>{publicHealth.status === "verified" ? "검증 완료" : "검증 필요"}</strong><small>{publicHealth.hostname}</small></span><span>마지막 상태 확인 <strong>{lastOperation}</strong></span></div>
</section>

<style>
  .panel, article { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  .judgments { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  article { background: #1d2124; min-width: 0; }
  article span { color: #aab2ae; font-size: .85rem; }
  article strong { font-size: 1.25rem; overflow-wrap: anywhere; }
  .recommendation { border-color: #76b900; }
  .attention-list, .check-list { display: grid; gap: 8px; }
  .check-list p { display: flex; justify-content: space-between; gap: 12px; margin: 0; padding: 10px 0; border-bottom: 1px solid #6b746f; }
  .check-list p > * { min-width: 0; overflow-wrap: anywhere; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .facts span { padding: 12px; background: #1d2124; border-radius: 6px; }
  .facts strong { display: block; margin-top: 4px; }
  small { color: #aab2ae; }
  .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; color: #ffd166; border: 1px solid #ffd166; }
  .attention > * { min-width: 0; overflow-wrap: anywhere; }
  .link-button { margin-left: auto; color: inherit; background: transparent; border: 1px solid currentColor; border-radius: 6px; padding: 8px 10px; min-height: 44px; cursor: pointer; }
  button { min-height: 44px; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; font: inherit; font-weight: 800; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .primary { background: #76b900; color: #091006; }
  @media (max-width: 767px) {
    .panel { padding: 16px; }
    .judgments { grid-template-columns: 1fr; }
  }
</style>
