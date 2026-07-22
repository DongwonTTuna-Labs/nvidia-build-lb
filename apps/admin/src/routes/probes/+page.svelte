<script lang="ts">
import { onMount } from "svelte";
import { ApiError, adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { Page, ProbeRun } from "$lib/types";

let items = $state<ProbeRun[]>([]),
  cursor = $state<string | null>(null),
  currentBefore = $state(""),
  loading = $state(true),
  error = $state(""),
  invalidCursor = $state(false),
  snapshotStale = $state(false),
  lastSuccessAt = $state<string | null>(null);
let requestEpoch = 0;
function readCursor(): void {
  currentBefore = new URL(window.location.href).searchParams.get("before") ?? "";
}
async function load(clear = false) {
  const epoch = ++requestEpoch;
  if (clear) items = [];
  loading = clear || !items.length;
  if (!items.length) error = "";
  try {
    const value = await api<Page<ProbeRun>>(
      `/probes?limit=50${currentBefore ? `&before=${encodeURIComponent(currentBefore)}` : ""}`,
    );
    if (epoch !== requestEpoch) return;
    items = value.items;
    cursor = value.next_before ?? null;
    lastSuccessAt = value.snapshot.observed_at;
    snapshotStale = value.snapshot.stale;
    error = "";
    invalidCursor = false;
  } catch (e) {
    if (epoch !== requestEpoch) return;
    error = adminErrorMessage(e, "Provider probe 목록을 읽지 못했습니다.");
    invalidCursor = e instanceof ApiError && e.code === "invalid_page_cursor";
  } finally {
    if (epoch === requestEpoch) loading = false;
  }
}
function showPage(before: string | null): void {
  const url = new URL(window.location.href);
  if (before) url.searchParams.set("before", before);
  else url.searchParams.delete("before");
  window.history.pushState({}, "", url);
  readCursor();
  void load(true);
}
onMount(() => {
  readCursor();
  const popstate = () => {
    readCursor();
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
  eyebrow="CAPACITY"
  title="Provider probes"
  description="어떤 upstream/profile을 실제 호출했고 무엇이 실패했는지 확인합니다."
/>{#if invalidCursor}<section class="page-empty" role="alert">
    <strong>저장된 probe 페이지를 열 수 없습니다</strong>
    <button type="button" onclick={() => showPage(null)}>최신 probe로 이동</button>
  </section>{:else}<DataState
  {loading}
  {error}
  hasData={items.length > 0}
  stale={snapshotStale}
  {lastSuccessAt}
  empty={!loading && !error && !items.length && !currentBefore}
  retry={() => load()}
/>{/if}
{#if !loading && !error && !items.length && currentBefore}<section class="page-empty">
    <strong>이 페이지에는 남은 probe가 없습니다</strong>
    <button type="button" onclick={() => showPage(null)}>최신 probe로 이동</button>
  </section>{/if}
{#if items.length > 0}<!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable data region) -->
<div class="table" role="region" aria-label="Provider probe 목록" tabindex="0">
  <table>
    <thead
      ><tr
        ><th>상태</th><th>종류 / profile</th><th>Upstream</th><th
          >HTTP / 오류</th
        ><th>비용</th><th>완료</th></tr
      ></thead
    ><tbody
      >{#each items as item (item.id)}<tr
          ><td><StatusBadge value={item.status} /></td><td
            ><strong>{item.kind}</strong><small
              >{item.profile_id ?? "credential"}</small
            ></td
          ><td><code>{item.upstream_id ?? "—"}</code></td><td
            >{item.status_code ?? "—"} / {item.error_class ?? "없음"}</td
          ><td>{item.billable ? "billable" : "무료"}</td><td
            >{displayTime(item.finished_at)}</td
          ></tr
        >{/each}</tbody
    >
  </table>
</div>
{#if items.length > 0 && (currentBefore || cursor)}<nav class="pagination" aria-label="Provider probe 페이지">
    {#if currentBefore}<button type="button" onclick={() => showPage(null)}>최신 50개</button>{/if}
    {#if cursor}<button type="button" onclick={() => showPage(cursor)}>다음 50개</button>{/if}
  </nav>{/if}
{/if}

<style>
  .table {
    overflow: auto;
    border: 1px solid #4c5650;
    border-radius: 8px;
  }
  .page-empty {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 18px;
    border: 1px solid #d99b48;
    border-radius: 8px;
    background: #171b1c;
  }
  .pagination {
    display: flex;
    justify-content: center;
    gap: 10px;
    padding: 22px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: #171b1c;
  }
  th,
  td {
    padding: 13px;
    text-align: left;
    border-bottom: 1px solid #3e4742;
    white-space: nowrap;
  }
  th {
    color: #8f9a94;
    font-size: 0.7rem;
    text-transform: uppercase;
  }
  td small {
    display: block;
    color: #aeb7b2;
  }
  td code {
    font-size: 0.72rem;
  }
</style>
