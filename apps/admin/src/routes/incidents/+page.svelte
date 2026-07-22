<script lang="ts">
import { onMount } from "svelte";
import { adminErrorMessage, api, displayTime, loadCursorWindow, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import PaginationButton from "$lib/components/PaginationButton.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { Incident, Page } from "$lib/types";

type Draft = {
  title: string;
  status: string;
  severity: string;
  public: boolean;
};
let items = $state<Incident[]>([]),
  cursor = $state<string | null>(null),
  loading = $state(true),
  more = $state(false);
let loadError = $state(""),
  actionError = $state(""),
  actionErrorTarget = $state(""),
  notice = $state(""),
  busy = $state(""),
  snapshotStale = $state(false),
  lastSuccessAt = $state<string | null>(null);
let slug = $state(""),
  title = $state(""),
  severity = $state("minor"),
  isPublic = $state(false),
  message = $state("");
let updates = $state<Record<string, string>>({}),
  drafts = $state<Record<string, Draft>>({});

function draftFor(item: Incident): Draft {
  return {
    title: item.title,
    status: item.status,
    severity: item.severity,
    public: item.public,
  };
}

function draftChanged(item: Incident, draft = drafts[item.id]): boolean {
  return Boolean(
    draft &&
      (draft.title !== item.title ||
        draft.status !== item.status ||
        draft.severity !== item.severity ||
        draft.public !== item.public),
  );
}

function resetDraft(item: Incident): void {
  drafts[item.id] = draftFor(item);
  updates[item.id] = "";
}

async function load(append = false): Promise<void> {
  append ? (more = true) : (loading = items.length === 0);
  if (!items.length) loadError = "";
  try {
    const previous = new Map(items.map((item) => [item.id, item]));
    const value = append
      ? await api<Page<Incident>>(
          `/incidents?limit=50${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`,
        )
      : await loadCursorWindow<Incident>("/incidents", items.length, (item) => item.id);
    if (append) {
      items = [...items, ...value.items];
      cursor = value.next_before ?? null;
    } else items = value.items;
    if (!append) cursor = value.next_before ?? null;
    lastSuccessAt = value.snapshot.observed_at;
    snapshotStale = value.snapshot.stale;
    loadError = "";
    for (const item of value.items) {
      const old = previous.get(item.id);
      const editing = Boolean(
        old && (draftChanged(old) || (updates[item.id] ?? "").trim().length > 0),
      );
      if (!editing) drafts[item.id] = draftFor(item);
    }
  } catch (error) {
    loadError = adminErrorMessage(error, "incident 목록을 읽지 못했습니다.");
  } finally {
    loading = false;
    more = false;
  }
}

async function create(): Promise<void> {
  busy = "create";
  actionError = "";
  actionErrorTarget = "";
  notice = "";
  try {
    await api("/incidents", {
      method: "POST",
      body: JSON.stringify({
        slug,
        title,
        status: "investigating",
        severity,
        public: isPublic,
        public_message: message,
      }),
    });
    slug = "";
    title = "";
    message = "";
    isPublic = false;
    notice = "Incident를 생성했습니다.";
    await load();
  } catch (error) {
    actionErrorTarget = "create";
    actionError = adminErrorMessage(error, "Incident를 생성하지 못했습니다.");
  } finally {
    busy = "";
  }
}

async function save(item: Incident): Promise<void> {
  const draft = drafts[item.id];
  if (!draft) return;
  busy = `${item.id}:save`;
  actionError = "";
  actionErrorTarget = "";
  notice = "";
  try {
    const patch: Partial<Draft> = {};
    if (draft.title !== item.title) patch.title = draft.title;
    if (draft.status !== item.status) patch.status = draft.status;
    if (draft.severity !== item.severity) patch.severity = draft.severity;
    if (draft.public !== item.public) patch.public = draft.public;
    if (!Object.keys(patch).length) return;
    await api(`/incidents/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    notice = `${item.slug} 메타데이터를 저장했습니다.`;
    await load();
  } catch (error) {
    actionErrorTarget = item.id;
    actionError = adminErrorMessage(error, "Incident를 수정하지 못했습니다.");
  } finally {
    busy = "";
  }
}

async function publish(item: Incident): Promise<void> {
  const publicMessage = (updates[item.id] ?? "").trim();
  if (!publicMessage) {
    notice = "";
    actionError = "공개 update 메시지를 입력하세요.";
    actionErrorTarget = item.id;
    return;
  }
  busy = `${item.id}:update`;
  actionError = "";
  actionErrorTarget = "";
  notice = "";
  try {
    await api(`/incidents/${item.id}/updates`, {
      method: "POST",
      body: JSON.stringify({
        status: drafts[item.id]?.status ?? item.status,
        public_message: publicMessage,
      }),
    });
    updates[item.id] = "";
    notice = `${item.slug} update를 발행했습니다.`;
    await load();
  } catch (error) {
    actionErrorTarget = item.id;
    actionError = adminErrorMessage(error, "Update를 발행하지 못했습니다.");
  } finally {
    busy = "";
  }
}

onMount(() => startPolling(() => (busy ? Promise.resolve() : load())));
</script>

<PageHeader
  eyebrow="OPERATIONS"
  title="Incidents"
  description="내부 상태와 public 메시지를 분리해 생성, 공개 전환, 갱신, 해결합니다."
/>
<DataState
  {loading}
  error={loadError}
  hasData={items.length > 0}
  stale={snapshotStale}
  {lastSuccessAt}
  empty={!loading && !loadError && !items.length}
  retry={() => load()}
/>
{#if actionError && actionErrorTarget === "create"}<p class="action-error" role="alert"
    >{actionError}</p
  >{/if}
{#if notice}<p class="notice" role="status">{notice}</p>{/if}

{#if lastSuccessAt}
  <section class="create">
    <h2>Incident 시작</h2>
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void create();
      }}
      aria-busy={busy === "create"}
    >
      <div class="row">
        <label
          >slug<input
            bind:value={slug}
            pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
            required
            disabled={busy !== ""}
          /></label
        ><label
          >제목<input
            bind:value={title}
            required
            disabled={busy !== ""}
          /></label
        ><label
          >심각도<select bind:value={severity} disabled={busy !== ""}
            ><option>minor</option><option>major</option><option
              >critical</option
            ></select
          ></label
        >
      </div>
      <label
        >첫 public 메시지<textarea
          bind:value={message}
          required
          maxlength="2000"
          disabled={busy !== ""}
        ></textarea></label
      >
      <label class="check"
        ><input
          type="checkbox"
          bind:checked={isPublic}
          disabled={busy !== ""}
        /> 즉시 public status에 게시</label
      >
      <button class="primary" disabled={busy !== ""}
        >{busy === "create" ? "생성 중…" : "Incident 시작"}</button
      >
    </form>
  </section>

  <div class="incidents">
    {#each items as item (item.id)}
      <article>
        <div class="heading">
          <div>
            <h2>{item.title}</h2>
            <code>{item.slug}</code>
          </div>
          <StatusBadge value={item.status} />
        </div>
        <p>
          시작 {displayTime(item.started_at)} · 공개 {item.public
            ? "예"
            : "아니오"} · {item.severity}
        </p>
        {#if actionError && actionErrorTarget === item.id}<p class="action-error" role="alert"
            >{actionError}</p
          >{/if}
        {#if drafts[item.id]}
          <div class="metadata">
            <label
              >제목<input
                bind:value={drafts[item.id].title}
                disabled={busy !== ""}
              /></label
            >
            <label
              >상태<select
                bind:value={drafts[item.id].status}
                disabled={busy !== ""}
                ><option>investigating</option><option>identified</option
                ><option>monitoring</option><option>resolved</option></select
              ></label
            >
            <label
              >심각도<select
                bind:value={drafts[item.id].severity}
                disabled={busy !== ""}
                ><option>minor</option><option>major</option><option
                  >critical</option
                ></select
              ></label
            >
            <label class="check"
              ><input
                type="checkbox"
                bind:checked={drafts[item.id].public}
                disabled={busy !== ""}
              /> Public status에 게시</label
            >
          </div>
          <div class="actions">
            <button
              onclick={() => resetDraft(item)}
              disabled={busy !== "" ||
                (!draftChanged(item) && !(updates[item.id] ?? "").trim())}
              >되돌리기</button
            ><button
              onclick={() => void save(item)}
              disabled={busy !== "" || !draftChanged(item)}
              >{busy === `${item.id}:save` ? "저장 중…" : "변경 저장"}</button
            >
          </div>
        {/if}
        <label
          >새 public update<textarea
            bind:value={updates[item.id]}
            maxlength="2000"
            placeholder="사용자에게 필요한 사실과 다음 갱신 시점만"
            disabled={busy !== ""}
          ></textarea></label
        >
        <button
          class="primary"
          onclick={() => void publish(item)}
          disabled={busy !== ""}
          >{busy === `${item.id}:update` ? "발행 중…" : "Update 발행"}</button
        >
        {#if item.updates?.length}<details>
            <summary>공개 update {item.updates.length}개</summary
            >{#each item.updates as update}<blockquote>
                <strong>{update.status}</strong>
                <p>{update.public_message}</p>
                <small>{displayTime(update.published_at)}</small>
              </blockquote>{/each}
          </details>{/if}
      </article>
    {/each}
  </div>
  <PaginationButton {cursor} busy={more} loadMore={() => void load(true)} />
{/if}

<style>
  summary {
    display: flex;
    align-items: center;
    min-height: 44px;
    cursor: pointer;
  }
  .create,
  .incidents article {
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .create {
    margin-bottom: 12px;
  }
  .create form,
  .incidents article {
    display: grid;
    gap: 12px;
  }
  .row,
  .metadata {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: end;
    gap: 8px;
  }
  .metadata {
    grid-template-columns: 2fr 1fr 1fr 1.2fr;
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
  }
  .check input {
    min-height: auto;
  }
  textarea {
    min-height: 80px;
  }
  .incidents {
    display: grid;
    gap: 10px;
  }
  .heading {
    display: flex;
    justify-content: space-between;
    gap: 10px;
  }
  .heading h2 {
    margin: 0;
  }
  .heading code,
  .incidents article > p,
  small {
    color: #aab4ae;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .notice,
  .action-error {
    padding: 12px;
    border: 1px solid #76b900;
  }
  .action-error {
    border-color: #d65e5e;
    color: #ffb0b0;
  }
  blockquote {
    margin: 10px 0;
    padding: 12px;
    border-left: 3px solid #76b900;
    background: #101415;
  }
  @media (max-width: 850px) {
    .metadata {
      grid-template-columns: 1fr 1fr;
    }
  }
  @media (max-width: 600px) {
    .row,
    .metadata {
      grid-template-columns: 1fr;
    }
    .heading {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
