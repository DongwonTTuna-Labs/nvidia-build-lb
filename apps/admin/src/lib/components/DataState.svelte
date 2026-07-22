<script lang="ts">
let {
  loading,
  error,
  stale = false,
  empty = false,
  hasData = false,
  lastSuccessAt = null,
  retryable = true,
  retry,
} = $props<{
  loading: boolean;
  error: string;
  stale?: boolean;
  empty?: boolean;
  hasData?: boolean;
  lastSuccessAt?: string | null;
  retryable?: boolean;
  retry: () => void | Promise<void>;
}>();
let retryBusy = $state(false);
async function runRetry(): Promise<void> {
  if (retryBusy) return;
  retryBusy = true;
  try {
    await retry();
  } finally {
    retryBusy = false;
  }
}
</script>

{#if loading}<div class="state" role="status">
    최신 상태를 확인하고 있습니다…
  </div>{:else if error}<div
    class="state error"
    role={hasData ? "status" : "alert"}
    aria-live={hasData ? "polite" : "assertive"}
    aria-busy={retryBusy}
  >
    <strong
      >{hasData
        ? "기존 데이터를 표시 중입니다"
        : "상태를 불러오지 못했습니다"}</strong
    ><span
      >{error}{#if hasData && lastSuccessAt}
        · 마지막 성공 {new Date(lastSuccessAt).toLocaleString(
          "ko-KR",
        )}{/if}</span
    >{#if retryable}<button
        type="button"
        onclick={() => void runRetry()}
        disabled={retryBusy}>{retryBusy ? "다시 확인 중…" : "다시 시도"}</button
      >{/if}
  </div>{:else if stale}<div
    class="state stale"
    role="status"
    aria-live="polite"
    aria-busy={retryBusy}
  >
    <strong>마지막 정상 snapshot을 표시 중입니다</strong>
    <span>{#if lastSuccessAt}마지막 확인 {new Date(lastSuccessAt).toLocaleString(
          "ko-KR",
        )}{:else}새 snapshot을 확인해 주세요.{/if}</span>
    {#if retryable}<button
        type="button"
        onclick={() => void runRetry()}
        disabled={retryBusy}>{retryBusy ? "다시 확인 중…" : "다시 확인"}</button
      >{/if}
  </div>{:else if empty}<div class="state">
    <strong>아직 표시할 항목이 없습니다.</strong><span
      >필요한 설정 또는 작업을 시작하세요.</span
    >
  </div>{/if}

<style>
  .state {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 18px;
    border: 1px solid #58615c;
    border-radius: 8px;
    background: #171b1c;
    color: #c5cdc9;
  }
  .state span {
    color: #aeb7b2;
  }
  .error {
    border-color: #d99b48;
  }
  .stale {
    border-color: #d99b48;
  }
  .stale strong {
    color: #ffd08a;
  }
  .error strong {
    color: #ffd08a;
  }
</style>
