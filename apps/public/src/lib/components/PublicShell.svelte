<script lang="ts">
import { page } from "$app/stores";

const primary = [
  ["/", "홈"],
  ["/status", "상태"],
  ["/models", "모델"],
  ["/docs", "연결 가이드"],
] as const;

const active = (path: string) =>
  path === "/" ? $page.url.pathname === "/" : $page.url.pathname.startsWith(path);
</script>

<a class="skip-link" href="#main-content">본문으로 건너뛰기</a>
<div class="site-shell">
  <header class="topbar">
    <a class="brand" href="/" aria-label="NVIDIA Build LB 홈">
      <span class="brand-mark" aria-hidden="true">N</span>
      <span
        ><strong>NVIDIA BUILD LB</strong><small>HOSTED API GATEWAY</small></span
      >
    </a>
    <nav aria-label="주요 메뉴">
      {#each primary as item}
        <a href={item[0]} aria-current={active(item[0]) ? "page" : undefined}
          >{item[1]}</a
        >
      {/each}
    </nav>
  </header>
  <main id="main-content"><slot /></main>
  <footer>
    <div>
      <strong>NVIDIA BUILD LB</strong>
      <p>두 개의 검증된 NVIDIA provider slot을 위한 OpenAI 호환 라우팅.</p>
    </div>
    <nav aria-label="보조 메뉴">
      <a href="/incidents">장애 이력</a>
      <a href="/security">보안과 개인정보</a>
      <a href="/docs#errors">오류와 재시도</a>
    </nav>
  </footer>
</div>

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(html) {
    color-scheme: dark;
    background: #080b0d;
  }
  :global(body) {
    margin: 0;
    background: #080b0d;
    color: #f3f6f4;
    font-family:
      Inter,
      ui-sans-serif,
      system-ui,
      -apple-system,
      sans-serif;
  }
  :global(button),
  :global(input),
  :global(select),
  :global(textarea) {
    font: inherit;
  }
  :global(a) {
    color: inherit;
  }
  :global(:focus-visible) {
    outline: 3px solid #b8e86f;
    outline-offset: 3px;
  }
  :global(h1),
  :global(h2),
  :global(h3),
  :global(p) {
    margin-top: 0;
  }
  :global(h1) {
    max-width: 850px;
    margin-bottom: 20px;
    font-size: clamp(42px, 7vw, 82px);
    letter-spacing: -0.06em;
    line-height: 0.98;
    word-break: keep-all;
    overflow-wrap: break-word;
  }
  :global(h2) {
    font-size: clamp(24px, 3vw, 34px);
    letter-spacing: -0.04em;
  }
  :global(.eyebrow) {
    margin: 0 0 13px;
    color: #8dc63f;
    font-size: 11px;
    font-weight: 850;
    letter-spacing: 0.16em;
  }
  :global(.lede) {
    max-width: 720px;
    color: #aeb9b4;
    font-size: clamp(15px, 2vw, 19px);
    line-height: 1.7;
  }
  :global(.page-head) {
    padding: clamp(58px, 9vw, 108px) 0 44px;
    border-bottom: 1px solid #26312d;
  }
  :global(.section) {
    padding: clamp(38px, 6vw, 72px) 0;
  }
  :global(.grid) {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 18px;
  }
  :global(.panel) {
    border: 1px solid #29342f;
    border-radius: 6px;
    background: #0e1314;
    padding: clamp(20px, 3vw, 30px);
  }
  :global(.status-pill) {
    display: inline-flex;
    align-items: center;
    min-height: 30px;
    padding: 5px 10px;
    border: 1px solid #52623f;
    border-radius: 999px;
    color: #d6e8bc;
    background: #182016;
    font-size: 12px;
    font-weight: 750;
  }
  :global(.status-pill[data-tone="warning"]) {
    border-color: #765e32;
    background: #211b12;
    color: #f0cc87;
  }
  :global(.status-pill[data-tone="danger"]) {
    border-color: #79483e;
    background: #211412;
    color: #f2a292;
  }
  :global(.muted) {
    color: #98a59f;
  }
  :global(.cta-row) {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 25px;
  }
  :global(.button) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 46px;
    padding: 10px 16px;
    border: 1px solid #496222;
    border-radius: 4px;
    background: #76b900;
    color: #071000;
    font-weight: 850;
    text-decoration: none;
  }
  :global(.button.secondary) {
    background: transparent;
    color: #dbe5df;
    border-color: #3a4842;
  }
  :global(.notice) {
    border-left: 3px solid #76b900;
    background: #111813;
    padding: 15px 17px;
    color: #bac7c0;
    line-height: 1.6;
  }
  :global(.error-panel) {
    border-color: #78483d;
  }
  :global(.empty-panel) {
    text-align: center;
    color: #9eaaa4;
  }
  :global(code),
  :global(pre) {
    font-family: "SFMono-Regular", Consolas, monospace;
  }
  :global(pre) {
    max-width: 100%;
    overflow-x: auto;
    border: 1px solid #2c3833;
    background: #080c0d;
    padding: 18px;
    line-height: 1.7;
  }
  .skip-link {
    position: fixed;
    z-index: 20;
    top: 8px;
    left: 8px;
    transform: translateY(-180%);
    background: white;
    color: #080b0d;
    padding: 12px 16px;
    font-weight: 800;
  }
  .skip-link:focus {
    transform: translateY(0);
  }
  .site-shell {
    max-width: 1240px;
    min-height: 100vh;
    margin: 0 auto;
    padding: 0 clamp(16px, 4vw, 56px);
  }
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 82px;
    gap: 24px;
    border-bottom: 1px solid #26312d;
  }
  .brand {
    display: flex;
    align-items: center;
    min-height: 48px;
    gap: 12px;
    text-decoration: none;
  }
  .brand-mark {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    background: #76b900;
    color: #071000;
    font-size: 20px;
    font-weight: 950;
    transform: skew(-10deg);
  }
  .brand strong,
  .brand small {
    display: block;
    letter-spacing: 0.1em;
  }
  .brand strong {
    font-size: 13px;
  }
  .brand small {
    color: #85928c;
    font-size: 9px;
  }
  nav {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 5px;
  }
  nav a {
    min-width: 44px;
    min-height: 44px;
    padding: 12px 13px;
    color: #aab5af;
    text-decoration: none;
    font-size: 13px;
    font-weight: 700;
  }
  nav a:hover,
  nav a[aria-current="page"] {
    color: #dff4bd;
    background: #141d11;
  }
  footer {
    display: flex;
    justify-content: space-between;
    gap: 30px;
    margin-top: 50px;
    padding: 32px 0 44px;
    border-top: 1px solid #26312d;
  }
  footer strong {
    font-size: 12px;
    letter-spacing: 0.14em;
  }
  footer p {
    max-width: 430px;
    margin: 8px 0 0;
    color: #84918b;
    font-size: 12px;
  }
  footer nav {
    align-items: flex-start;
  }
  @media (max-width: 760px) {
    .topbar {
      align-items: flex-start;
      flex-direction: column;
      padding: 18px 0 12px;
      gap: 8px;
    }
    .topbar nav {
      width: 100%;
      overflow-x: auto;
      flex-wrap: nowrap;
    }
    .topbar nav a {
      white-space: nowrap;
    }
    footer {
      flex-direction: column;
    }
    footer nav {
      align-items: stretch;
      flex-direction: column;
    }
  }
  @media (max-width: 420px) {
    :global(h1) {
      font-size: clamp(34px, 11vw, 42px);
      line-height: 1.06;
      letter-spacing: -0.045em;
    }
    :global(.page-head h1 br) {
      display: none;
    }
    :global(.page-head) {
      padding-top: 44px;
    }
  }
  @media (max-width: 220px) {
    :global(h1) {
      font-size: 24px;
      letter-spacing: -0.025em;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
      animation-duration: 0.01ms !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }
  }
</style>
