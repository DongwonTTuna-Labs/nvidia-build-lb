<script lang="ts">
let { value } = $props<{ value: string }>();
const positive = new Set([
  "active",
  "available",
  "eligible",
  "operational",
  "passed",
  "succeeded",
  "verified",
]);
const negative = new Set(["failed", "rejected", "revoked", "retired", "critical", "unavailable"]);
let tone = $derived(
  positive.has(value) ? "positive" : negative.has(value) ? "negative" : "neutral",
);
</script>

<span class={tone}>{value.replaceAll("_", " ")}</span>

<style>
  span {
    display: inline-flex;
    align-items: center;
    min-height: 26px;
    border: 1px solid #59635e;
    border-radius: 999px;
    padding: 2px 9px;
    color: #c5cec9;
    font-size: 0.76rem;
    font-weight: 750;
  }
  .positive {
    border-color: #5d8e23;
    color: #b9ed77;
    background: #1a2512;
  }
  .negative {
    border-color: #9b5151;
    color: #ffc0c0;
    background: #2b1717;
  }
  .neutral {
    background: #202526;
  }
</style>
