<script lang="ts">
import { onDestroy, onMount } from "svelte";
import { base } from "$app/paths";
import {
  ApiError,
  adminErrorMessage,
  api,
  copyText,
  displayTime,
  loadCursorWindow,
  startPolling,
} from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import PaginationButton from "$lib/components/PaginationButton.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import { staleClientActionMessage, staleClientActionRefreshFailedMessage } from "$lib/operations";
import type { Client, Page } from "$lib/types";

const allScopes = [
  "models:read",
  "chat:write",
  "embeddings:write",
  "images:write",
  "media:write",
  "audio:write",
];
let items = $state<Client[]>([]),
  cursor = $state<string | null>(null),
  more = $state(false),
  loading = $state(true),
  queryError = $state(""),
  actionError = $state(""),
  notice = $state(""),
  busy = $state(""),
  label = $state(""),
  scopes = $state(["models:read", "chat:write"]),
  rpm = $state<number | undefined>(),
  concurrency = $state<number | undefined>(),
  daily = $state<number | undefined>(),
  expires = $state(""),
  allowlist = $state(""),
  secret = $state(""),
  copied = $state(false),
  copyError = $state(""),
  lastSuccessAt = $state<string | null>(null),
  snapshotStale = $state(false),
  dialog = $state<HTMLDialogElement>(),
  copyButton = $state<HTMLButtonElement>(),
  restoreFocus = $state<HTMLElement | null>(null);
let copyEpoch = 0;
async function load(append = false) {
  append ? (more = true) : (loading = !items.length);
  if (!items.length) queryError = "";
  try {
    const value = append
      ? await api<Page<Client>>(
          `/clients?limit=50${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`,
        )
      : await loadCursorWindow<Client>("/clients", items.length, (item) => item.id);
    if (append) {
      items = [...items, ...value.items];
      cursor = value.next_before ?? null;
    } else items = value.items;
    if (!append) cursor = value.next_before ?? null;
    lastSuccessAt = value.snapshot.observed_at;
    snapshotStale = value.snapshot.stale;
    queryError = "";
  } catch (e) {
    queryError = adminErrorMessage(e, "Client 목록을 읽지 못했습니다.");
  } finally {
    loading = false;
    more = false;
  }
}
function toggleScope(scope: string) {
  scopes = scopes.includes(scope) ? scopes.filter((value) => value !== scope) : [...scopes, scope];
}
function showSecret(value: string, focusTarget: HTMLElement | null): void {
  copyEpoch += 1;
  restoreFocus = focusTarget;
  secret = value;
  copied = false;
  copyError = "";
  dialog?.showModal();
  requestAnimationFrame(() => copyButton?.focus());
}
async function create() {
  if (!scopes.length) {
    notice = "";
    actionError = "scope를 하나 이상 선택하세요.";
    return;
  }
  const focusTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  busy = "create";
  actionError = "";
  notice = "";
  try {
    const value = await api<{ token: string }>("/clients", {
      method: "POST",
      body: JSON.stringify({
        label,
        scopes,
        expires_at: expires ? new Date(expires).toISOString() : null,
        model_allowlist: allowlist.trim()
          ? allowlist
              .split(",")
              .map((v) => v.trim())
              .filter(Boolean)
          : null,
        rpm_limit: rpm ?? null,
        max_concurrency: concurrency ?? null,
        request_limit_day: daily ?? null,
      }),
    });
    showSecret(value.token, focusTarget);
    label = "";
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Client 발급에 실패했습니다.");
  } finally {
    busy = "";
  }
}
async function action(item: Client, name: "rotate" | "revoke") {
  const prompt =
    name === "rotate"
      ? `${item.label}의 기존 token은 즉시 무효화됩니다. 교체할까요?`
      : `${item.label} credential을 폐기할까요?`;
  if (!confirm(prompt)) return;
  const focusTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  busy = `${item.id}:${name}`;
  actionError = "";
  notice = "";
  const targetLabel = item.label;
  try {
    const value = await api<{ token?: string }>(`/clients/${item.id}/${name}`, { method: "POST" });
    if (value.token) {
      showSecret(value.token, focusTarget);
    }
    notice = `${targetLabel}: ${name} 완료`;
    await load();
  } catch (e) {
    if (name === "rotate" && e instanceof ApiError && e.status === 404) {
      await load();
      if (queryError) {
        queryError = staleClientActionRefreshFailedMessage(targetLabel, queryError);
      } else {
        notice = staleClientActionMessage(targetLabel);
      }
    } else {
      actionError = adminErrorMessage(e, "Client 작업에 실패했습니다.");
    }
  } finally {
    busy = "";
  }
}
async function copy() {
  const epoch = ++copyEpoch;
  copied = false;
  copyError = "";
  const success = await copyText(secret);
  if (epoch !== copyEpoch) return;
  copied = success;
  if (!success) copyError = "복사하지 못했습니다. token을 직접 선택해 복사하세요.";
}
async function close() {
  if (copied && !(await copyText(""))) {
    copyError = "클립보드를 비우지 못했습니다. 다른 값을 복사한 뒤 다시 닫으세요.";
    return;
  }
  copyEpoch += 1;
  secret = "";
  copied = false;
  copyError = "";
  dialog?.close();
}
function afterClose(): void {
  copyEpoch += 1;
  secret = "";
  copied = false;
  copyError = "";
  requestAnimationFrame(() => restoreFocus?.focus());
}
onMount(() => startPolling(() => (busy ? Promise.resolve() : load(false))));
onDestroy(() => {
  copyEpoch += 1;
});
</script>

<PageHeader
  eyebrow="ACCESS"
  title="Downstream clients"
  description="사용 주체별 최소 scope와 소비 제한을 한 번에 발급합니다."
/>
<DataState
  {loading}
  error={queryError}
  hasData={items.length > 0}
  stale={snapshotStale}
  {lastSuccessAt}
  empty={!loading && !queryError && !items.length}
  retry={() => load()}
/>
{#if actionError}<p class="error" role="alert">{actionError}</p>{/if}
{#if notice}<p class="notice" role="status">{notice}</p>{/if}
{#if lastSuccessAt}
  <section class="form">
    <h2>새 client 발급</h2>
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void create();
      }}
      aria-busy={busy === "create"}
    >
      <label>이름<input bind:value={label} required maxlength="128" /></label>
      <fieldset>
        <legend>허용 scope</legend>{#each allScopes as scope}<label
            class="check"
            ><input
              type="checkbox"
              checked={scopes.includes(scope)}
              onchange={() => toggleScope(scope)}
            />{scope}</label
          >{/each}
      </fieldset>
      <div class="limits">
        <label>분당 요청<input type="number" min="1" bind:value={rpm} /></label
        ><label
          >동시 요청<input
            type="number"
            min="1"
            max="64"
            bind:value={concurrency}
          /></label
        ><label
          >일일 요청<input type="number" min="1" bind:value={daily} /></label
        ><label
          >만료 시각<input type="datetime-local" bind:value={expires} /></label
        >
      </div>
      <label
        >모델 allowlist · 쉼표 구분<input
          bind:value={allowlist}
          placeholder="비워 두면 모든 모델"
        /></label
      >
      <button class="primary" disabled={busy !== ""}
        >{busy === "create" ? "발급 중…" : "한 번만 token 표시"}</button
      >
    </form>
  </section>
  <div class="list">
    {#each items as item (item.id)}
      <article>
        <div>
          <h2><a href={`${base}/clients/${item.id}`}>{item.label}</a></h2>
          <span>{item.prefix} · 생성 {displayTime(item.created_at)}</span>
        </div>
        <StatusBadge value={item.active ? "active" : "revoked"} />
        <p>{item.scopes.join(" · ")}</p>
        <small
          >RPM {item.rpm_limit ?? "무제한"} · 동시 {item.max_concurrency ??
            "무제한"} · 일일 {item.request_limit_day ?? "무제한"} · 누적 {item.request_count}</small
        >
        <div class="actions">
          <button
            onclick={() => void action(item, "rotate")}
            disabled={!item.active || busy !== ""}>교체</button
          ><button
            class="danger"
            onclick={() => void action(item, "revoke")}
            disabled={!item.active || busy !== ""}>폐기</button
          >
        </div>
      </article>
    {/each}
  </div>
  <PaginationButton {cursor} busy={more} loadMore={() => void load(true)} />
  <dialog
    bind:this={dialog}
    onclose={afterClose}
    oncancel={(event) => {
      event.preventDefault();
      void close();
    }}
    aria-labelledby="secret-dialog-title"
  >
    <h2 id="secret-dialog-title">지금만 보이는 token</h2>
    <p>안전한 secret manager에 복사한 뒤 닫으세요. 다시 조회할 수 없습니다.</p>
    <textarea readonly value={secret} aria-label="새 downstream token"
    ></textarea>
    <div class="dialog-actions">
      <button bind:this={copyButton} onclick={() => void copy()}
        >{copied ? "복사 완료" : "token 복사"}</button
      ><button class="primary" onclick={() => void close()}>메모리에서 지우고 닫기</button>
    </div>
    <p class="copy-feedback" aria-live="polite">
      {copied ? "클립보드에 복사했습니다." : copyError}
    </p>
  </dialog>
{/if}

<style>
  .form,
  .list article {
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .form {
    margin-bottom: 12px;
  }
  .form form {
    display: grid;
    gap: 14px;
  }
  label {
    display: grid;
    gap: 5px;
  }
  fieldset {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    border: 0;
    padding: 0;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 5px;
    min-height: 44px;
    padding: 6px 8px;
    background: #252b28;
  }
  .check input {
    min-height: auto;
  }
  .limits {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
  }
  .list {
    display: grid;
    gap: 10px;
  }
  .list article {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
  }
  .list h2 {
    margin: 0;
  }
  .list h2 a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
  }
  .list span,
  .list small {
    color: #aeb7b2;
  }
  .list p,
  .list small,
  .actions {
    grid-column: 1/-1;
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .notice,
  .error {
    padding: 12px;
  }
  .notice {
    border: 1px solid #76b900;
  }
  .error {
    border: 1px solid #d65e5e;
  }
  dialog {
    max-width: 650px;
    width: calc(100% - 30px);
    background: #171b1c;
    color: #fff;
    border: 1px solid #76b900;
    border-radius: 8px;
  }
  dialog::backdrop {
    background: rgba(0, 0, 0, 0.8);
  }
  textarea {
    width: 100%;
    min-height: 110px;
    margin: 10px 0;
  }
  .dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  @media (max-width: 780px) {
    .limits {
      grid-template-columns: 1fr 1fr;
    }
  }
  @media (max-width: 450px) {
    .limits {
      grid-template-columns: 1fr;
    }
    .dialog-actions {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
