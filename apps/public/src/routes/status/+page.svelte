<script lang="ts">
import { onMount } from "svelte";
import { PublicApiError, publicApi, startPolling } from "$lib/api";
import { milliseconds, observed, percent } from "$lib/format";
import { publicErrorCopy, publicStateCopy } from "$lib/state-copy";
import type { PublicIncidents, PublicMetrics, PublicSummary } from "$lib/types";

let summary: PublicSummary | null = null;
let metrics: PublicMetrics | null = null;
let incidents: PublicIncidents | null = null;
let window = "24h";
let invalidWindow = "";
let loading = true;
let refreshing = false;
let stale = false;
let error = "";
let errorCode = "";
let metricsError = "";
let incidentsError = "";
let metricsSuccessAt: string | null = null;
let incidentsSuccessAt: string | null = null;
let metricsWindow: string | null = null;

const totalRequests = () =>
  metrics?.points.reduce((sum, point) => sum + point.sample_count, 0) ?? 0;
const maxRequests = () =>
  Math.max(0, ...(metrics?.points.map((point) => point.sample_count) ?? []));
const barHeight = (count: number) =>
  count === 0 || maxRequests() === 0 ? 0 : Math.max(4, (count / maxRequests()) * 100);
const capacitySummary = () => {
  const values =
    metrics?.points.flatMap((point) =>
      point.eligible_provider_count === null ? [] : [point.eligible_provider_count],
    ) ?? [];
  if (!values.length) return `${metricsWindow ?? window} capacity 관측값이 없습니다.`;
  return `${metricsWindow ?? window} capacity 추이. 최저 ${Math.min(...values)} / 2, 최신 ${values.at(-1)} / 2. 정확한 시각별 값은 아래 표에서 확인할 수 있습니다.`;
};
const stateCopy = () => publicStateCopy(summary?.state);
const errorCopy = () => publicErrorCopy(errorCode, error);

async function load(requestedWindow = window, background = false) {
  if (background || summary) refreshing = true;
  else loading = true;
  error = "";
  errorCode = "";
  metricsError = "";
  incidentsError = "";
  try {
    const step = requestedWindow === "1h" ? "5m" : "1h";
    const nextSummary = await publicApi.summary();
    summary = nextSummary;
    const [metricsResult, incidentsResult] = await Promise.allSettled([
      publicApi.metrics(requestedWindow, step),
      publicApi.incidents(),
    ]);
    if (metricsResult.status === "fulfilled") {
      metrics = metricsResult.value;
      metricsSuccessAt = metricsResult.value.snapshot.observed_at;
      metricsWindow = requestedWindow;
      window = requestedWindow;
    } else metricsError = "지표를 불러오지 못했습니다.";
    if (incidentsResult.status === "fulfilled") {
      incidents = incidentsResult.value;
      incidentsSuccessAt = incidentsResult.value.snapshot.observed_at;
    } else incidentsError = "장애 이력을 불러오지 못했습니다.";
    stale =
      nextSummary.snapshot.stale ||
      metrics?.snapshot.stale === true ||
      incidents?.snapshot.stale === true;
  } catch (caught) {
    error = caught instanceof Error ? caught.message : "상태를 불러오지 못했습니다.";
    errorCode = caught instanceof PublicApiError ? caught.code : "status_unavailable";
    stale = summary !== null;
  } finally {
    loading = false;
    refreshing = false;
  }
}

async function selectWindow(value: string) {
  invalidWindow = "";
  window = value;
  const params = new URLSearchParams(globalThis.window.location.search);
  if (value === "24h") params.delete("window");
  else params.set("window", value);
  const query = params.toString();
  globalThis.window.history.pushState(
    {},
    "",
    `${globalThis.window.location.pathname}${query ? `?${query}` : ""}`,
  );
  await load(value, true);
}
onMount(() => {
  const requested = new URLSearchParams(globalThis.window.location.search).get("window");
  if (requested && ["1h", "24h", "7d"].includes(requested)) window = requested;
  else if (requested) invalidWindow = requested;
  const popstate = () => {
    const value = new URLSearchParams(globalThis.window.location.search).get("window") ?? "24h";
    if (["1h", "24h", "7d"].includes(value)) {
      invalidWindow = "";
      window = value;
      void load(value, true);
    } else invalidWindow = value;
  };
  globalThis.window.addEventListener("popstate", popstate);
  const stop = startPolling(() =>
    invalidWindow ? Promise.resolve() : load(window, summary !== null),
  );
  return () => {
    stop();
    globalThis.window.removeEventListener("popstate", popstate);
  };
});
</script>

<svelte:head
  ><title>서비스 상태 · NVIDIA Build LB</title><meta
    name="description"
    content="NVIDIA Build LB의 현재 가용성, 지연, failover와 공개 장애 이력."
  /></svelte:head
>

<header class="page-head">
  <p class="eyebrow">SERVICE STATUS</p>
  <h1>지금 받을 수 있는지,<br />한눈에 확인합니다.</h1>
  <p class="lede">
    aggregate 상태와 확인 가능한 원인만 공개합니다. provider·client·request
    식별자는 이 화면에 존재하지 않습니다.
  </p>
</header>

{#if !invalidWindow && stale && summary}<div class="notice" role="status">
    {#if error}새로 고침 실패: {error} · {/if}마지막 성공 snapshot을 표시합니다 · 마지막 성공 {observed(
      summary.snapshot.observed_at,
    )} ·
    <button
      type="button"
      onclick={() => load(window, true)}
      disabled={refreshing}>{refreshing ? "확인 중" : "다시 시도"}</button
    >
  </div>{/if}
{#if invalidWindow}<section class="section panel error-panel" role="alert">
    <h2>지원하지 않는 지표 기간입니다</h2>
    <p><code>{invalidWindow}</code> 기간은 사용할 수 없습니다. 1h, 24h, 7d 중에서 선택하세요.</p>
    <button class="button secondary" type="button" onclick={() => selectWindow("24h")}
      >24h 상태로 이동</button
    >
  </section>
{:else if loading}<section class="section panel" role="status" aria-busy="true">
    <h2>상태 확인 중</h2>
    <p class="muted">현재 snapshot과 aggregate metric을 불러오고 있습니다.</p>
  </section>
{:else if error && !summary}<section class="section panel error-panel" role="alert">
    <h2>{errorCopy().title}</h2>
    <p>{errorCopy().message}</p>
    <button class="button secondary" type="button" onclick={() => load()}
      >{errorCopy().action.label}</button
    >
  </section>
{:else}
  <section class="section current">
    <article class="panel current-card">
      <div>
        <p class="eyebrow">CURRENT</p>
        <span
          class="status-pill"
          data-tone={summary?.state.traffic_ready ? "ok" : "warning"}
          >{summary?.state.status}</span
        >
        <h2>{stateCopy().title}</h2>
        <p class="muted">{stateCopy().message}</p>
        {#if summary}
          {@const action = stateCopy().action}
          {#if action.kind === "retry"}<button
              class="button secondary"
              type="button"
              onclick={() => load(window, true)}
              disabled={refreshing}
              >{refreshing ? "확인 중" : action.label}</button
            >{:else}<a class="button secondary" href={action.href}
              >{action.label}</a
            >{/if}
        {/if}
      </div>
      <div class="capacity">
        <strong>{summary?.capacity.eligible}</strong><span>/ 2 eligible</span
        ><small>{observed(summary?.snapshot.observed_at)}</small>
      </div>
    </article>
  </section>
  {#if summary}<section class="section endpoint-status" aria-labelledby="endpoint-title">
      <div class="section-title">
        <div>
          <p class="eyebrow">ENDPOINTS</p>
          <h2 id="endpoint-title">기능별 현재 상태</h2>
        </div>
        <a href="/models">모델 proof 상세 →</a>
      </div>
      <div class="endpoint-grid">
        {#each summary.endpoints as endpoint (endpoint.kind)}<article class="panel">
            <strong>{endpoint.kind}</strong>
            <span
              class="status-pill"
              data-tone={endpoint.state === "verified"
                ? "ok"
                : endpoint.state === "unavailable"
                  ? "danger"
                  : "warning"}
              >{endpoint.state}</span
            >
          </article>{/each}
      </div>
    </section>{/if}
  <section class="section">
    {#if metricsError}<div class="notice" role="status">
        {metricsError} 현재 서비스 상태는 위 snapshot을 기준으로 확인하세요.
        {#if metricsSuccessAt}· 표시 중인 지표 {metricsWindow} · 마지막 성공 {observed(
            metricsSuccessAt,
          )}{/if}
        <button
          type="button"
          onclick={() => load(window, true)}
          disabled={refreshing}>{refreshing ? "확인 중" : "지표 다시 확인"}</button
        >
      </div>{/if}
    <div class="range" aria-label="지표 기간">
      {#each ["1h", "24h", "7d"] as value}<button
          type="button"
          class:active={window === value}
          aria-pressed={window === value}
          onclick={() => selectWindow(value)}
          disabled={refreshing}>{value}</button
        >{/each}
    </div>
    {#if metrics}<div class="grid metrics">
        <article class="panel">
          <span>요청</span><strong>{totalRequests()}</strong>
        </article>
        <article class="panel">
          <span>최근 성공률</span><strong
            >{percent(metrics?.points.at(-1)?.success_rate ?? null)}</strong
          >
        </article>
        <article class="panel">
          <span>최근 p95</span><strong
            >{milliseconds(
              metrics?.points.at(-1)?.latency_p95_ms ?? null,
            )}</strong
          >
        </article>
        <article class="panel">
          <span>최근 p95 첫 응답</span><strong
            >{milliseconds(metrics?.points.at(-1)?.ttfb_p95_ms ?? null)}</strong
          >
        </article>
        <article class="panel">
          <span>최근 failover</span><strong
            >{percent(metrics?.points.at(-1)?.failover_rate ?? null)}</strong
          >
        </article>
        <article class="panel">
          <span>Client 취소율</span><strong
            >{percent(metrics?.points.at(-1)?.cancellation_rate ?? null)}</strong
          >
        </article>
        <article class="panel">
          <span>Eligible provider</span><strong
            >{metrics?.points.at(-1)?.eligible_provider_count ?? "—"} / 2</strong
          >
        </article>
      </div>{/if}
    {#if metrics?.points.length}
      <div
        class="capacity-trend panel"
        role="img"
        aria-label={capacitySummary()}
      >
        {#each metrics.points as point (point.at)}<span
            data-capacity={point.eligible_provider_count ?? 0}
            title={`${observed(point.at)} · eligible ${point.eligible_provider_count ?? "—"}/2`}
            >{point.eligible_provider_count ?? "—"}</span
          >{/each}
      </div>
      {#if totalRequests() > 0}
        <div
          class="chart panel"
          role="img"
          aria-label={`${metricsWindow} 요청량 추이. 아래 표에서 정확한 값을 확인할 수 있습니다.`}
        >
          {#each metrics.points as point (point.at)}<div
              class="bar"
              title={`${observed(point.at)} · ${point.sample_count} requests`}
            >
              <span style={`height:${barHeight(point.sample_count)}%`}></span>
            </div>{/each}
        </div>
      {:else}<div class="panel empty-panel">
          <h2>최근 기간에 요청 없음</h2>
          <p>요청 표본은 없지만 eligible provider 관측값은 계속 확인할 수 있습니다.</p>
        </div>{/if}
      <details class="metric-table panel">
        <summary>요청·capacity 정확한 값을 표로 보기</summary>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable data region) -->
        <div
          class="table-scroll"
          role="region"
          aria-label={`${metricsWindow} 지표 표`}
          tabindex="0"
        >
          <table>
            <thead
              ><tr
                ><th>시각</th><th>요청</th><th>성공률</th><th>p95 지연</th><th
                  >p95 첫 응답</th
                ><th>failover</th><th>취소율</th><th>eligible</th></tr
              ></thead
            ><tbody
              >{#each metrics.points as point (point.at)}<tr
                  ><td>{observed(point.at)}</td><td>{point.sample_count}</td><td
                    >{percent(point.success_rate)}</td
                  ><td>{milliseconds(point.latency_p95_ms)}</td><td
                    >{milliseconds(point.ttfb_p95_ms)}</td
                  ><td>{percent(point.failover_rate)}</td><td
                    >{percent(point.cancellation_rate)}</td
                  ><td>{point.eligible_provider_count ?? "—"} / 2</td></tr
                >{/each}</tbody
            >
          </table>
        </div>
      </details>
    {:else if metricsError}<div class="panel error-panel">
        <h2>부가 지표 확인 불가</h2>
        <p>
          핵심 요청 수신 상태와 별개로 지표 조회만 실패했습니다.{#if metricsSuccessAt}
            · 마지막 성공 {observed(metricsSuccessAt)}{/if}
        </p>
      </div>{:else}<div class="panel empty-panel">
        <h2>최근 기간에 요청 없음</h2>
        <p>표본이 생기면 aggregate 추이가 표시됩니다.</p>
      </div>{/if}
  </section>
  <section class="section">
    <div class="section-title">
      <div>
        <p class="eyebrow">INCIDENTS</p>
        <h2>공개 장애 이력</h2>
      </div>
      <a href="/incidents">전체 이력 →</a>
    </div>
    {#if incidentsError}<div
        class="notice"
        role={incidents ? "status" : "alert"}
      >
        <h3>장애 이력 확인 불가</h3>
        <p>
          핵심 요청 수신 상태와 별개로 장애 이력 조회만 실패했습니다.{#if incidentsSuccessAt}
            · 마지막 성공 목록을 표시합니다 · {observed(
              incidentsSuccessAt,
            )}{/if}
        </p>
        <button
          type="button"
          onclick={() => load(window, true)}
          disabled={refreshing}>{refreshing ? "확인 중" : "장애 이력 다시 확인"}</button
        >
      </div>{/if}
    {#if incidents?.items.length}<div class="incident-list">
        {#each incidents.items.slice(0, 3) as incident (incident.slug)}<a
            class="panel"
            href={`/incidents/${incident.slug}`}
            ><span
              class="status-pill"
              data-tone={incident.status === "resolved" ? "ok" : "warning"}
              >{incident.status}</span
            >
            <h3>{incident.title}</h3>
            <p class="muted">시작 {observed(incident.started_at)}</p></a
          >{/each}
      </div>{:else if !incidentsError}<div class="panel empty-panel">
        <h3>공개 중인 장애가 없습니다</h3>
        <p>운영자가 공개한 incident만 이곳에 표시됩니다.</p>
      </div>{/if}
  </section>
{/if}

<style>
  .notice {
    margin-top: 20px;
  }
  .notice button {
    min-height: 44px;
    border: 0;
    background: transparent;
    color: #b8e86f;
    text-decoration: underline;
  }
  .current-card {
    display: flex;
    justify-content: space-between;
    gap: 30px;
    align-items: center;
  }
  .current-card h2 {
    margin: 18px 0 10px;
  }
  .capacity {
    min-width: 170px;
    text-align: right;
  }
  .capacity strong,
  .capacity span,
  .capacity small {
    display: block;
  }
  .endpoint-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }
  .endpoint-grid article {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .capacity-trend {
    display: flex;
    min-height: 76px;
    align-items: end;
    gap: 4px;
    margin-top: 14px;
    overflow-x: auto;
  }
  .capacity-trend span {
    display: grid;
    width: 28px;
    min-width: 28px;
    height: 24px;
    place-items: center;
    border-top: 3px solid #d99b48;
    background: #222b26;
    color: #cbd5cf;
    font-size: 0.7rem;
  }
  .capacity-trend span[data-capacity="2"] {
    height: 52px;
    border-color: #76b900;
  }
  .capacity-trend span[data-capacity="1"] {
    height: 38px;
  }
  .capacity strong {
    color: #a9df5e;
    font-size: 64px;
    line-height: 1;
  }
  .capacity span {
    margin-top: 5px;
  }
  .capacity small {
    margin-top: 15px;
    color: #89978f;
  }
  .range {
    display: flex;
    gap: 6px;
    margin-bottom: 18px;
  }
  .range button {
    min-width: 52px;
    min-height: 44px;
    border: 1px solid #34423c;
    background: transparent;
    color: #aab7b0;
  }
  .range button.active {
    border-color: #76b900;
    background: #16200f;
    color: #dff4bd;
  }
  .metrics article {
    grid-column: span 3;
  }
  .metrics span {
    color: #8f9c95;
    font-size: 12px;
  }
  .metrics strong {
    display: block;
    margin-top: 18px;
    font-size: 28px;
  }
  .chart {
    display: flex;
    align-items: end;
    min-height: 210px;
    gap: 3px;
    margin-top: 18px;
  }
  .bar {
    display: flex;
    align-items: end;
    flex: 1;
    height: 150px;
    min-width: 3px;
  }
  .bar span {
    width: 100%;
    background: linear-gradient(#99d64d, #4f760f);
  }
  .metric-table {
    margin-top: 12px;
  }
  .metric-table summary {
    min-height: 44px;
    cursor: pointer;
    color: #b8e86f;
  }
  .table-scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    min-width: 760px;
    border-collapse: collapse;
  }
  th,
  td {
    padding: 12px;
    border-bottom: 1px solid #2e3a35;
    text-align: left;
  }
  th {
    color: #8e9b94;
    font-size: 11px;
    text-transform: uppercase;
  }
  .section-title {
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: 20px;
    margin-bottom: 18px;
  }
  .section-title a {
    min-height: 44px;
    padding: 12px;
    color: #a9d66d;
  }
  .incident-list {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 18px;
  }
  .incident-list a {
    text-decoration: none;
  }
  .incident-list h3 {
    margin: 18px 0 8px;
  }
  @media (max-width: 760px) {
    .endpoint-grid {
      grid-template-columns: 1fr;
    }
    .current-card {
      align-items: flex-start;
      flex-direction: column;
    }
    .capacity {
      text-align: left;
    }
    .metrics article {
      grid-column: span 6;
    }
    .incident-list {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 460px) {
    .metrics article {
      grid-column: 1 / -1;
    }
  }
</style>
