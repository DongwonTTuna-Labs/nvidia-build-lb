<script lang="ts">
import { type AdminRouteId, adminRoutes } from "$lib/copy";

export let active: AdminRouteId;
export let onSelect: (route: AdminRouteId) => void;
</script>

<nav aria-label="관리 메뉴" class="nav">
  {#each adminRoutes as route}
    <a
      href={route.href}
      aria-current={active === route.id ? "page" : undefined}
      onclick={(event) => {
        event.preventDefault();
        onSelect(route.id);
      }}>{route.label}</a
    >
  {/each}
</nav>

<style>
  .nav {
    display: flex;
    flex-wrap: wrap;
    overflow: visible;
    border-bottom: 1px solid #6b746f;
  }
  .nav a {
    min-height: 44px;
    padding: 10px 12px;
    color: #d0d5d2;
    text-decoration: none;
    white-space: nowrap;
  }
  .nav a[aria-current="page"] {
    color: #76b900;
    border-bottom: 3px solid #76b900;
    font-weight: 800;
  }
  .nav a:focus-visible {
    outline: 3px solid #76b900;
    outline-offset: 3px;
  }
  @media (max-width: 767px) {
    .nav {
      gap: 4px;
      overflow-x: auto;
      flex-wrap: nowrap;
      scrollbar-width: thin;
      padding-right: 52px;
      background: linear-gradient(90deg, transparent 0 calc(100% - 44px), #171a1d 100%);
    }
    .nav::after {
      content: "좌우로 더 보기";
      position: sticky;
      right: 0;
      align-self: center;
      flex: 0 0 auto;
      color: #aab2ae;
      font-size: 0.72rem;
      background: #171a1d;
      padding: 4px 6px;
      pointer-events: none;
    }
    .nav a {
      flex: 0 0 auto;
      text-align: center;
    }
  }
</style>
