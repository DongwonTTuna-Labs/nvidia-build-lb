<script lang="ts">
import { onMount } from "svelte";
import { PublicApiError, publicApi, startPolling } from "$lib/api";
import { milliseconds, observed, percent } from "$lib/format";
import { publicErrorCopy, publicStateCopy } from "$lib/state-copy";
import type { PublicSummary } from "$lib/types";

let summary: PublicSummary | null = null;
let loading = true;
let refreshing = false;
let stale = false;
let error = "";
let errorCode = "";

const stateCopy = () => publicStateCopy(summary?.state);
const errorCopy = () => publicErrorCopy(errorCode, error);

async function load(background = false) {
  if (background || summary) refreshing = true;
  else loading = true;
  error = "";
  errorCode = "";
  try {
    summary = await publicApi.summary();
    stale = summary.snapshot.stale;
  } catch (caught) {
    error = caught instanceof Error ? caught.message : "현재 상태를 불러오지 못했습니다.";
    errorCode = caught instanceof PublicApiError ? caught.code : "status_unavailable";
    stale = summary !== null;
  } finally {
    loading = false;
    refreshing = false;
  }
}

onMount(() => startPolling(() => load(summary !== null)));
</script>

<svelte:head
  ><title>NVIDIA Build LB · NVIDIA Hosted API 라우팅</title><meta
    name="description"
    content="두 개의 검증된 NVIDIA hosted API slot을 위한 OpenAI-compatible load balancer."
  /></svelte:head
>

<section class="hero page-head">
  <div>
    <p class="eyebrow">NVIDIA HOSTED API ROUTING</p>
    <h1>필요한 정보와 API에<br /><span>바로 연결</span>됩니다.</h1>
    <p class="lede">
      두 provider slot의 검증 상태를 구분해 보여주고, OpenAI 호환 요청을
      rate-aware round-robin과 안전한 failover로 전달합니다.
    </p>
    <div class="cta-row">
      {#if summary}
        {@const action = stateCopy().action}
        {#if action.kind === "retry"}<button
            class="button"
            type="button"
            onclick={() => load(true)}
            disabled={refreshing}>{refreshing ? "확인 중" : action.label}</button
          >{:else}<a class="button" href={action.href}>{action.label}</a>{/if}
        {#if action.kind !== "link" || action.href !== "/models"}<a
            class="button secondary"
            href="/models">모델 proof 확인</a
          >{/if}
      {:else if loading}<a class="button secondary" href="/models"
          >모델 proof 확인</a
        >{/if}
    </div>
  </div>
  <article
    class:error-panel={error}
    class="panel status-card"
    aria-live="polite"
    aria-busy={loading}
  >
    {#if loading}<p class="eyebrow">GATEWAY STATUS</p>
      <h2>상태 확인 중</h2>
      <p class="muted">현재 snapshot을 불러오고 있습니다.</p>
    {:else if error && !summary}<p class="eyebrow">GATEWAY STATUS</p>
      <h2>{errorCopy().title}</h2>
      <p>{errorCopy().message}</p>
      <button class="button secondary" type="button" onclick={() => load()}
        >{errorCopy().action.label}</button
      >
    {:else}<p class="eyebrow">GATEWAY STATUS</p>
      <span
        class="status-pill"
        data-tone={summary?.state.traffic_ready ? "ok" : "warning"}
        >{stateCopy().title}</span
      >
      <h2>{summary?.capacity.eligible} / 2 slot</h2>
      <p>{stateCopy().message}</p>
      <small class="muted"
        >마지막 확인 {observed(summary?.snapshot.observed_at)}</small
      >{/if}
  </article>
</section>

{#if stale && summary}<div class="notice snapshot-notice" role="status">
    {#if error}새로 고침 실패: {error} · {/if}마지막 성공 snapshot을 표시합니다 · 마지막 성공 {observed(
      summary.snapshot.observed_at,
    )} ·
    <button type="button" onclick={() => load(true)} disabled={refreshing}
      >{refreshing ? "확인 중" : "다시 확인"}</button
    >
  </div>{/if}

{#if summary}<section class="section">
  <div class="grid metrics" aria-label="24시간 핵심 지표">
    <article class="panel">
      <span>요청</span><strong
        >{summary ? summary.metrics_24h.sample_count : "—"}</strong
      ><small
        >{summary.metrics_24h.sample_count > 0
          ? "최근 24시간"
          : "최근 기간에 요청 없음"}</small
      >
    </article>
    <article class="panel">
      <span>성공률</span><strong
        >{percent(summary?.metrics_24h.success_rate ?? null)}</strong
      ><small>5건 미만은 비공개</small>
    </article>
    <article class="panel">
      <span>p95 지연</span><strong
        >{milliseconds(summary?.metrics_24h.latency_p95_ms ?? null)}</strong
      ><small>aggregate bucket</small>
    </article>
    <article class="panel">
      <span>p95 첫 응답</span><strong
        >{milliseconds(summary?.metrics_24h.ttfb_p95_ms ?? null)}</strong
      ><small>stream 첫 frame 포함</small>
    </article>
    <article class="panel">
      <span>failover</span><strong
        >{percent(summary?.metrics_24h.failover_rate ?? null)}</strong
      ><small>downstream request 기준</small>
    </article>
  </div>
</section>

<section class="section endpoints">
  <div class="section-heading">
    <div>
      <p class="eyebrow">PROOF-AWARE SURFACE</p>
      <h2>확인된 기능만 명확하게</h2>
    </div>
    <a href="/models">전체 모델 매트릭스 →</a>
  </div>
  <div class="grid">
    {#each summary.endpoints as endpoint}
      <article class="panel endpoint-card">
        <span
          class="status-pill"
          data-tone={endpoint.state === "verified"
            ? "ok"
            : endpoint.state === "unavailable"
              ? "danger"
              : "warning"}>{endpoint.state}</span
        >
        <h3>{endpoint.kind}</h3>
        <p class="muted">
          {endpoint.state === "verified"
            ? "현재 eligible slot의 provider proof가 있습니다."
            : endpoint.state === "proof_required"
              ? "catalog에는 있지만 실제 provider proof가 필요합니다."
              : endpoint.state === "maintenance"
                ? "계획된 점검이 끝날 때까지 요청을 잠시 멈춥니다."
                : "현재 요청 가능한 proof와 capacity 조합이 없습니다."}
        </p>
      </article>
    {/each}
  </div>
</section>
{/if}

<section class="section quick">
  <div>
    <p class="eyebrow">OPENAI COMPATIBLE</p>
    <h2>기존 client에서 base URL만 변경</h2>
    <p class="lede">
      NVIDIA provider key가 아니라 이 게이트웨이가 발급한 downstream token을
      사용합니다.
    </p>
  </div>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
  <pre role="region" aria-label="curl 빠른 시작" tabindex="0"><code
      >curl https://nvidia-lb.dongwontuna.net/v1/models \
  -H "Authorization: Bearer $NVIDIA_LB_TOKEN"</code
    ></pre>
</section>

<style>
  .hero {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(290px, 390px);
    gap: clamp(30px, 7vw, 100px);
    align-items: end;
  }
  h1 span {
    color: #76b900;
  }
  .status-card h2 {
    margin: 24px 0 10px;
    font-size: 34px;
  }
  .status-card button {
    margin-top: 12px;
  }
  .snapshot-notice {
    margin-top: 20px;
  }
  .snapshot-notice button {
    min-height: 44px;
    border: 0;
    background: transparent;
    color: #b8e86f;
    text-decoration: underline;
  }
  .metrics article {
    grid-column: span 4;
    min-height: 150px;
  }
  .metrics span,
  .metrics small {
    display: block;
    color: #8f9d96;
    font-size: 11px;
  }
  .metrics strong {
    display: block;
    margin: 20px 0 8px;
    color: #b5e66c;
    font-size: clamp(25px, 4vw, 39px);
  }
  .section-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 20px;
  }
  .section-heading a {
    min-height: 44px;
    padding: 12px;
    color: #a9d66d;
  }
  .endpoint-card {
    grid-column: span 4;
  }
  .endpoint-card h3 {
    margin: 24px 0 10px;
    text-transform: capitalize;
    font-size: 22px;
  }
  .endpoint-card p {
    margin: 0;
    line-height: 1.6;
  }
  .quick {
    display: grid;
    grid-template-columns: 0.8fr 1.2fr;
    gap: 30px;
    align-items: center;
    border-top: 1px solid #29342f;
  }
  .quick pre {
    margin: 0;
  }
  @media (max-width: 800px) {
    .hero,
    .quick {
      grid-template-columns: 1fr;
    }
    .metrics article,
    .endpoint-card {
      grid-column: span 6;
  }
}
  @media (max-width: 520px) {
    .metrics article,
    .endpoint-card {
      grid-column: 1 / -1;
    }
    .section-heading {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
