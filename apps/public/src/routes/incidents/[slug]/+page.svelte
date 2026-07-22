<script lang="ts">
import { onMount } from "svelte";
import { page } from "$app/state";
import { PublicApiError, publicApi, startPolling } from "$lib/api";
import { observed } from "$lib/format";
import type { PublicIncidentDetail } from "$lib/types";

let data = $state<PublicIncidentDetail | null>(null);
let loading = $state(true);
let refreshing = $state(false);
let stale = $state(false);
let error = $state("");
let errorStatus = $state(0);
let terminalNotFound = $state(false);
let activeSlug = "";
let routeEpoch = 0;
function resetForRoute(slug: string): number {
  activeSlug = slug;
  routeEpoch += 1;
  data = null;
  loading = true;
  refreshing = false;
  stale = false;
  error = "";
  errorStatus = 0;
  terminalNotFound = false;
  return routeEpoch;
}
async function load(background = false, slug = activeSlug, epoch = routeEpoch): Promise<void> {
  if (!slug || epoch !== routeEpoch || slug !== activeSlug) return;
  if (background || data) refreshing = true;
  else loading = true;
  error = "";
  errorStatus = 0;
  try {
    const value = await publicApi.incident(slug);
    if (epoch !== routeEpoch || slug !== activeSlug) return;
    data = value;
    terminalNotFound = false;
    stale = data.snapshot.stale;
  } catch (caught) {
    if (epoch !== routeEpoch || slug !== activeSlug) return;
    error = caught instanceof Error ? caught.message : "incident를 불러오지 못했습니다.";
    errorStatus = caught instanceof PublicApiError ? caught.status : 0;
    if (errorStatus === 404) {
      data = null;
      stale = false;
      terminalNotFound = true;
    } else {
      stale = data !== null;
    }
  } finally {
    if (epoch === routeEpoch && slug === activeSlug) {
      loading = false;
      refreshing = false;
    }
  }
}
$effect(() => {
  const slug = page.params.slug;
  if (!slug || slug === activeSlug) return;
  const epoch = resetForRoute(slug);
  void load(false, slug, epoch);
});
onMount(() => {
  const stop = startPolling(
    () => (terminalNotFound ? Promise.resolve() : load(data !== null)),
    30_000,
    false,
  );
  return () => {
    routeEpoch += 1;
    stop();
  };
});
</script>

<svelte:head
  ><title>{data?.item.title ?? "Incident"} · NVIDIA Build LB</title
  ></svelte:head
>
<header class="page-head">
  <a class="back" href="/incidents">← 장애 이력</a>{#if data}<p class="eyebrow">
      {data.item.severity.toUpperCase()} INCIDENT
    </p>
    <h1>{data.item.title}</h1>
    <div class="meta">
      <span
        class="status-pill"
        data-tone={data.item.status === "resolved" ? "ok" : "warning"}
        >{data.item.status}</span
      ><span>시작 {observed(data.item.started_at)}</span>
    </div>{:else}<p class="eyebrow">INCIDENT DETAIL</p>
    <h1>장애 기록</h1>{/if}
</header>
<section class="section">
  {#if stale && data}<div class="notice" role="status">
      {#if error}새로 고침 실패: {error} · {/if}마지막 정상 snapshot {observed(
        data.snapshot.observed_at,
      )} ·
      <button type="button" onclick={() => load(true)} disabled={refreshing}
        >{refreshing ? "확인 중" : "다시 확인"}</button
      >
    </div>{/if}{#if loading}<div class="panel" role="status" aria-busy="true">
      <h2>incident 확인 중</h2>
    </div>{:else if error && !data}<div class="panel error-panel" role="alert">
      <h2>
        {errorStatus === 404
          ? "incident를 찾을 수 없습니다"
          : "incident를 불러오지 못했습니다"}
      </h2>
      <p>{error}</p>
      <div class="cta-row">
        {#if errorStatus !== 404}<button
            class="button secondary"
            type="button"
            onclick={() => load()}>다시 시도</button
          >{/if}<a class="button secondary" href="/incidents">목록으로</a>
      </div>
    </div>{:else if data}<ol class="timeline">
      {#each data.item.updates as update}<li>
          <span aria-hidden="true"></span>
          <article class="panel">
            <div>
              <strong>{update.status}</strong><time
                >{observed(update.published_at)}</time
              >
            </div>
            <p>{update.message}</p>
          </article>
        </li>{/each}
    </ol>
    {#if !data.item.updates.length}<div class="panel empty-panel">
        <h2>아직 공개 update가 없습니다</h2>
      </div>{/if}{/if}
</section>

<style>
  .notice {
    margin-bottom: 20px;
  }
  .notice button {
    min-height: 44px;
    border: 0;
    background: transparent;
    color: #b8e86f;
    text-decoration: underline;
  }
  .back {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #a9d66d;
  }
  .meta {
    display: flex;
    align-items: center;
    gap: 14px;
    color: #89978f;
  }
  .timeline {
    max-width: 850px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .timeline li {
    display: grid;
    grid-template-columns: 18px 1fr;
    gap: 16px;
  }
  .timeline li > span {
    position: relative;
    width: 11px;
    height: 11px;
    margin-top: 28px;
    border-radius: 50%;
    background: #76b900;
  }
  .timeline li > span::after {
    content: "";
    position: absolute;
    top: 11px;
    bottom: -100px;
    left: 5px;
    width: 1px;
    background: #35423c;
  }
  .timeline li:last-child > span::after {
    display: none;
  }
  .timeline article {
    margin-bottom: 16px;
  }
  .timeline article div {
    display: flex;
    justify-content: space-between;
    gap: 20px;
  }
  .timeline time {
    color: #89968f;
    font-size: 12px;
  }
  .timeline p {
    margin: 15px 0 0;
    color: #b1bdb7;
    line-height: 1.7;
  }
</style>
