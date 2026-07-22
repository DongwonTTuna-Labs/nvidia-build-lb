<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { page } from "$app/state";
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
import type { Client } from "$lib/types";

const allScopes = [
  "models:read",
  "chat:write",
  "embeddings:write",
  "images:write",
  "media:write",
  "audio:write",
];
interface FormState {
  scopes: string[];
  rpm: string;
  concurrency: string;
  daily: string;
  expires: string;
  allowlist: string;
}
let item = $state<Client | null>(null),
  loading = $state(true),
  queryError = $state(""),
  actionError = $state(""),
  notice = $state(""),
  busy = $state(false),
  baseline = $state<FormState | null>(null),
  scopes = $state<string[]>([]),
  rpm = $state(""),
  concurrency = $state(""),
  daily = $state(""),
  expires = $state(""),
  allowlist = $state(""),
  stale = $state(false),
  lastSuccessAt = $state<string | null>(null),
  notFound = $state(false);
let activeRouteId = "";
let routeEpoch = 0;
let loadEpoch = 0;
const dirty = $derived(
  baseline !== null && JSON.stringify(formState()) !== JSON.stringify(baseline),
);
function formState(): FormState {
  return { scopes: [...scopes], rpm, concurrency, daily, expires, allowlist };
}
function hydrate(value: Client): void {
  item = value;
  scopes = [...value.scopes];
  rpm = value.rpm_limit?.toString() ?? "";
  concurrency = value.max_concurrency?.toString() ?? "";
  daily = value.request_limit_day?.toString() ?? "";
  expires = datetimeLocalValue(value.expires_at);
  allowlist = value.model_allowlist?.join(", ") ?? "";
  baseline = formState();
  stale = false;
  lastSuccessAt = new Date().toISOString();
}
function resetForRoute(id: string): number {
  activeRouteId = id;
  routeEpoch += 1;
  loadEpoch += 1;
  item = null;
  loading = true;
  queryError = "";
  actionError = "";
  notice = "";
  busy = false;
  baseline = null;
  scopes = [];
  rpm = "";
  concurrency = "";
  daily = "";
  expires = "";
  allowlist = "";
  stale = false;
  lastSuccessAt = null;
  notFound = false;
  return routeEpoch;
}
async function load(id = activeRouteId, epoch = routeEpoch): Promise<void> {
  if (!id || epoch !== routeEpoch || id !== activeRouteId) return;
  if (busy || dirty) {
    stale = item !== null;
    return;
  }
  const requestEpoch = ++loadEpoch;
  loading = !item;
  if (!item) queryError = "";
  try {
    const value = await api<Client>(`/clients/${id}`);
    if (epoch !== routeEpoch || id !== activeRouteId || requestEpoch !== loadEpoch || busy || dirty)
      return;
    hydrate(value);
    queryError = "";
    notFound = false;
  } catch (e) {
    if (epoch !== routeEpoch || id !== activeRouteId || requestEpoch !== loadEpoch) return;
    if (e instanceof ApiError && [404, 422].includes(e.status)) {
      notFound = true;
      queryError = "";
    } else queryError = adminErrorMessage(e, "Client 상세를 읽지 못했습니다.");
  } finally {
    if (epoch === routeEpoch && id === activeRouteId && requestEpoch === loadEpoch) loading = false;
  }
}
function toggle(scope: string) {
  scopes = scopes.includes(scope) ? scopes.filter((v) => v !== scope) : [...scopes, scope];
}
async function save() {
  if (!scopes.length) {
    actionError = "scope를 하나 이상 선택하세요.";
    return;
  }
  const id = activeRouteId;
  const epoch = routeEpoch;
  if (!baseline || !dirty || !item || item.id !== id) return;
  loadEpoch += 1;
  busy = true;
  actionError = "";
  notice = "";
  try {
    const current = formState();
    const body: Record<string, unknown> = {};
    if (JSON.stringify(current.scopes) !== JSON.stringify(baseline.scopes)) {
      body.scopes = current.scopes;
    }
    if (current.expires !== baseline.expires) {
      body.expires_at = current.expires ? new Date(current.expires).toISOString() : null;
    }
    if (current.allowlist !== baseline.allowlist) {
      body.model_allowlist = current.allowlist.trim()
        ? current.allowlist
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean)
        : null;
    }
    for (const [field, value, previous] of [
      ["rpm_limit", current.rpm, baseline.rpm],
      ["max_concurrency", current.concurrency, baseline.concurrency],
      ["request_limit_day", current.daily, baseline.daily],
    ] as const) {
      if (value !== previous) body[field] = value ? Number(value) : null;
    }
    const value = await api<{ item: Client }>(`/clients/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    if (epoch !== routeEpoch || id !== activeRouteId || value.item.id !== id) return;
    hydrate(value.item);
    notice = "변경한 정책만 저장했습니다.";
  } catch (e) {
    if (epoch === routeEpoch && id === activeRouteId) {
      actionError = adminErrorMessage(e, "Client 정책 저장에 실패했습니다.");
    }
  } finally {
    if (epoch === routeEpoch && id === activeRouteId) busy = false;
  }
}
function reset(): void {
  if (item) hydrate(item);
  actionError = "";
  notice = "변경 내용을 되돌렸습니다.";
}
$effect(() => {
  const id = page.params.id;
  if (!id || id === activeRouteId) return;
  const epoch = resetForRoute(id);
  void load(id, epoch);
});
onMount(() => {
  const stop = startPolling(load, 30_000, false);
  return () => {
    routeEpoch += 1;
    stop();
  };
});
</script>

<a class="back" href={`${base}/clients`}>← Client 목록</a><PageHeader
  title={item?.label ?? "Client 상세"}
  description="최소 권한 scope, 만료, allowlist, 소비 제한을 편집합니다."
/>{#if notFound}<section class="missing" role="alert">
    <strong>Client를 찾을 수 없습니다.</strong><span
      >삭제되었거나 URL이 올바르지 않습니다.</span
    ><a href={`${base}/clients`}>Client 목록으로 돌아가기</a>
  </section>{:else}<DataState
    {loading}
    error={queryError}
    hasData={item !== null}
    {lastSuccessAt}
    retry={load}
  />{/if}
{#if stale}<p class="stale" role="status">
    저장되지 않은 변경이 있어 자동 갱신을 보류했습니다 · 마지막 확인 {displayTime(
      lastSuccessAt,
    )}
  </p>{/if}{#if actionError}<p class="error" role="alert">
    {actionError}
  </p>{/if}{#if notice}<p class="notice" role="status">
    {notice}
  </p>{/if}{#if item && !notFound}<section class="meta">
    <StatusBadge value={item.active ? "active" : "revoked"} /><span
      >{item.prefix}</span
    ><span>마지막 사용 {displayTime(item.last_used_at)}</span><span
      >누적 {item.request_count} 요청</span
    >{#if dirty}<strong>저장되지 않은 변경</strong>{/if}
  </section>
  <form
    onsubmit={(e) => {
      e.preventDefault();
      void save();
    }}
  >
    <fieldset disabled={!item.active || busy}>
      <legend>허용 scope</legend>{#each allScopes as scope}<label class="check"
          ><input
            type="checkbox"
            checked={scopes.includes(scope)}
            onchange={() => toggle(scope)}
          />{scope}</label
        >{/each}
    </fieldset>
    <div class="grid">
      <label
        >RPM · 비우면 해제<input
          type="number"
          min="1"
          bind:value={rpm}
        /></label
      ><label
        >동시 요청 · 비우면 해제<input
          type="number"
          min="1"
          max="64"
          bind:value={concurrency}
        /></label
      ><label
        >일일 요청 · 비우면 해제<input
          type="number"
          min="1"
          bind:value={daily}
        /></label
      ><label
        >만료 · 비우면 해제<input
          type="datetime-local"
          bind:value={expires}
        /></label
      >
    </div>
    <label>모델 allowlist · 비우면 해제<input bind:value={allowlist} /></label>
    <div class="actions">
      <button type="button" onclick={reset} disabled={!dirty || busy}
        >변경 취소</button
      ><button class="primary" disabled={!item.active || !dirty || busy}
        >{busy ? "저장 중…" : "변경 정책 저장"}</button
      >
    </div>
  </form>{/if}

<style>
  .meta,
  form {
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .meta {
    display: flex;
    gap: 14px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .meta span {
    color: #adb7b1;
  }
  .meta strong {
    color: #ffd08a;
  }
  form {
    display: grid;
    gap: 16px;
  }
  fieldset {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    border: 0;
    padding: 0;
  }
  .check {
    display: flex;
    align-items: center;
    min-height: 44px;
    gap: 5px;
    padding: 7px 9px;
    background: #252b28;
  }
  .check input {
    min-height: auto;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
  }
  label {
    display: grid;
    gap: 5px;
  }
  .notice,
  .error,
  .stale {
    padding: 12px;
  }
  .notice {
    border: 1px solid #76b900;
  }
  .error {
    border: 1px solid #d65e5e;
  }
  .stale {
    border: 1px solid #d99b48;
    color: #ffd08a;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
  .back,
  .missing a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
  }
  .missing {
    display: grid;
    gap: 8px;
    padding: 20px;
    border: 1px solid #d99b48;
  }
  @media (max-width: 800px) {
    .grid {
      grid-template-columns: 1fr 1fr;
    }
  }
  @media (max-width: 450px) {
    .grid {
      grid-template-columns: 1fr;
    }
    .actions {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
