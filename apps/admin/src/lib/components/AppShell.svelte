<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { page } from "$app/state";
import { token } from "$lib/api";
import AuthControl from "./AuthControl.svelte";

let { children } = $props();
let drawer = $state(false);
let authState = $state<"checking" | "locked" | "ready" | "unavailable">("checking");
let pollBusy = $state(false);
let pollMessage = $state("");
let trigger: HTMLButtonElement;
let navigation: HTMLElement;

const groups = [
  { label: "Command", links: [["/", "개요"]] },
  {
    label: "Capacity",
    links: [
      ["/upstreams", "Upstreams"],
      ["/routing", "라우팅"],
      ["/models", "모델"],
      ["/probes", "Probes"],
    ],
  },
  { label: "Access", links: [["/clients", "Clients"]] },
  {
    label: "Operations",
    links: [
      ["/requests", "요청"],
      ["/incidents", "Incidents"],
      ["/qa", "QA"],
    ],
  },
  {
    label: "Governance",
    links: [
      ["/audit", "감사"],
      ["/settings", "설정"],
    ],
  },
];

let routeLabel = $derived(
  groups
    .flatMap((group) => group.links)
    .find(([path]) => path !== "/" && page.url.pathname.startsWith(`${base}${path}`))?.[1] ??
    "개요",
);
let documentTitle = $derived.by(() => {
  const id = page.params.id;
  if (id && page.url.pathname.startsWith(`${base}/clients/`)) return `Client ${id}`;
  if (id && page.url.pathname.startsWith(`${base}/upstreams/`)) return `Upstream ${id}`;
  if (id && page.url.pathname.startsWith(`${base}/requests/`)) return `요청 ${id}`;
  return routeLabel;
});
function active(path: string): boolean {
  return path === "/"
    ? page.url.pathname === `${base}/` || page.url.pathname === base
    : page.url.pathname === `${base}${path}` || page.url.pathname.startsWith(`${base}${path}/`);
}
function openDrawer(): void {
  drawer = true;
  requestAnimationFrame(() => navigation?.querySelector<HTMLElement>("a[data-nav-item]")?.focus());
}
function closeDrawer(): void {
  drawer = false;
  requestAnimationFrame(() => trigger?.focus());
}
function onKeydown(event: KeyboardEvent): void {
  if (!drawer) return;
  if (event.key === "Escape") {
    closeDrawer();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    navigation.querySelectorAll<HTMLElement>("a,button,input:not([disabled])"),
  ).filter((item) => item.offsetParent !== null);
  const first = focusable[0],
    last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
onMount(() => {
  authState = token() ? "checking" : "locked";
  const update = (event: Event): void => {
    const state = (event as CustomEvent<{ state: "locked" | "checking" | "ready" | "unavailable" }>)
      .detail?.state;
    if (state) authState = state;
  };
  window.addEventListener("nblb-auth-state", update);
  const polling = (event: Event): void => {
    const state = (event as CustomEvent<{ state?: string }>).detail?.state;
    pollBusy = state === "refreshing";
    pollMessage = pollBusy
      ? "운영 정보를 자동 갱신하고 있습니다."
      : "운영 정보 자동 갱신을 마쳤습니다.";
  };
  window.addEventListener("nblb-poll-state", polling);
  return () => {
    window.removeEventListener("nblb-auth-state", update);
    window.removeEventListener("nblb-poll-state", polling);
  };
});
</script>

<svelte:head><title>{documentTitle} · NVIDIA Build LB Admin</title></svelte:head>
<svelte:window onkeydown={onKeydown} />
<a class="skip" href="#content" inert={drawer ? true : undefined}
  >본문 바로가기</a
>
<div class="shell">
  <header inert={drawer ? true : undefined}>
    <a class="brand" href={base || "/admin"}
      ><span>NVIDIA</span> BUILD LB <small>OPERATIONS</small></a
    >
    <button
      class="menu"
      bind:this={trigger}
      type="button"
      aria-expanded={drawer}
      aria-controls="navigation"
      onclick={openDrawer}>메뉴</button
    >
  </header>
  <div class="body">
    {#if drawer}<button
        class="backdrop"
        aria-label="관리 메뉴 닫기"
        onclick={closeDrawer}
      ></button>{/if}
    <nav
      bind:this={navigation}
      id="navigation"
      aria-label="관리 메뉴"
      class:open={drawer}
      role={drawer ? "dialog" : undefined}
      aria-modal={drawer ? "true" : undefined}
    >
      <div class="nav-head">
        <strong>관리 메뉴</strong><button type="button" onclick={closeDrawer}
          >닫기</button
        >
      </div>
      <div class="nav-auth"><AuthControl compact /></div>
      {#each groups as group}
        <section aria-labelledby={`nav-${group.label}`}>
          <h2 id={`nav-${group.label}`}>{group.label}</h2>
          {#each group.links as link}
            <a
              data-nav-item
              class:active={active(link[0])}
              aria-current={active(link[0]) ? "page" : undefined}
              href={`${base}${link[0]}`}
              onclick={closeDrawer}>{link[1]}</a
            >
          {/each}
        </section>
      {/each}
    </nav>
    <main id="content" inert={drawer ? true : undefined}>
      <div class="sr-only" role="status" aria-live="polite" aria-busy={pollBusy}>
        {pollMessage}
      </div>
      <nav class="breadcrumb" aria-label="현재 위치">
        <a href={base || "/admin"}>관리</a><span aria-hidden="true">/</span
        ><span>{routeLabel}</span>
      </nav>
      {#if authState === "ready"}
        {@render children()}
      {:else if authState === "checking"}
        <section class="auth-state" role="status" aria-busy="true">
          <strong>관리 권한 확인 중</strong><span
            >이 탭에 입력한 token의 유효성을 확인하고 있습니다.</span
          >
        </section>
      {:else if authState === "unavailable"}
        <section class="auth-state unavailable" role="alert">
          <strong>관리 서버에 연결할 수 없습니다</strong>
          <span
            >이 탭의 token은 유지됩니다. 관리 메뉴에서 다시 확인하거나 잠글 수
            있습니다.</span
          >
          <button class="mobile-auth-open" type="button" onclick={openDrawer}
            >관리 메뉴 열기</button
          >
        </section>
      {:else}
        <section class="auth-state locked" role="alert">
          <strong>관리 인증이 필요합니다</strong>
          <span class="desktop-auth-help"
            >왼쪽 관리 메뉴의 token 입력란에서 다시 인증하면 최신 운영 상태를
            불러옵니다. 이전 snapshot은 표시하지 않습니다.</span
          >
          <span class="mobile-auth-help"
            >관리 메뉴를 열어 token으로 다시 인증하면 최신 운영 상태를
            불러옵니다. 이전 snapshot은 표시하지 않습니다.</span
          >
          <button class="mobile-auth-open" type="button" onclick={openDrawer}
            >관리 메뉴 열기</button
          >
        </section>
      {/if}
    </main>
  </div>
</div>

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(html) {
    background: #0d0f10;
    color: #f4f7f5;
    font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  }
  :global(body) {
    margin: 0;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  :global(button),
  :global(input),
  :global(select),
  :global(textarea) {
    font: inherit;
  }
  :global(button) {
    min-height: 44px;
    border: 1px solid #77817c;
    border-radius: 6px;
    background: #252a2d;
    color: #f4f7f5;
    padding: 8px 13px;
    cursor: pointer;
  }
  :global(button:disabled) {
    cursor: not-allowed;
    opacity: 0.55;
  }
  :global(button.primary) {
    background: #76b900;
    color: #081000;
    border-color: #76b900;
    font-weight: 800;
  }
  :global(button.danger) {
    color: #ffadad;
    border-color: #d65e5e;
  }
  :global(input),
  :global(select),
  :global(textarea) {
    min-height: 44px;
    border: 1px solid #77817c;
    border-radius: 6px;
    background: #101314;
    color: #f4f7f5;
    padding: 9px 11px;
  }
  :global(a) {
    color: #9cdb45;
  }
  :global(:focus-visible) {
    outline: 3px solid #9cdb45;
    outline-offset: 3px;
  }
  .skip {
    position: fixed;
    left: 12px;
    top: -80px;
    z-index: 30;
    background: #fff;
    color: #000;
    padding: 10px;
  }
  .skip:focus {
    top: 12px;
  }
  .shell {
    min-height: 100vh;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 8;
    display: flex;
    align-items: center;
    gap: 20px;
    min-height: 68px;
    padding: 10px 22px;
    border-bottom: 1px solid #3e4642;
    background: #111415;
  }
  .brand {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #fff;
    text-decoration: none;
    font-weight: 900;
    letter-spacing: 0.04em;
  }
  .brand span {
    color: #76b900;
  }
  .brand small {
    display: block;
    color: #aab3ae;
    font-size: 0.65rem;
  }
  .menu,
  .nav-head {
    display: none;
  }
  .nav-auth {
    margin-bottom: 12px;
    padding: 0 8px;
  }
  .body {
    display: grid;
    grid-template-columns: 230px minmax(0, 1fr);
  }
  nav#navigation {
    position: sticky;
    top: 68px;
    height: calc(100vh - 68px);
    overflow: auto;
    padding: 14px 12px;
    border-right: 1px solid #3e4642;
    background: #131718;
  }
  nav section {
    margin-bottom: 14px;
  }
  nav h2 {
    margin: 8px 12px 3px;
    color: #78837d;
    font-size: 0.66rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  nav a {
    display: block;
    min-height: 44px;
    padding: 11px 12px;
    border-radius: 6px;
    color: #c7cfcb;
    text-decoration: none;
  }
  nav a.active {
    background: #263017;
    color: #b7ef68;
    font-weight: 800;
  }
  main {
    min-width: 0;
    max-width: 1440px;
    width: 100%;
    padding: 24px 30px 40px;
    margin: 0 auto;
  }
  .breadcrumb {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 18px;
    color: #8f9a94;
    font-size: 0.78rem;
  }
  .breadcrumb a {
    display: inline-flex;
    min-width: 44px;
    min-height: 44px;
    align-items: center;
    background: none;
  }
  .backdrop {
    display: none;
  }
  .auth-state {
    display: grid;
    gap: 7px;
    padding: 20px;
    border: 1px solid #5f6964;
    border-radius: 8px;
    background: #171b1c;
    color: #c7cfcb;
  }
  .auth-state.locked {
    border-color: #d99b48;
  }
  .auth-state strong {
    color: #fff;
  }
  .mobile-auth-help,
  .mobile-auth-open {
    display: none;
  }
  @media (max-width: 760px) {
    header {
      padding: 10px 14px;
    }
    .menu,
    .nav-head {
      display: flex;
    }
    .menu {
      margin-left: auto;
    }
    .body {
      display: block;
    }
    nav#navigation {
      position: fixed;
      inset: 0 12% 0 0;
      z-index: 22;
      height: 100vh;
      transform: translateX(-105%);
      transition: transform 0.15s;
      background: #15191a;
      padding: 18px;
      box-shadow: 0 20px 50px #000;
      visibility: hidden;
    }
    nav#navigation.open {
      transform: none;
      visibility: visible;
    }
    .nav-head {
      justify-content: space-between;
      align-items: center;
    }
    .nav-auth {
      padding: 0;
    }
    .backdrop {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 20;
      width: 100%;
      height: 100%;
      border: 0;
      border-radius: 0;
      background: rgba(0, 0, 0, 0.72);
    }
    main {
      padding: 18px 14px 34px;
    }
    .desktop-auth-help {
      display: none;
    }
    .mobile-auth-help,
    .mobile-auth-open {
      display: block;
    }
    .mobile-auth-open {
      justify-self: start;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    nav#navigation {
      transition: none;
    }
  }
</style>
