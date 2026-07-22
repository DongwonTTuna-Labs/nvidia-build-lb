<script lang="ts">
import { onMount } from "svelte";
import { adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import type { Settings } from "$lib/types";

let value = $state<Settings | null>(null);
let original = $state<Settings | null>(null);
let loading = $state(true);
let loadError = $state("");
let actionError = $state("");
let notice = $state("");
let busy = $state(false);
let stale = $state(false);
let lastSuccessAt = $state<string | null>(null);
const dirty = $derived(JSON.stringify(value) !== JSON.stringify(original));

async function load(): Promise<void> {
  if (busy || dirty) {
    stale = true;
    return;
  }
  loading = value === null;
  if (!value) loadError = "";
  try {
    const next = await api<Settings>("/settings");
    if (busy || dirty) {
      stale = true;
      return;
    }
    value = { ...next };
    original = { ...next };
    stale = false;
    lastSuccessAt = new Date().toISOString();
    loadError = "";
  } catch (error) {
    loadError = adminErrorMessage(error, "설정을 읽지 못했습니다.");
  } finally {
    loading = false;
  }
}

async function save(): Promise<void> {
  if (!value || !original || busy || !dirty) return;
  const current = value;
  const baseline = original;
  const patch = Object.fromEntries(
    (Object.keys(current) as Array<keyof Settings>)
      .filter((key) => current[key] !== baseline[key])
      .map((key) => [key, current[key]]),
  );
  busy = true;
  actionError = "";
  notice = "";
  try {
    const result = await api<{ item: Settings }>("/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    value = { ...result.item };
    original = { ...result.item };
    stale = false;
    loadError = "";
    lastSuccessAt = new Date().toISOString();
    notice = "변경한 설정을 저장했습니다.";
  } catch (error) {
    actionError = adminErrorMessage(error, "설정을 저장하지 못했습니다.");
  } finally {
    busy = false;
  }
}

onMount(() => startPolling(() => load()));
</script>

<PageHeader
  eyebrow="GOVERNANCE"
  title="Operations settings"
  description="proof 경고와 보존 기간, 공개 incident 정책을 한 곳에서 관리합니다."
/>
<DataState
  {loading}
  error={loadError}
  hasData={value !== null}
  {lastSuccessAt}
  retry={load}
/>
{#if stale}<p class="stale" role="status">
    저장되지 않은 변경이 있어 자동 갱신을 보류했습니다 · 마지막 확인 {displayTime(
      lastSuccessAt,
    )}
  </p>{/if}
{#if actionError}<p class="action-error" role="alert">{actionError}</p>{/if}
{#if notice}<p class="notice" role="status">{notice}</p>{/if}
{#if value}
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void save();
    }}
    aria-busy={busy}
  >
    <label>
      Proof freshness 초
      <input
        type="number"
        min="300"
        max="2592000"
        bind:value={value.proof_freshness_seconds}
        disabled={busy}
      />
      <small>이 시간이 지나면 UI와 라우팅에서 proof를 stale로 취급합니다.</small
      >
    </label>
    <label>
      Request 보존 일
      <input
        type="number"
        min="7"
        max="90"
        bind:value={value.request_retention_days}
        disabled={busy}
      />
    </label>
    <label>
      Metric 보존 일
      <input
        type="number"
        min="7"
        max="365"
        bind:value={value.metric_retention_days}
        disabled={busy}
      />
    </label>
    <label class="check">
      <input
        type="checkbox"
        bind:checked={value.public_incidents_enabled}
        disabled={busy}
      />
      <span>공개 incident 표시</span>
    </label>
    <div class="actions">
      <button class="primary" disabled={busy || !dirty}
        >{busy ? "저장 중…" : dirty ? "변경 저장" : "변경 없음"}</button
      >
      {#if dirty}<button
          type="button"
          onclick={() => {
            value = { ...original! };
            stale = false;
            actionError = "";
            notice = "변경 내용을 되돌렸습니다.";
          }}
          disabled={busy}>되돌리기</button
        >{/if}
    </div>
  </form>
{/if}

<style>
  form {
    display: grid;
    gap: 16px;
    max-width: 620px;
    padding: 22px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  label {
    display: grid;
    gap: 6px;
  }
  label small {
    color: #aeb7b2;
  }
  .check {
    display: flex;
    align-items: center;
    min-height: 44px;
    gap: 9px;
    padding: 8px;
  }
  .check input {
    min-height: auto;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
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
</style>
