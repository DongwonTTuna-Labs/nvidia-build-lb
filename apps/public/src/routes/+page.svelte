<script lang="ts">
import { onMount } from "svelte";

type Health = {
  status: string;
  ready: boolean;
  database_ready: boolean;
  traffic_ready: boolean;
  pair_ready: boolean;
  eligible_keys: number;
  reason_codes: string[];
};

type ViewState = "loading" | "ready" | "degraded" | "offline";

const initialHealth: Health = {
  status: "loading",
  ready: false,
  database_ready: false,
  traffic_ready: false,
  pair_ready: false,
  eligible_keys: 0,
  reason_codes: [],
};

let health: Health = initialHealth;
let viewState: ViewState = "loading";
let lastChecked = "";
let refreshing = false;

function stateLabel() {
  if (viewState === "offline") return "연결 확인 필요";
  if (viewState === "loading") return "상태 확인 중";
  const reason = primaryReason();
  if (reason === "database_unavailable") return "상태 확인 불가";
  if (reason === "no_eligible_upstream") return "운영 준비 중";
  if (reason === "pair_not_ready") return "제한된 용량으로 운영 중";
  if (health.pair_ready) return "트래픽 준비 완료";
  return "운영 상태 확인 중";
}

function primaryReason() {
  for (const reason of ["database_unavailable", "no_eligible_upstream", "pair_not_ready"]) {
    if (health.reason_codes.includes(reason)) return reason;
  }
  return health.reason_codes.length > 0 ? "unknown" : null;
}

function stateMessage() {
  if (viewState === "offline") return "게이트웨이에 연결할 수 없습니다. 잠시 후 다시 시도하세요.";
  if (viewState === "loading") return "게이트웨이의 현재 상태를 확인하고 있습니다.";
  const reason = primaryReason();
  if (reason === "database_unavailable") {
    return "게이트웨이 저장소 연결을 확인하고 있습니다.";
  }
  if (reason === "no_eligible_upstream") {
    return "현재 요청을 전달할 provider 용량을 준비하고 있습니다.";
  }
  if (reason === "pair_not_ready") {
    return "요청은 가능하지만 이중화 용량을 준비하고 있습니다.";
  }
  if (health.pair_ready) return "요청을 안전하게 전달할 수 있습니다.";
  if (reason === "unknown") {
    return "확인된 상태 정보가 갱신될 때까지 잠시 기다려 주세요.";
  }
  return "확인된 상태 정보가 갱신될 때까지 잠시 기다려 주세요.";
}

function checkedLabel() {
  return lastChecked ? `마지막 확인 ${lastChecked}` : "아직 확인하지 않음";
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch("/health", { cache: "no-store" });
    const body = (await response.json()) as Partial<Health>;
    health = {
      status: typeof body.status === "string" ? body.status : "unknown",
      ready: body.ready === true,
      database_ready: body.database_ready === true,
      traffic_ready: body.traffic_ready === true,
      pair_ready: body.pair_ready === true,
      eligible_keys: Number.isInteger(body.eligible_keys) ? Number(body.eligible_keys) : 0,
      reason_codes: Array.isArray(body.reason_codes)
        ? body.reason_codes.filter((reason): reason is string => typeof reason === "string")
        : [],
    };
    viewState = health.traffic_ready ? "ready" : "degraded";
    lastChecked = new Intl.DateTimeFormat("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date());
  } catch {
    health = initialHealth;
    viewState = "offline";
  } finally {
    refreshing = false;
  }
}

onMount(() => {
  void refresh();
  const interval = window.setInterval(() => void refresh(), 15_000);
  return () => window.clearInterval(interval);
});
</script>

<svelte:head>
  <title>NVIDIA Build LB · 운영 상태</title>
  <meta
    name="description"
    content="NVIDIA hosted API load balancer의 공개 운영 상태와 지원 API를 확인합니다."
  />
</svelte:head>

<a class="skip-link" href="#main-content">본문으로 건너뛰기</a>
<main id="main-content" class="shell">
  <header class="topbar">
    <a class="brand" href="/" aria-label="NVIDIA Build LB 홈">
      <span class="brand-mark" aria-hidden="true">N</span>
      <span>
        <strong>NVIDIA BUILD LB</strong>
        <small>HOSTED API GATEWAY</small>
      </span>
    </a>
    <span class="environment">HOME SERVER · PUBLIC STATUS</span>
  </header>

  <section class="hero" aria-labelledby="page-title">
    <div class="hero-copy">
      <p class="eyebrow">NVIDIA HOSTED API ROUTING</p>
      <h1 id="page-title">필요한 API에<br /><span>바로 연결</span>됩니다.</h1>
      <p class="lede">
        두 개의 upstream을 rate-aware하게 분산하고, 장애 시 자동 전환하는 OpenAI 호환
        게이트웨이입니다.
      </p>
    </div>
    <div
      class:ready={health.pair_ready}
      class:offline={viewState === "offline"}
      class="status-card"
      aria-live="polite"
      aria-busy={refreshing}
    >
      <div class="status-card-head">
        <span class="status-dot" aria-hidden="true"></span>
        <span>GATEWAY STATUS</span>
      </div>
      <strong>{stateLabel()}</strong>
      <p>{stateMessage()}</p>
      <div class="status-meta">
        <span>{checkedLabel()}</span>
        <button type="button" class="refresh" on:click={refresh} disabled={refreshing} aria-busy={refreshing}>
          {refreshing ? "확인 중…" : "새로 확인"}
        </button>
      </div>
    </div>
  </section>

  <section class="metrics" aria-label="게이트웨이 핵심 상태">
    <article class="metric-card accent">
      <span class="metric-label">ACTIVE UPSTREAMS</span>
      <strong>{health.eligible_keys}<small> / 2</small></strong>
      <p>{health.pair_ready ? "분산 라우팅 가능" : health.traffic_ready ? "제한된 용량으로 운영 중" : "관리자 설정 대기"}</p>
    </article>
    <article class="metric-card">
      <span class="metric-label">ROUTING MODE</span>
      <strong>ROUND-ROBIN</strong>
      <p>rate-aware · failover</p>
    </article>
    <article class="metric-card">
      <span class="metric-label">API CONTRACT</span>
      <strong>OPENAI</strong>
      <p>호환 인터페이스</p>
    </article>
  </section>

  <section class="content-grid" aria-label="지원 API와 운영 안내">
    <article class="panel api-panel">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">ONE GATEWAY</p>
          <h2>지원 API</h2>
        </div>
        <span class="panel-count">07</span>
      </div>
      <div class="api-list">
        <div class="api-row"><span class="api-icon">✦</span><span><strong>Chat Completions</strong><small>텍스트 · 멀티모달 · streaming</small></span><code>POST /v1/chat/completions</code></div>
        <div class="api-row"><span class="api-icon">◈</span><span><strong>Embeddings</strong><small>검색·벡터화</small></span><code>POST /v1/embeddings</code></div>
        <div class="api-row"><span class="api-icon">▣</span><span><strong>Image generation</strong><small>텍스트·이미지 기반 생성</small></span><code>POST /v1/images/generations</code></div>
        <div class="api-row"><span class="api-icon">▶</span><span><strong>Video generation</strong><small>이미지 기반 영상 생성</small></span><code>POST /v1/videos/generations</code></div>
        <div class="api-row"><span class="api-icon">◉</span><span><strong>Speech</strong><small>텍스트 음성 합성</small></span><code>POST /v1/audio/speech</code></div>
        <div class="api-row"><span class="api-icon">◌</span><span><strong>Transcription</strong><small>음성 텍스트 변환</small></span><code>POST /v1/audio/transcriptions</code></div>
        <div class="api-row"><span class="api-icon">⌁</span><span><strong>NVIDIA Inference</strong><small>NVCF 호환 호출</small></span><code>POST /v1/nvidia/inference</code></div>
      </div>
    </article>

    <aside class="side-stack">
      <article class="panel trust-panel">
        <p class="eyebrow">BUILT FOR OPERATIONS</p>
        <h2>운영 원칙</h2>
        <ul>
          <li><span>01</span> 키는 암호화 저장하고 화면에 다시 표시하지 않습니다.</li>
          <li><span>02</span> rate limit·quota·transport 오류를 자동으로 격리합니다.</li>
          <li><span>03</span> 재시작 후에도 routing 상태와 evidence를 유지합니다.</li>
        </ul>
      </article>
      <article class="panel operator-panel">
        <span class="panel-kicker">OPERATOR ACCESS</span>
        <h2>관리 콘솔은<br />안전하게 제한됩니다.</h2>
        <p>운영자 화면은 공개 인터넷에 노출하지 않고 서버의 loopback에서만 접근합니다.</p>
        <span class="lock-label"><span aria-hidden="true">●</span> LOOPBACK ONLY</span>
      </article>
    </aside>
  </section>

  <footer>
    <span>NVIDIA BUILD LB · STATUS SURFACE</span>
    <span>자동 새로고침 15초 · {health.status}</span>
  </footer>
</main>

<style>
  :global(*) { box-sizing: border-box; }
  :global(body) { margin: 0; background: #080b0d; color: #f4f7f5; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }
  :global(button) { font: inherit; }
  :global(:focus-visible) { outline: 3px solid #b5e66c; outline-offset: 3px; }
  .skip-link { position: fixed; z-index: 10; top: 8px; left: 8px; transform: translateY(-160%); background: #f4f7f5; color: #080b0d; padding: 12px 16px; font-weight: 800; }
  .skip-link:focus { transform: translateY(0); }
  .shell { max-width: 1240px; margin: 0 auto; padding: 28px clamp(20px, 4vw, 56px) 32px; }
  .topbar, .status-card-head, .status-meta, .panel-heading, .api-row, footer { display: flex; align-items: center; }
  .topbar { justify-content: space-between; gap: 24px; border-bottom: 1px solid #27312e; padding-bottom: 22px; }
  .brand { display: flex; align-items: center; min-height: 44px; gap: 12px; color: inherit; text-decoration: none; }
  .brand-mark { display: grid; place-items: center; width: 36px; height: 36px; background: #76b900; color: #081000; font-size: 20px; font-weight: 900; transform: skew(-10deg); }
  .brand strong, .brand small { display: block; letter-spacing: .1em; }
  .brand strong { font-size: 13px; }
  .brand small, .environment, .eyebrow, .metric-label, .panel-kicker, footer { color: #84928d; font-size: 10px; letter-spacing: .16em; }
  .environment { white-space: nowrap; }
  .hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 390px); gap: clamp(28px, 7vw, 110px); align-items: end; padding: clamp(60px, 10vw, 120px) 0 68px; }
  .eyebrow { color: #76b900; font-weight: 800; margin: 0 0 14px; }
  h1, h2, p { margin-top: 0; }
  h1 { max-width: 700px; margin-bottom: 22px; font-size: clamp(42px, 7vw, 84px); letter-spacing: -.065em; line-height: .98; }
  h1 span { color: #76b900; }
  .lede { max-width: 540px; color: #aab7b1; font-size: clamp(15px, 2vw, 19px); line-height: 1.7; }
  .status-card { border: 1px solid #3e512e; border-radius: 4px; background: linear-gradient(145deg, #182116, #101512); padding: 24px; box-shadow: 0 18px 60px #0008; }
  .status-card.offline { border-color: #814735; background: #211512; }
  .status-card-head { gap: 9px; color: #9eaf9a; font-size: 10px; font-weight: 800; letter-spacing: .14em; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #d89b4b; box-shadow: 0 0 0 5px #d89b4b22; }
  .status-card.ready .status-dot { background: #76b900; box-shadow: 0 0 0 5px #76b90022; }
  .status-card.offline .status-dot { background: #e06650; box-shadow: 0 0 0 5px #e0665022; }
  .status-card > strong { display: block; margin: 24px 0 8px; font-size: 27px; letter-spacing: -.04em; }
  .status-card p { min-height: 48px; color: #c0cac5; font-size: 13px; line-height: 1.6; }
  .status-meta { justify-content: space-between; gap: 12px; border-top: 1px solid #465a41; padding-top: 16px; color: #aab7b1; font-size: 11px; }
  .refresh { min-width: 44px; min-height: 44px; border: 0; background: transparent; color: #9dd54a; cursor: pointer; font-size: 11px; font-weight: 700; }
  .refresh:disabled { cursor: wait; opacity: .55; }
  .metrics { display: grid; grid-template-columns: repeat(3, 1fr); border-block: 1px solid #27312e; }
  .metric-card { min-height: 150px; padding: 24px 26px; border-right: 1px solid #27312e; }
  .metric-card:last-child { border-right: 0; }
  .metric-card.accent { background: #10170d; }
  .metric-label { display: block; margin-bottom: 18px; }
  .metric-card strong { display: block; color: #f2f5f1; font-size: clamp(21px, 3vw, 34px); letter-spacing: -.04em; }
  .metric-card.accent strong { color: #9dd54a; }
  .metric-card strong small { color: #99a895; font-size: .55em; }
  .metric-card p { margin: 8px 0 0; color: #a4b0aa; font-size: 12px; }
  .content-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap: 20px; padding-top: 20px; }
  .side-stack { display: grid; gap: 20px; }
  .panel { border: 1px solid #27312e; background: #0e1314; padding: clamp(22px, 3vw, 32px); }
  .panel-heading { justify-content: space-between; gap: 20px; margin-bottom: 25px; }
  h2 { margin-bottom: 0; font-size: 24px; letter-spacing: -.04em; }
  .panel-count { color: #84928d; font-size: 28px; font-weight: 800; }
  .api-list { border-top: 1px solid #27312e; }
  .api-row { min-height: 71px; gap: 14px; border-bottom: 1px solid #27312e; }
  .api-row > span:nth-child(2) { min-width: 0; flex: 1; }
  .api-icon { display: grid; place-items: center; width: 28px; height: 28px; border: 1px solid #466325; color: #9dd54a; font-size: 15px; }
  .api-row strong, .api-row small { display: block; }
  .api-row strong { font-size: 13px; }
  .api-row small { margin-top: 5px; color: #a4b0aa; font-size: 11px; }
  code { max-width: 48%; color: #9fbaa4; font-size: 10px; overflow-wrap: anywhere; text-align: right; }
  .trust-panel ul { display: grid; gap: 16px; margin: 26px 0 0; padding: 0; list-style: none; }
  .trust-panel li { display: grid; grid-template-columns: 25px 1fr; gap: 9px; color: #a7b3ad; font-size: 12px; line-height: 1.55; }
  .trust-panel li span { color: #76b900; font-weight: 800; }
  .operator-panel { background: #17200f; border-color: #344d1f; }
  .operator-panel h2 { margin: 15px 0; color: #dbeabf; line-height: 1.25; }
  .operator-panel p { color: #aebca4; font-size: 12px; line-height: 1.6; }
  .lock-label { display: inline-block; margin-top: 10px; color: #9dd54a; font-size: 10px; font-weight: 800; letter-spacing: .12em; }
  .lock-label span { margin-right: 7px; }
  footer { justify-content: space-between; gap: 15px; padding-top: 30px; }
  @media (max-width: 780px) {
    .hero, .content-grid { grid-template-columns: 1fr; }
    .hero { padding-top: 64px; }
    .metrics { grid-template-columns: 1fr; }
    .metric-card { min-height: auto; border-right: 0; border-bottom: 1px solid #27312e; }
    .metric-card:last-child { border-bottom: 0; }
    .api-row { align-items: flex-start; flex-wrap: wrap; padding-block: 16px; }
    code { width: 100%; max-width: none; padding-left: 42px; text-align: left; }
  }
  @media (max-width: 480px) {
    .shell { padding-inline: 16px; }
    .environment { display: none; }
    .topbar { padding-bottom: 16px; }
    .hero { padding-block: 52px 48px; }
    h1 { font-size: 48px; }
    .api-row { min-height: 78px; }
    footer { align-items: flex-start; flex-direction: column; line-height: 1.5; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation: none !important; }
  }
</style>
