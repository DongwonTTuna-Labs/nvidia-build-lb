<script lang="ts">
let { cursor, busy, loadMore } = $props<{
  cursor?: string | null;
  busy: boolean;
  loadMore: () => void;
}>();
</script>

{#if cursor}
  <div class="more">
    <button type="button" onclick={loadMore} disabled={busy} aria-busy={busy}
      >{busy ? "더 불러오는 중…" : "더 보기"}</button
    >
  </div>
{/if}

<style>
  .more {
    display: flex;
    justify-content: center;
    padding: 22px;
  }
  .more button {
    min-width: 150px;
  }
</style>
