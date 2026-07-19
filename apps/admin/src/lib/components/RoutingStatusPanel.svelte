<script lang="ts">
import { actionLabel, formatDateTime } from "$lib/admin-format";
import type { Key, ProfileCapability, SlotProjection, SnapshotState } from "$lib/admin-types";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { AdminRouteId } from "$lib/copy";

export let active: AdminRouteId;
export let keys: Key[];
export let probeState: Record<string, "idle" | "pending" | "valid" | "invalid">;
export let lifecycleKeyId: string;
export let sessionToken: string;
export let state: SnapshotState;
export let mutating: boolean;
export let mutationState: string;
export let readinessReasons: string[];
export let profileCapabilities: ProfileCapability[];
export let slotProjections: SlotProjection[];
export let onProbe: (key: Key) => void;
export let onToggle: (key: Key) => void;
export let onDelete: (kind: "delete", id: string, label: string, event: MouseEvent) => void;

function eligible(key: Key) {
  return key.enabled && key.verified && !key.cooldown_until;
}

function probeStatus(key: Key) {
  return key.verified ? "valid" : probeState[key.id];
}

function mutationBlocked() {
  return ["offline", "stale", "partial", "error", "recovery"].includes(state);
}
</script>

<section id="routing" class:panel-hidden={active !== "routing"} class="panel" aria-labelledby="routing-title" hidden={active !== "routing"}>
  <h2 id="routing-title">두 슬롯의 상태</h2>
  <p class="muted">활성·cooldown 키만 새 요청을 받을 수 있습니다. 원문 키는 표시하지 않습니다.</p>
  {#if mutationBlocked()}<p id="routing-mutation-blocked" class="attention" role="status" aria-live="polite">최신 상태를 확인하지 못해 변경 조작을 잠갔습니다. 먼저 상태를 다시 확인하세요.</p>{/if}
  <div class="table-wrap">
    <table>
      <caption class="sr-only">NVIDIA upstream 슬롯</caption>
      <thead><tr><th scope="col">키</th><th scope="col">상태</th><th scope="col">누적</th><th scope="col"><span class="sr-only">조작</span></th></tr></thead>
      <tbody>
        {#each [0, 1] as index}
          {@const key = keys[index]}
          {#if key}
            {@const probe = probeStatus(key)}
            <tr>
              <th scope="row"><span id={`key-${key.id}`}>슬롯 {index + 1} · {key.label}</span><small>{key.fingerprint.slice(0, 15)}…</small></th>
              <td data-label="상태">
                <StatusBadge good={eligible(key)} label={key.cooldown_until ? "일시 대기" : !key.verified ? (probe === "invalid" ? "검증 실패" : "검증 필요") : key.enabled ? "활성" : "중지됨"} />
                {#if key.cooldown_until}<small>{formatDateTime(key.cooldown_until)}까지</small>{/if}
                {#if key.id === lifecycleKeyId && !key.enabled && probe !== "valid"}<small class="next-step">다음 단계: 제공자 검증을 통과하면 라우팅에 포함할 수 있습니다.</small>{/if}
                {#if !key.enabled && probe === "invalid"}<small>키를 교체하거나 다시 검증하세요.</small>{/if}
              </td>
              <td data-label="누적">{key.request_count}회 요청 · {key.failure_count}회 실패</td>
              <td data-label="조작" class="actions">
                <button class="secondary" type="button" aria-describedby={mutationBlocked() ? "routing-mutation-blocked" : undefined} aria-label={`슬롯 ${index + 1} ${key.label} ${key.verified ? "재검증" : "제공자 검증"}`} onclick={() => onProbe(key)} disabled={!sessionToken || mutationBlocked() || mutating || mutationState !== "idle"}>{probe === "pending" ? "검증 중…" : key.verified ? "재검증" : "검증"}</button>
                <button class="secondary" type="button" aria-describedby={mutationBlocked() ? "routing-mutation-blocked" : undefined} aria-label={`슬롯 ${index + 1} ${key.label} ${key.enabled ? "라우팅 제외" : "라우팅 포함"}`} onclick={() => onToggle(key)} disabled={!sessionToken || mutationBlocked() || mutating || mutationState !== "idle" || (!key.enabled && probe !== "valid")}>{key.enabled ? "제외" : "포함"}</button>
                <button class="danger" type="button" aria-describedby={mutationBlocked() ? "routing-mutation-blocked" : undefined} aria-label={`슬롯 ${index + 1} ${key.label} 영구 폐기`} onclick={(event) => onDelete("delete", key.id, key.label, event)} disabled={!sessionToken || mutationBlocked() || mutating || mutationState !== "idle"}>영구 폐기</button>
              </td>
            </tr>
          {:else}
            <tr><th scope="row">슬롯 {index + 1}</th><td data-label="상태">구성 필요</td><td data-label="누적">아직 등록된 키가 없습니다.</td><td data-label="조작"></td></tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
  {#if readinessReasons.length}<p class="attention" role="status">발급·운영 준비 조건: {readinessReasons.map(actionLabel).join(" · ")}</p>{/if}
  <slot />
  {#if slotProjections.length}
    <div class="subpanel">
      <h3>프로필별 슬롯 준비</h3>
      <p class="muted">두 슬롯의 자격 증명 상태와 모델별 제공자 proof를 분리해 표시합니다.</p>
      <div class="profile-grid">
        {#each profileCapabilities as capability}
          <article><strong>{capability.id}</strong><small>{capability.route}</small><span>{capability.available_now ? "제공자 검증 완료" : capability.proof_status === "provider_proof_required" ? "제공자 proof 필요" : "두 슬롯 준비 확인 필요"}</span><small>{slotProjections.filter((slot) => slot.profiles.some((profile) => profile.profile_id === capability.id && profile.eligible_now)).length}/2 슬롯 가능</small></article>
        {/each}
      </div>
    </div>
  {/if}
</section>

<style>
  .panel, article, .subpanel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  article { background: #1d2124; min-width: 0; }
  article strong { font-size: 1.25rem; overflow-wrap: anywhere; }
  .profile-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .profile-grid article { padding: 12px; }
  .muted, small { color: #aab2ae; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 680px; }
  th, td { padding: 12px 10px; border-bottom: 1px solid #6b746f; text-align: left; vertical-align: middle; }
  th { color: #f5f7f6; }
  td, th small { display: table-cell; }
  tbody th { display: table-cell; }
  tbody th small { display: block; margin-top: 3px; }
  .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; color: #ffd166; border: 1px solid #ffd166; }
  .next-step { color: #ffd166; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
  button { min-height: 44px; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; font: inherit; font-weight: 800; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .secondary { color: #f5f7f6; background: #30363a; }
  .danger { color: #ff8a8a; background: transparent; border: 1px solid #ff8a8a; }
  @media (max-width: 767px) { .panel, .subpanel { padding: 16px; } .profile-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } table { min-width: 0; } thead { display: none; } table, tbody, tr, th, td { display: block; width: 100%; } tr { padding: 12px 0; border-bottom: 1px solid #6b746f; } th, td { border: 0; padding: 5px 0; } td::before { content: attr(data-label); display: block; color: #aab2ae; font-size: .8rem; } .actions { justify-content: stretch; } .actions button { flex: 1 1 120px; } }
</style>
