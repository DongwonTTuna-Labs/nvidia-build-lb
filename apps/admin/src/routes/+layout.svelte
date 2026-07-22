<script lang="ts">
import AppShell from "$lib/components/AppShell.svelte";

let { children } = $props();
</script>

<svelte:head><link rel="icon" href="/favicon.svg" type="image/svg+xml" /></svelte:head>
<AppShell>{@render children()}</AppShell>
