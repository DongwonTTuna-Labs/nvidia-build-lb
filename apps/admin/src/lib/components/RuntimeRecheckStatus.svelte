<script lang="ts">
let { message }: { message: string } = $props();
</script>

<p class="runtime-recheck-status" role="status" aria-live="polite" aria-atomic="true">
  {message}
</p>

<style>
  .runtime-recheck-status {
    min-height: 1.5em;
    margin: 8px 0 16px;
    color: #d7ddd9;
  }
</style>
