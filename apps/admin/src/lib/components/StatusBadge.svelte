<script lang="ts">
export let good = false;
export let label = "";
</script>

<span class:good class="status"><span aria-hidden="true">{good ? "●" : "○"}</span> {label}</span>

<style>
  .status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    width: fit-content;
    border: 1px solid #6b746f;
    border-radius: 6px;
    padding: 8px 10px;
    color: #d0d5d2;
    font-weight: 700;
    white-space: nowrap;
  }
  .status.good {
    color: #76b900;
    border-color: #76b900;
  }
  @media (max-width: 767px) {
    .status {
      white-space: normal;
      overflow-wrap: anywhere;
    }
  }
</style>

