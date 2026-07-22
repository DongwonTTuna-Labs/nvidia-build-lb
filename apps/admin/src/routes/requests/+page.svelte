<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import {
  ApiError,
  adminErrorMessage,
  api,
  datetimeLocalValue,
  displayTime,
  startPolling,
} from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { ProxyRequest, RequestAggregate, RequestPage } from "$lib/types";

const filterKeys = ["request_id", "client_id", "profile", "outcome", "endpoint"] as const;
const endpointOptions = [
  "/v1/chat/completions",
  "/v1/embeddings",
  "/v1/images/generations",
  "/v1/videos/generations",
  "/v1/audio/speech",
  "/v1/audio/transcriptions",
  "/v1/nvidia/inference",
];
const profileOptions = [
  "z-ai/glm-5.2",
  "microsoft/phi-4-multimodal-instruct",
  "nvidia/vila",
  "nvidia/nvclip",
  "black-forest-labs/flux.1-kontext-dev",
  "stabilityai/stable-video-diffusion",
  "nvidia/magpie-tts-multilingual",
  "nvidia/parakeet-ctc-1.1b",
];

type Draft = {
  request_id: string;
  client_id: string;
  profile: string;
  outcome: string;
  endpoint: string;
  since: string;
  until: string;
};

const emptyAggregate = (): RequestAggregate => ({
  total: 0,
  succeeded: 0,
  failed: 0,
  cancelled: 0,
  active: 0,
  average_duration_ms: null,
});

let items = $state<ProxyRequest[]>([]);
let aggregate = $state<RequestAggregate>(emptyAggregate());
let cursor = $state<string | null>(null);
let currentBefore = $state("");
let loading = $state(true);
let error = $state("");
let invalidQuery = $state<"" | "cursor" | "filter">("");
let snapshotStale = $state(false);
let lastSuccessAt = $state<string | null>(null);
let requestEpoch = 0;
let draft = $state<Draft>({
  request_id: "",
  client_id: "",
  profile: "",
  outcome: "",
  endpoint: "",
  since: "",
  until: "",
});

const activeFilterCount = () => {
  const params = new URL(window.location.href).searchParams;
  return [...filterKeys, "since", "until"].filter((key) => params.has(key)).length;
};

function readUrl(): void {
  const params = new URL(window.location.href).searchParams;
  currentBefore = params.get("before") ?? "";
  draft = {
    request_id: params.get("request_id") ?? "",
    client_id: params.get("client_id") ?? "",
    profile: params.get("profile") ?? "",
    outcome: params.get("outcome") ?? "",
    endpoint: params.get("endpoint") ?? "",
    since: datetimeLocalValue(params.get("since")),
    until: datetimeLocalValue(params.get("until")),
  };
}

function apiPath(): string {
  const source = new URL(window.location.href).searchParams;
  const params = new URLSearchParams({ limit: "50" });
  for (const key of [...filterKeys, "since", "until", "before"] as const) {
    const value = source.get(key);
    if (value) params.set(key, value);
  }
  return `/requests?${params}`;
}

async function load(clear = false): Promise<void> {
  const epoch = ++requestEpoch;
  if (clear) {
    items = [];
    aggregate = emptyAggregate();
  }
  loading = clear || !items.length;
  if (!items.length) error = "";
  try {
    const value = await api<RequestPage>(apiPath());
    if (epoch !== requestEpoch) return;
    items = value.items;
    aggregate = value.aggregate;
    cursor = value.next_before ?? null;
    lastSuccessAt = value.snapshot.observed_at;
    snapshotStale = value.snapshot.stale;
    error = "";
    invalidQuery = "";
  } catch (caught) {
    if (epoch !== requestEpoch) return;
    error = adminErrorMessage(caught, "요청 evidence를 읽지 못했습니다.");
    invalidQuery =
      caught instanceof ApiError && caught.status === 422
        ? caught.code === "invalid_page_cursor"
          ? "cursor"
          : "filter"
        : "";
    snapshotStale = items.length > 0;
  } finally {
    if (epoch === requestEpoch) loading = false;
  }
}

function writeDraftToUrl(): void {
  const url = new URL(window.location.href);
  for (const key of filterKeys) {
    const value = draft[key].trim();
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  for (const key of ["since", "until"] as const) {
    const value = draft[key];
    if (value) url.searchParams.set(key, new Date(value).toISOString());
    else url.searchParams.delete(key);
  }
  url.searchParams.delete("before");
  window.history.pushState({}, "", url);
  readUrl();
}

function applyFilters(event: SubmitEvent): void {
  event.preventDefault();
  writeDraftToUrl();
  void load(true);
}

function clearFilters(): void {
  const url = new URL(window.location.href);
  for (const key of [...filterKeys, "since", "until", "before"] as const) {
    url.searchParams.delete(key);
  }
  window.history.pushState({}, "", url);
  readUrl();
  void load(true);
}

function nextPage(): void {
  if (!cursor) return;
  const url = new URL(window.location.href);
  url.searchParams.set("before", cursor);
  window.history.pushState({}, "", url);
  readUrl();
  void load(true);
  document.querySelector("h1")?.scrollIntoView({ block: "start" });
}

function latestPage(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("before");
  window.history.pushState({}, "", url);
  readUrl();
  void load(true);
}

onMount(() => {
  readUrl();
  const popstate = () => {
    readUrl();
    void load(true);
  };
  window.addEventListener("popstate", popstate);
  const stop = startPolling(() => load(false));
  return () => {
    stop();
    window.removeEventListener("popstate", popstate);
  };
});
</script>

<PageHeader
  eyebrow="OPERATIONS"
  title="Request evidence"
  description="필요한 요청만 좁힌 뒤 결과를 보고, attempt timeline으로 원인을 확인합니다."
/>

<form class="filters" aria-label="요청 검색" onsubmit={applyFilters}>
  <div class="filter-grid">
    <label>
      <span>Request ID</span>
      <input bind:value={draft.request_id} placeholder="UUID" autocomplete="off" spellcheck="false" />
    </label>
    <label>
      <span>Client ID</span>
      <input bind:value={draft.client_id} placeholder="UUID" autocomplete="off" spellcheck="false" />
    </label>
    <label>
      <span>Profile</span>
      <input
        bind:value={draft.profile}
        list="request-profiles"
        placeholder="모든 profile"
        autocomplete="off"
        spellcheck="false"
      />
    </label>
    <label>
      <span>결과</span>
      <select bind:value={draft.outcome}>
        <option value="">모든 결과</option>
        <option value="succeeded">성공</option>
        <option value="failed">실패</option>
        <option value="rejected">Provider 거절</option>
        <option value="cancelled">Client 취소</option>
        <option value="started">진행 중</option>
        <option value="abandoned_after_restart">재시작 후 종료</option>
      </select>
    </label>
    <label>
      <span>Endpoint</span>
      <select bind:value={draft.endpoint}>
        <option value="">모든 endpoint</option>
        {#each endpointOptions as endpoint (endpoint)}
          <option value={endpoint}>{endpoint}</option>
        {/each}
      </select>
    </label>
    <label>
      <span>시작 시각 이후</span>
      <input type="datetime-local" bind:value={draft.since} />
    </label>
    <label>
      <span>시작 시각 이전</span>
      <input type="datetime-local" bind:value={draft.until} />
    </label>
  </div>
  <datalist id="request-profiles">
    {#each profileOptions as profile (profile)}<option value={profile}></option>{/each}
  </datalist>
  <div class="filter-actions">
    <button class="primary" type="submit" disabled={loading}>검색</button>
    {#if activeFilterCount() > 0}
      <button type="button" onclick={clearFilters} disabled={loading}>필터 초기화</button>
      <span>{activeFilterCount()}개 조건 적용 중</span>
    {/if}
  </div>
</form>

{#if invalidQuery}
  <section class="query-state" role="alert">
    <strong>{invalidQuery === "cursor" ? "저장된 페이지를 열 수 없습니다" : "검색 조건을 확인해 주세요"}</strong>
    <span>{error}</span>
    <div>
      {#if invalidQuery === "cursor"}
        <button type="button" onclick={latestPage}>최신 요청으로 이동</button>
      {:else}
        <button type="button" onclick={clearFilters}>검색 조건 초기화</button>
      {/if}
    </div>
  </section>
{:else}
  <DataState
    {loading}
    {error}
    hasData={items.length > 0}
    stale={snapshotStale}
    {lastSuccessAt}
    empty={!loading && !error && !items.length && activeFilterCount() === 0 && !currentBefore}
    retry={() => load()}
  />
  {#if !loading && !error && !items.length && currentBefore}
    <section class="query-state">
      <strong>이 페이지에는 남은 요청이 없습니다</strong>
      <span>보존 기간이 지난 결과일 수 있습니다. 최신 요청으로 돌아가세요.</span>
      <div><button type="button" onclick={latestPage}>최신 요청으로 이동</button></div>
    </section>
  {:else if !loading && !error && !items.length && activeFilterCount() > 0}
    <section class="query-state">
      <strong>조건에 맞는 요청이 없습니다</strong>
      <span>검색 범위나 식별자를 바꾸거나 필터를 초기화하세요.</span>
      <div><button type="button" onclick={clearFilters}>필터 초기화</button></div>
    </section>
  {/if}
{/if}

{#if !loading && !error}
  <section class="aggregate" aria-label="검색 결과 요약">
    <article><span>전체</span><strong>{aggregate.total}</strong></article>
    <article><span>성공</span><strong>{aggregate.succeeded}</strong></article>
    <article><span>실패·거절·재시작 종료</span><strong>{aggregate.failed}</strong></article>
    <article><span>Client 취소</span><strong>{aggregate.cancelled}</strong></article>
    <article><span>진행 중</span><strong>{aggregate.active}</strong></article>
    <article
      ><span>평균 처리 시간</span><strong
        >{aggregate.average_duration_ms === null ? "—" : `${aggregate.average_duration_ms}ms`}</strong
      ></article
    >
  </section>
{/if}

{#if items.length > 0}
  <div class="list">
    {#each items as item (item.request_id)}
      <a href={`${base}/requests/${item.request_id}`}>
        <div class="identity">
          <strong>{item.profile_id}</strong>
          <code>{item.endpoint}</code>
        </div>
        <StatusBadge value={item.outcome} />
        <span>{item.modality} · {item.stream ? "stream" : "non-stream"}</span>
        <span
          >client {item.client_id ?? "anonymous"} · HTTP {item.status_code ?? "—"} ·
          {item.duration_ms ?? "—"}ms · failover {item.failover_count}</span
        >
        <time datetime={item.started_at}>{displayTime(item.started_at)}</time>
      </a>
    {/each}
  </div>
  <nav class="pagination" aria-label="요청 결과 페이지">
    {#if currentBefore}
      <button type="button" onclick={latestPage} disabled={loading}>최신 50개</button>
    {/if}
    {#if cursor}
      <button type="button" onclick={nextPage} disabled={loading}>다음 50개</button>
    {/if}
  </nav>
{/if}

<style>
  .filters {
    display: grid;
    gap: 14px;
    margin-bottom: 18px;
    padding: 16px;
    border: 1px solid #46504b;
    border-radius: 8px;
    background: #171b1c;
  }
  .query-state {
    display: grid;
    gap: 8px;
    margin-bottom: 18px;
    padding: 18px;
    border: 1px solid #d99b48;
    border-radius: 8px;
    background: #171b1c;
  }
  .query-state strong {
    color: #ffd08a;
  }
  .query-state span {
    color: #aeb7b2;
  }
  .filter-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
  }
  label {
    display: grid;
    gap: 6px;
    min-width: 0;
  }
  label span,
  .filter-actions span {
    color: #aab4af;
    font-size: 0.75rem;
  }
  input,
  select {
    width: 100%;
    min-height: 44px;
    padding: 9px 10px;
    border: 1px solid #59635e;
    border-radius: 6px;
    background: #0f1314;
    color: #edf2ef;
  }
  .filter-actions,
  .pagination {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  button {
    min-height: 44px;
  }
  button.primary {
    border-color: #76b900;
    background: #76b900;
    color: #0c100a;
    font-weight: 750;
  }
  .aggregate {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 8px;
    margin: 0 0 18px;
  }
  .aggregate article {
    display: grid;
    gap: 5px;
    padding: 14px;
    border: 1px solid #46504b;
    border-radius: 7px;
    background: #171b1c;
  }
  .aggregate span {
    color: #aab4af;
    font-size: 0.72rem;
  }
  .aggregate strong {
    font-size: 1.2rem;
  }
  .list {
    display: grid;
    gap: 7px;
  }
  .list > a {
    display: grid;
    grid-template-columns: minmax(180px, 1.4fr) auto minmax(120px, 0.8fr) minmax(260px, 1.4fr) auto;
    gap: 12px;
    align-items: center;
    padding: 15px;
    border: 1px solid #46504b;
    border-radius: 7px;
    background: #171b1c;
    color: #e9efec;
    text-decoration: none;
  }
  .list > a:hover,
  .list > a:focus-visible {
    border-color: #76b900;
  }
  .identity {
    min-width: 0;
  }
  .identity strong,
  .identity code {
    display: block;
    overflow-wrap: anywhere;
  }
  .list code,
  .list span,
  .list time {
    color: #aab4af;
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }
  .list time {
    text-align: right;
  }
  .pagination {
    justify-content: center;
    padding: 22px;
  }
  @media (max-width: 1050px) {
    .filter-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .aggregate {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .list > a {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .list span,
    .list time {
      grid-column: 1/-1;
      text-align: left;
    }
  }
  @media (max-width: 520px) {
    .filter-grid,
    .aggregate {
      grid-template-columns: 1fr;
    }
    .filter-actions button,
    .pagination button {
      flex: 1 1 auto;
    }
  }
</style>
