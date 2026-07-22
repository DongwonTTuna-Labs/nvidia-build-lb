<script lang="ts">
import { onMount } from "svelte";
import { adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import type { RoutingPolicy } from "$lib/types";

type Simulation = {
  selected_slot: number | null;
  eligible_order: string[];
  retry_allowed: boolean;
  reason_codes: string[];
  would_advance_generation: boolean;
  would_persist: false;
};

let value = $state<RoutingPolicy | null>(null);
let original = $state<RoutingPolicy | null>(null);
let loading = $state(true);
let loadError = $state("");
let actionError = $state("");
let notice = $state("");
let busy = $state<"save" | "simulate" | "">("");
let profile = $state("z-ai/glm-5.2");
let endpoint = $state("/v1/chat/completions");
let stream = $state(false);
let simulation = $state<Simulation | null>(null);
let retryableStatusesDraft = $state("");
let stale = $state(false);
let lastSuccessAt = $state<string | null>(null);
const dirty = $derived(JSON.stringify(value) !== JSON.stringify(original));

async function load(): Promise<void> {
  if (busy !== "" || dirty) {
    stale = true;
    return;
  }
  loading = value === null;
  if (!value) loadError = "";
  try {
    const next = await api<RoutingPolicy>("/routing/policy");
    if (busy !== "" || dirty) {
      stale = true;
      return;
    }
    value = { ...next, retryable_statuses: [...next.retryable_statuses] };
    original = { ...next, retryable_statuses: [...next.retryable_statuses] };
    retryableStatusesDraft = next.retryable_statuses.join(", ");
    stale = false;
    lastSuccessAt = new Date().toISOString();
    loadError = "";
  } catch (error) {
    loadError = adminErrorMessage(error, "라우팅 정책을 읽지 못했습니다.");
  } finally {
    loading = false;
  }
}

async function save(): Promise<void> {
  if (!value || busy || !dirty) return;
  busy = "save";
  actionError = "";
  notice = "";
  try {
    const result = await api<{ item: RoutingPolicy }>("/routing/policy", {
      method: "PATCH",
      body: JSON.stringify({
        retryable_statuses: value.retryable_statuses,
        default_cooldown_seconds: value.default_cooldown_seconds,
      }),
    });
    value = {
      ...result.item,
      retryable_statuses: [...result.item.retryable_statuses],
    };
    original = {
      ...result.item,
      retryable_statuses: [...result.item.retryable_statuses],
    };
    retryableStatusesDraft = result.item.retryable_statuses.join(", ");
    stale = false;
    loadError = "";
    lastSuccessAt = new Date().toISOString();
    notice = `정책 버전 ${value.version}을 활성화했습니다.`;
  } catch (error) {
    actionError = adminErrorMessage(error, "라우팅 정책을 저장하지 못했습니다.");
  } finally {
    busy = "";
  }
}

function reset(): void {
  if (!original || busy) return;
  value = {
    ...original,
    retryable_statuses: [...original.retryable_statuses],
  };
  retryableStatusesDraft = original.retryable_statuses.join(", ");
  stale = false;
  actionError = "";
  notice = "변경 내용을 되돌렸습니다.";
}

async function simulate(): Promise<void> {
  if (busy) return;
  busy = "simulate";
  actionError = "";
  notice = "";
  simulation = null;
  try {
    simulation = await api<Simulation>("/routing/simulate", {
      method: "POST",
      body: JSON.stringify({ profile_id: profile, endpoint, stream }),
    });
  } catch (error) {
    actionError = adminErrorMessage(error, "라우팅 simulation을 실행하지 못했습니다.");
  } finally {
    busy = "";
  }
}

onMount(() => startPolling(() => load()));
</script>

<PageHeader
  eyebrow="CAPACITY"
  title="Routing policy"
  description="정책 변경 전 현재 eligible 순서와 retry boundary를 먼저 확인합니다."
/>
<DataState
  {loading}
  error={loadError}
  hasData={value !== null}
  {lastSuccessAt}
  retry={load}
/>
{#if stale}<p class="stale" role="status">
    편집 중이라 자동 갱신을 보류했습니다 · 마지막 확인 {displayTime(
      lastSuccessAt,
    )} · 저장하거나 되돌린 뒤 다시 확인하세요.
  </p>{/if}
{#if actionError}<p class="action-error" role="alert">{actionError}</p>{/if}
{#if notice}<p class="notice" role="status">{notice}</p>{/if}
{#if value}
  <div class="layout">
    <section>
      <h2>활성 정책 <small>v{value.version}</small></h2>
      <label
        >기본 cooldown 초<input
          type="number"
          min="1"
          max="3600"
          bind:value={value.default_cooldown_seconds}
          disabled={busy !== ""}
        /></label
      >
      <label>
        재시도 HTTP 상태
        <input
          value={retryableStatusesDraft}
          oninput={(event) => {
            retryableStatusesDraft = event.currentTarget.value;
          }}
          onchange={() => {
            const parsed = retryableStatusesDraft
              .split(",")
              .map((item) => Number(item.trim()))
              .filter((item) => Number.isInteger(item) && item >= 100 && item <= 599);
            value!.retryable_statuses = [...new Set(parsed)];
            retryableStatusesDraft = value!.retryable_statuses.join(", ");
          }}
          disabled={busy !== ""}
        />
      </label>
      <dl>
        <div>
          <dt>Streaming</dt>
          <dd>첫 유효 frame 전까지만 failover</dd>
        </div>
        <div>
          <dt>Generation</dt>
          <dd>provider side effect 후 자동 재전송 안 함</dd>
        </div>
      </dl>
      <div class="actions">
        <button type="button" onclick={reset} disabled={busy !== "" || !dirty}
          >되돌리기</button
        ><button
          class="primary"
          onclick={() => void save()}
          disabled={busy !== "" || !dirty}
          >{busy === "save"
            ? "적용 중…"
            : dirty
              ? "새 버전 활성화"
              : "변경 없음"}</button
        >
      </div>
    </section>
    <section>
      <h2>요청 simulation</h2>
      <label>Profile<input bind:value={profile} disabled={busy !== ""} /></label
      >
      <label
        >Endpoint<input bind:value={endpoint} disabled={busy !== ""} /></label
      >
      <label class="check"
        ><input
          type="checkbox"
          bind:checked={stream}
          disabled={busy !== ""}
        /><span>Streaming 요청</span></label
      >
      <button onclick={() => void simulate()} disabled={busy !== ""}
        >{busy === "simulate" ? "계산 중…" : "상태 변경 없이 계산"}</button
      >
      {#if simulation}
        <div class="result" aria-live="polite">
          <strong
            >{simulation.selected_slot
              ? `다음 선택 · Slot ${simulation.selected_slot}`
              : "선택 가능한 slot 없음"}</strong
          ><span
            >후보 {simulation.eligible_order.length}개 · retry {simulation.retry_allowed
              ? "허용"
              : "금지"}</span
          ><small
            >{simulation.would_advance_generation
              ? "실제 요청이면 cursor가 이동합니다."
              : "cursor는 이동하지 않습니다."} Simulation은 상태를 저장하지 않습니다.</small
          ><small
            >{simulation.reason_codes.join(" · ") || "추가 제한 없음"}</small
          >
        </div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .layout {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  section {
    display: grid;
    align-content: start;
    gap: 13px;
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  section h2 {
    margin: 0;
  }
  section h2 small {
    color: #9cdb45;
  }
  label {
    display: grid;
    gap: 5px;
  }
  .check {
    display: flex;
    align-items: center;
    min-height: 44px;
    gap: 8px;
    padding: 8px;
  }
  .check input {
    min-height: auto;
  }
  dl {
    display: grid;
    gap: 8px;
    margin: 0;
  }
  dl div {
    padding: 10px;
    background: #101415;
  }
  dt {
    color: #8f9a94;
    font-size: 0.72rem;
  }
  dd {
    margin: 4px 0 0;
  }
  .result {
    display: grid;
    gap: 5px;
    padding: 12px;
    border-left: 3px solid #76b900;
    background: #101415;
  }
  .result span,
  .result small {
    color: #aeb7b2;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
  }
  .notice,
  .action-error,
  .stale {
    padding: 12px;
    border: 1px solid #76b900;
  }
  .stale {
    border-color: #d99b48;
    color: #ffd08a;
  }
  .action-error {
    border-color: #d65e5e;
    color: #ffb0b0;
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }
</style>
