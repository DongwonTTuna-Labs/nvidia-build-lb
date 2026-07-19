<script lang="ts">
import type { Client } from "$lib/admin-types";
import type { AdminRouteId } from "$lib/copy";

export let active: AdminRouteId;
export let clients: Client[];
export let sessionToken: string;
export let mutating: boolean;
export let mutationState: string;
export let onRevoke: (id: string, label: string, event: MouseEvent) => void;
</script>

<section id="clients" class:panel-hidden={active !== "clients"} class="panel" aria-labelledby="clients-title" hidden={active !== "clients"}>
  <h2 id="clients-title">필요한 권한만 발급</h2>
  <p class="muted">새 접속 키는 발급 직후 native dialog에서 한 번만 보입니다.</p>
  <slot />
  <div class="table-wrap">
    <table>
      <caption class="sr-only">다운스트림 접속 키</caption>
      <thead><tr><th scope="col">라벨</th><th scope="col">권한</th><th scope="col">상태</th><th scope="col"><span class="sr-only">조작</span></th></tr></thead>
      <tbody>
        {#each clients as client}
          <tr><th scope="row" id={`client-${client.id}`}>{client.label}<small>{client.request_count}회 사용</small></th><td data-label="권한">{client.scopes.join(", ")}</td><td data-label="상태">{client.active ? "사용 중" : "폐기됨"}</td><td data-label="조작">{#if client.active}<button class="danger" type="button" aria-label={`${client.label} 접속 키 폐기`} onclick={(event) => onRevoke(client.id, client.label, event)} disabled={!sessionToken || mutating || mutationState !== "idle"}>폐기</button>{/if}</td></tr>
        {:else}
          <tr><td colspan="4">발급된 접속 키가 없습니다.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

<style>
  .panel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  .muted, small { color: #aab2ae; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 680px; }
  th, td { padding: 12px 10px; border-bottom: 1px solid #6b746f; text-align: left; vertical-align: middle; }
  th { color: #f5f7f6; }
  td, th small { display: table-cell; }
  tbody th { display: table-cell; }
  tbody th small { display: block; margin-top: 3px; }
  button { min-height: 44px; border: 0; border-radius: 6px; padding: 10px 14px; cursor: pointer; font: inherit; font-weight: 800; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .danger { color: #ff8a8a; background: transparent; border: 1px solid #ff8a8a; }
  @media (max-width: 767px) {
    .panel { padding: 16px; }
    table { min-width: 0; }
    thead { display: none; }
    table, tbody, tr, th, td { display: block; width: 100%; }
    tr { padding: 12px 0; border-bottom: 1px solid #6b746f; }
    th, td { border: 0; padding: 5px 0; }
    td::before { content: attr(data-label); display: block; color: #aab2ae; font-size: .8rem; }
  }
</style>
