<script lang="ts">
import { onMount } from "svelte";
import { ApiError, adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { AuditEvent, Page } from "$lib/types";

let items = $state<AuditEvent[]>([]),
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
    const value = await api<Page<AuditEvent>>(
      `/audit?limit=50${currentBefore ? `&before=${encodeURIComponent(currentBefore)}` : ""}`,
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
    error = adminErrorMessage(e, "감사 로그를 읽지 못했습니다.");
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
  eyebrow="GOVERNANCE"
  title="Audit log"
  description="성공과 실패를 resource, actor, request correlation으로 추적합니다."
/>{#if invalidCursor}<section class="page-empty" role="alert">
    <strong>저장된 감사 페이지를 열 수 없습니다</strong>
    <button type="button" onclick={() => showPage(null)}>최신 이벤트로 이동</button>
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
    <strong>이 페이지에는 남은 감사 이벤트가 없습니다</strong>
    <button type="button" onclick={() => showPage(null)}>최신 이벤트로 이동</button>
  </section>{/if}
<div class="events">
  {#each items as item (item.id)}<article>
      <div>
        <strong>{item.action}</strong><small
          >{displayTime(item.created_at)}</small
        >
      </div>
      <StatusBadge value={item.outcome} />
      <p>
        {item.actor_kind} · {item.resource_kind} · {item.resource_id ?? "—"}
      </p>
      <code>request {item.request_id ?? "—"}</code
      >{#if Object.keys(item.detail).length}<dl>
          {#each Object.entries(item.detail) as [key, value]}<div>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </div>{/each}
        </dl>{/if}
    </article>{/each}
</div>
{#if items.length > 0 && (currentBefore || cursor)}<nav class="pagination" aria-label="감사 이벤트 페이지">
    {#if currentBefore}<button type="button" onclick={() => showPage(null)}>최신 50개</button>{/if}
    {#if cursor}<button type="button" onclick={() => showPage(cursor)}>다음 50개</button>{/if}
  </nav>{/if}

<style>
  .events {
    display: grid;
    gap: 8px;
  }
  .events article {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px;
    padding: 16px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .events small {
    display: block;
    color: #aeb7b2;
  }
  .events p,
  .events code,
  .events dl {
    grid-column: 1/-1;
    margin: 0;
  }
  .events code {
    color: #9eaaa3;
  }
  .events dl {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .events dl div {
    padding: 6px 8px;
    background: #101415;
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
  .events dt {
    color: #8f9a94;
    font-size: 0.68rem;
  }
  .events dd {
    margin: 2px 0 0;
  }
</style>
