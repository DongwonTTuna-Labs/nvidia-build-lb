<script lang="ts">
import { onMount } from "svelte";
import { publicApi, startPolling } from "$lib/api";
import { observed } from "$lib/format";
import type { PublicIncidents } from "$lib/types";

let data: PublicIncidents | null = null;
let loading = true;
let refreshing = false;
let stale = false;
let error = "";
async function load(background = false) {
  if (background || data) refreshing = true;
  else loading = true;
  error = "";
  try {
    data = await publicApi.incidents();
    stale = data.snapshot.stale;
  } catch (caught) {
    error = caught instanceof Error ? caught.message : "장애 이력을 불러오지 못했습니다.";
    stale = data !== null;
  } finally {
    loading = false;
    refreshing = false;
  }
}
onMount(() => startPolling(() => load(data !== null)));
</script>

<svelte:head
  ><title>장애 이력 · NVIDIA Build LB</title><meta
    name="description"
    content="NVIDIA Build LB의 공개 incident와 상태 update."
  /></svelte:head
>
<header class="page-head">
  <p class="eyebrow">INCIDENT HISTORY</p>
  <h1>확인된 사실만<br />시간순으로 남깁니다.</h1>
  <p class="lede">
    운영자가 공개한 incident와 update만 표시합니다. 내부 provider·client·request
    식별자는 공개하지 않습니다.
  </p>
</header>
<section class="section">
  {#if stale && data}<div class="notice" role="status">
      {#if error}새로 고침 실패: {error} · {/if}마지막 성공 snapshot을 표시합니다 · 마지막 성공 {observed(
        data.snapshot.observed_at,
      )} ·
      <button type="button" onclick={() => load(true)} disabled={refreshing}
        >{refreshing ? "확인 중" : "다시 확인"}</button
      >
    </div>{/if}{#if loading}<div class="panel" role="status" aria-busy="true">
      <h2>장애 이력 확인 중</h2>
    </div>{:else if error && !data}<div class="panel error-panel" role="alert">
      <h2>이력을 불러오지 못했습니다</h2>
      <p>{error}</p>
      <button class="button secondary" type="button" onclick={() => load()}
        >다시 시도</button
      >
    </div>{:else if !data?.items.length}<div class="panel empty-panel">
      <span class="status-pill">공개 incident 없음</span>
      <h2>현재 공개된 장애 이력이 없습니다</h2>
      <p>incident가 공개되면 시작 시각과 update가 이곳에 표시됩니다.</p>
    </div>{:else}<div class="incident-list">
      {#each data.items as incident (incident.slug)}<a
          class="panel"
          href={`/incidents/${incident.slug}`}
          ><div>
            <span
              class="status-pill"
              data-tone={incident.status === "resolved" ? "ok" : "warning"}
              >{incident.status}</span
            ><span class="severity">{incident.severity}</span>
          </div>
          <h2>{incident.title}</h2>
          <p>
            {incident.updates.at(-1)?.message ?? "상세 update를 확인하세요."}
          </p>
          <small
            >{observed(incident.started_at)} · update {incident.updates
              .length}개</small
          ></a
        >{/each}
    </div>{/if}
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
  .incident-list {
    display: grid;
    gap: 14px;
  }
  .incident-list a {
    display: block;
    text-decoration: none;
  }
  .incident-list a:hover {
    border-color: #55732c;
  }
  .incident-list h2 {
    margin: 19px 0 9px;
  }
  .incident-list p {
    color: #a6b2ac;
  }
  .incident-list small,
  .severity {
    color: #829089;
    font-size: 11px;
  }
  .severity {
    margin-left: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
</style>
