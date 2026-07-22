<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import type { Attention, Overview, Page } from "$lib/types";

let data = $state<Overview | null>(null),
  attentions = $state<Attention[]>([]),
  loading = $state(true),
  dataError = $state(""),
  attentionError = $state(""),
  dataSuccessAt = $state<string | null>(null),
  attentionSuccessAt = $state<string | null>(null);
let attentionStale = $state(false);
let refreshing = $state(false);
async function load(): Promise<void> {
  if (refreshing) return;
  refreshing = true;
  loading = !data;
  try {
    const [overviewResult, attentionResult] = await Promise.allSettled([
      api<Overview>("/overview"),
      api<Page<Attention>>("/attentions"),
    ]);
    if (overviewResult.status === "fulfilled") {
      data = overviewResult.value;
      dataError = "";
      dataSuccessAt = overviewResult.value.snapshot.observed_at;
    } else {
      dataError = adminErrorMessage(overviewResult.reason, "개요를 읽지 못했습니다.");
    }
    if (attentionResult.status === "fulfilled") {
      attentions = attentionResult.value.items;
      attentionError = "";
      attentionSuccessAt = attentionResult.value.snapshot.observed_at;
      attentionStale = attentionResult.value.snapshot.stale;
    } else {
      attentionError = adminErrorMessage(attentionResult.reason, "조치 대기열을 읽지 못했습니다.");
      attentionStale = attentions.length > 0;
    }
  } finally {
    loading = false;
    refreshing = false;
  }
}
const percent = (value: number | null) =>
  value === null ? "표본 부족" : `${(value * 100).toFixed(1)}%`;
onMount(() => startPolling(load));
</script>

<PageHeader
  eyebrow="COMMAND CENTER"
  title="지금 해야 할 한 가지"
  description="상태 → 이유 → 행동 → 증거 순서로 판단합니다."
/>
<DataState
  {loading}
  error={dataError}
  hasData={data !== null}
  stale={data?.snapshot.stale ?? false}
  lastSuccessAt={dataSuccessAt}
  retry={load}
/>
{#if data}
  {#if data.primary_action}<section class="action">
      <p>{data.primary_action.title}</p>
      <h2>{data.primary_action.reason}</h2>
      <a href={`${base}${data.primary_action.href.replace("/admin", "")}`}
        >{data.primary_action.label}</a
      >
    </section>{:else}<section class="action good">
      <p>운영 준비 완료</p>
      <h2>지금 즉시 처리할 차단 항목이 없습니다.</h2>
      <a href={`${base}/requests`}>최근 요청 확인</a>
    </section>{/if}
  <section class="stats" aria-label="핵심 운영 상태">
    <article>
      <span>트래픽</span><strong
        >{data.runtime.traffic_ready ? "요청 가능" : "준비 필요"}</strong
      ><small
        >{data.capacity.eligible_slots}/2 slot eligible · pair {data.capacity
          .pair_ready
          ? "ready"
          : "not ready"}</small
      >
    </article>
    <article>
      <span>런타임</span><strong
        >{data.runtime.database_ready && data.runtime.owner_lease_ready
          ? "정상"
          : "확인 필요"}</strong
      ><small
        >DB {data.runtime.database_ready ? "OK" : "DOWN"} · lease {data.runtime
          .owner_lease_ready
          ? "OK"
          : "STALE"}</small
      >
    </article>
    <article>
      <span>모델</span><strong
        >{data.profiles.available}/{data.profiles.advertised}</strong
      ><small>현재 사용 가능 / advertised</small>
    </article>
    <article>
      <span>Clients</span><strong>{data.clients.active_count}</strong><small
        >active credential</small
      >
    </article>
  </section>
  <section class="metrics">
    <div>
      <span>24h 표본</span><strong>{data.metrics_24h.sample_count}</strong>
    </div>
    <div>
      <span>성공률</span><strong
        >{percent(data.metrics_24h.success_rate)}</strong
      >
    </div>
    <div>
      <span>Failover</span><strong
        >{percent(data.metrics_24h.failover_rate)}</strong
      >
    </div>
    <div>
      <span>Latency p95</span><strong
        >{data.metrics_24h.latency_p95_ms === null
          ? "표본 부족"
          : `${data.metrics_24h.latency_p95_ms}ms`}</strong
      >
    </div>
    <div>
      <span>TTFB p95</span><strong
        >{data.metrics_24h.ttfb_p95_ms === null
          ? "표본 부족"
          : `${data.metrics_24h.ttfb_p95_ms}ms`}</strong
      >
    </div>
  </section>
  <section class="panel">
    <h2>조치 대기열 <small>{attentions.length}</small></h2>
    {#if attentionError || attentionStale}<p
        class="queue-error"
        role={attentionStale || attentionSuccessAt ? "status" : "alert"}
      >
        {attentionStale
          ? `마지막 정상 대기열 표시 중 · 마지막 성공 ${displayTime(attentionSuccessAt)}${attentionError ? " · " : ""}`
          : ""}{attentionError}
        <button
          type="button"
          onclick={() => void load()}
          disabled={refreshing}
          aria-busy={refreshing}>{refreshing ? "확인 중…" : "다시 확인"}</button
        >
      </p>{/if}{#if attentions.length}<ol>
        {#each attentions as item}<li>
            <div><strong>{item.title}</strong><span>{item.reason}</span></div>
            <a href={`${base}${item.action.href.replace("/admin", "")}`}
              >{item.action.label}</a
            >
          </li>{/each}
      </ol>{:else if !attentionError}<p>대기 중인 조치가 없습니다.</p>{/if}
  </section>
  <section class="panel evidence">
    <h2>최근 실제 증거</h2>
    <p>
      <span>NVIDIA 성공</span><strong
        >{displayTime(data.recent.last_nvidia_success_at)}</strong
      >
    </p>
    <p>
      <span>Hermes E2E</span><strong
        >{displayTime(data.recent.last_hermes_e2e_at)}</strong
      >
    </p>
  </section>
{/if}

<style>
  .action,
  .stats article,
  .metrics,
  .panel {
    border: 1px solid #55605a;
    border-radius: 8px;
    background: #171b1c;
  }
  .action {
    padding: 24px;
    margin-bottom: 16px;
    border-left: 5px solid #f1b454;
  }
  .action.good {
    border-left-color: #76b900;
  }
  .action p {
    color: #aeb7b2;
    margin: 0;
  }
  .action h2 {
    margin: 8px 0 18px;
  }
  .action a,
  .panel a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    padding: 10px 14px;
    border-radius: 6px;
    background: #76b900;
    color: #091006;
    text-decoration: none;
    font-weight: 900;
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }
  .stats article {
    padding: 18px;
  }
  .stats span,
  .stats small,
  .metrics span,
  .panel span {
    display: block;
    color: #aeb7b2;
  }
  .stats strong {
    display: block;
    font-size: 1.45rem;
    margin: 6px 0;
  }
  .metrics {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    margin-top: 12px;
  }
  .metrics div {
    padding: 16px;
    border-right: 1px solid #3f4743;
  }
  .metrics div:last-child {
    border: 0;
  }
  .metrics strong {
    display: block;
    margin-top: 6px;
  }
  .panel {
    padding: 20px;
    margin-top: 16px;
  }
  .panel h2 {
    margin-top: 0;
  }
  .panel h2 small {
    color: #9cdb45;
  }
  .panel ol {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .panel li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    padding: 13px 0;
    border-top: 1px solid #3f4743;
  }
  .queue-error {
    padding: 12px;
    border: 1px solid #d99b48;
    color: #ffd08a;
  }
  .evidence p {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    border-top: 1px solid #3f4743;
    padding: 12px 0;
    margin: 0;
  }
  @media (max-width: 900px) {
    .stats {
      grid-template-columns: 1fr 1fr;
    }
    .metrics {
      grid-template-columns: 1fr 1fr;
    }
    .metrics div {
      border-bottom: 1px solid #3f4743;
    }
  }
  @media (max-width: 480px) {
    .stats,
    .metrics {
      grid-template-columns: 1fr;
    }
    .panel li {
      align-items: flex-start;
      flex-direction: column;
    }
    .evidence p {
      display: grid;
    }
  }
</style>
