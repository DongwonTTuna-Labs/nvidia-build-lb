<script lang="ts">
import { onMount, tick } from "svelte";
import { type AdminRouteId, adminRoutes } from "$lib/copy";

type Key = {
  id: string;
  label: string;
  fingerprint: string;
  enabled: boolean;
  cooldown_until: string | null;
  request_count: number;
  failure_count: number;
};
type Client = {
  id: string;
  label: string;
  scopes: string[];
  active: boolean;
  request_count: number;
  revoked_at: string | null;
};

const supportedScopes = [
  ["models:read", "모델 조회"],
  ["chat:write", "대화"],
  ["embeddings:write", "임베딩"],
  ["images:write", "이미지"],
  ["audio:write", "음성"],
  ["media:write", "영상·미디어"],
] as const;

let active: AdminRouteId = "overview";
let adminToken = "";
let ready = false;
let models: string[] = [];
let keys: Key[] = [];
let clients: Client[] = [];
let loading = false;
let error = "";
let notice = "";
let updatedAt = "";
let upstreamLabel = "";
let upstreamCredential = "";
let clientLabel = "";
let clientScopes: string[] = ["models:read", "chat:write"];
let issuedToken = "";
let mutating = false;
let evidence = {
  source_of_truth: "확인 전",
  persisted_upstream_keys: 0,
  persisted_downstream_credentials: 0,
  persisted_routing_profiles: 0,
};

const routeTitle: Record<AdminRouteId, string> = {
  overview: "현재 상태",
  routing: "라우팅 상태",
  clients: "접속 키",
  models: "모델 카탈로그",
  evidence: "확인 기록",
};

function routeFromHash() {
  const candidate = location.hash.slice(1) as AdminRouteId;
  if (adminRoutes.some((route) => route.id === candidate)) active = candidate;
}

async function selectRoute(id: AdminRouteId) {
  active = id;
  history.pushState({}, "", `#${id}`);
  await tick();
  document.getElementById(id)?.focus();
}

async function refresh() {
  loading = true;
  error = "";
  notice = "";
  try {
    const healthResponse = await fetch("/health", { cache: "no-store" });
    const health = await healthResponse.json().catch(() => ({}));
    ready = health.ready === true;
    const response = await fetch("/admin/api/v1/overview", {
      headers: adminToken ? { Authorization: `Bearer ${adminToken}` } : {},
      cache: "no-store",
    });
    if (response.status === 401) throw new Error("관리자 토큰을 입력해야 합니다.");
    if (!response.ok) throw new Error("게이트웨이 상태를 읽지 못했습니다.");
    const snapshot = await response.json();
    keys = snapshot.upstream_keys?.items ?? [];
    clients = snapshot.downstream_credentials?.items ?? [];
    models = snapshot.models ?? [];
    evidence = snapshot.evidence ?? evidence;
    ready = snapshot.runtime?.ready === true;
    updatedAt = new Date().toLocaleTimeString("ko-KR");
  } catch (caught) {
    error = caught instanceof Error ? caught.message : "상태를 읽지 못했습니다.";
    if (!adminToken) ready = false;
  } finally {
    loading = false;
  }
}

async function addUpstream() {
  if (mutating) return;
  mutating = true;
  notice = "";
  error = "";
  try {
    const response = await fetch("/admin/api/v1/upstream-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
      body: JSON.stringify({ label: upstreamLabel.trim(), credential: upstreamCredential }),
    });
    if (!response.ok) {
      error = await responseError(response, "NVIDIA 키를 저장하지 못했습니다.");
      return;
    }
    upstreamLabel = "";
    upstreamCredential = "";
    notice = "NVIDIA 키를 암호화해 저장했습니다.";
    await refresh();
  } finally {
    mutating = false;
  }
}

async function issueClient() {
  if (mutating || clientScopes.length === 0) return;
  mutating = true;
  notice = "";
  error = "";
  try {
    const response = await fetch("/admin/api/v1/downstream-credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
      body: JSON.stringify({ label: clientLabel.trim(), scopes: clientScopes }),
    });
    if (!response.ok) {
      error = await responseError(response, "접속 키를 발급하지 못했습니다.");
      return;
    }
    const body = await response.json();
    issuedToken = body.token ?? "";
    clientLabel = "";
    notice = "접속 키는 지금 한 번만 복사하세요.";
    await refresh();
  } finally {
    mutating = false;
  }
}

async function revokeClient(id: string) {
  if (mutating) return;
  if (!confirm("이 접속 키를 폐기할까요?")) return;
  mutating = true;
  try {
    const response = await fetch(`/admin/api/v1/downstream-credentials/${id}/revoke`, {
      method: "POST",
      headers: { Authorization: `Bearer ${adminToken}` },
    });
    if (!response.ok) {
      error = await responseError(response, "접속 키를 폐기하지 못했습니다.");
      return;
    }
    notice = "접속 키를 폐기했습니다.";
    await refresh();
  } finally {
    mutating = false;
  }
}

async function toggleKey(key: Key) {
  if (mutating) return;
  mutating = true;
  error = "";
  try {
    const response = await fetch(`/admin/api/v1/upstream-keys/${key.id}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
      body: JSON.stringify({ enabled: !key.enabled }),
    });
    if (!response.ok) {
      error = await responseError(response, "라우팅 상태를 바꾸지 못했습니다.");
      return;
    }
    notice = key.enabled ? "키를 라우팅에서 제외했습니다." : "키를 라우팅에 다시 포함했습니다.";
    await refresh();
  } finally {
    mutating = false;
  }
}

async function copyIssuedToken() {
  if (!issuedToken) return;
  try {
    await navigator.clipboard.writeText(issuedToken);
    notice = "접속 키를 클립보드에 복사했습니다. 저장 후 이 화면에서 지우세요.";
  } catch {
    error = "클립보드에 복사하지 못했습니다. 표시된 키를 안전한 곳에 직접 저장하세요.";
  }
}

async function responseError(response: Response, fallback: string) {
  const body = await response.json().catch(() => ({}));
  return body?.error?.message ? `${fallback} ${body.error.message}` : fallback;
}

onMount(() => {
  routeFromHash();
  addEventListener("hashchange", routeFromHash);
  addEventListener("popstate", routeFromHash);
  refresh();
  return () => {
    removeEventListener("hashchange", routeFromHash);
    removeEventListener("popstate", routeFromHash);
  };
});
</script>

<svelte:head><title>{routeTitle[active]} · NVIDIA Build LB</title></svelte:head>

<div class="shell">
  <a class="skip" href="#overview">본문으로 건너뛰기</a>
  <header class="topbar"><div><p class="eyebrow">NVIDIA BUILD LB</p><h1>관리 콘솔</h1></div><span class:good={ready} class="status" aria-live="polite">{ready ? '정상' : '확인 필요'}</span></header>
  <section class="auth" aria-label="관리자 인증"><label for="admin-token">관리자 토큰</label><input id="admin-token" type="password" bind:value={adminToken} autocomplete="off" placeholder="토큰을 입력하면 관리 정보가 표시됩니다" /><button class="secondary" onclick={refresh} disabled={loading}>{loading ? '확인 중…' : '상태 확인'}</button></section>
  <nav aria-label="관리 메뉴" class="nav">
    {#each adminRoutes as route}
      <a
        href={route.href}
        aria-current={active === route.id ? "page" : undefined}
        aria-controls={route.id}
        onclick={(event) => {
          event.preventDefault();
          selectRoute(route.id);
        }}>{route.label}</a
      >
    {/each}
  </nav>
  <main tabindex="-1" aria-busy={loading}>
    <section class="hero"><div><p class="eyebrow">{routeTitle[active]}</p><h2>{ready ? '요청을 받을 준비가 됐습니다' : '상태를 확인하세요'}</h2><p class="muted">모델 {models.length}개 · NVIDIA 키 {keys.length}개 · 마지막 확인 {updatedAt || '아직 없음'}</p></div><button class="primary" onclick={refresh} disabled={loading}>{loading ? '확인 중…' : '새로 확인'}</button></section>
    {#if error}<p class="alert" role="alert">{error}</p>{/if}{#if notice}<p class="notice" role="status">{notice}</p>{/if}
    <section class="grid" aria-label="핵심 판단"><article><span>게이트웨이</span><strong>{ready ? '검증 완료' : '확인 필요'}</strong><small>{ready ? '구조적 상태와 키 상태를 확인했습니다.' : '관리자 토큰과 키 상태를 확인하세요.'}</small></article><article><span>라우팅 슬롯</span><strong>{keys.filter((key) => key.enabled).length}개 활성</strong><small>rate-aware round-robin · 실패 키는 cooldown</small></article><article><span>다음 행동</span><strong>{ready ? '없음' : '상태 다시 확인'}</strong><small>필요한 조치만 표시합니다.</small></article></section>

    <section id="overview" class="panel" tabindex="-1" hidden={active !== 'overview'}><h3>현재 상태</h3><p>운영에 필요한 판단만 한 화면에 모았습니다.</p><div class="facts"><span>활성 키 <strong>{keys.filter((key) => key.enabled).length}/{keys.length}</strong></span><span>사용 가능 모델 <strong>{models.length}</strong></span><span>접속 키 <strong>{clients.filter((client) => client.active).length}</strong></span></div></section>
    <section id="routing" class="panel" tabindex="-1" hidden={active !== "routing"}>
      <h3>라우팅 상태</h3>
      <p>각 키의 상태와 실패 누적만 보여줍니다. 중지한 키는 새 요청에서 제외됩니다.</p>
      <div class="table">
        {#each keys as key}
          <div class="row">
            <span><strong>{key.label}</strong><small>{key.fingerprint}</small></span>
            <span>{key.enabled ? "활성" : "중지"}{#if key.cooldown_until}<small>cooldown 중</small>{/if}</span>
            <span>요청 {key.request_count} · 실패 {key.failure_count}</span>
            <button class="secondary" onclick={() => toggleKey(key)} disabled={!adminToken || mutating}>
              {key.enabled ? "라우팅 제외" : "라우팅 포함"}
            </button>
          </div>
        {:else}
          <p class="muted">저장된 NVIDIA 키가 없습니다.</p>
        {/each}
      </div>
      <div id="upstream-form" class="subpanel">
        <h3>NVIDIA 키 추가</h3>
        <p>값은 저장 직후 화면에서 사라지고 암호화 vault에만 남습니다.</p>
        <div class="form">
          <label for="upstream-label">라벨</label>
          <input id="upstream-label" bind:value={upstreamLabel} maxlength="128" placeholder="예: nvidia-primary" />
          <label for="upstream-credential">NVIDIA API 키</label>
          <input id="upstream-credential" type="password" bind:value={upstreamCredential} autocomplete="off" placeholder="nvapi-…" />
          <button class="primary" onclick={addUpstream} disabled={!adminToken || !upstreamLabel.trim() || !upstreamCredential || mutating}>
            {mutating ? "저장 중…" : "암호화 저장"}
          </button>
        </div>
      </div>
    </section>
    <section id="clients" class="panel" tabindex="-1" hidden={active !== "clients"}>
      <h3>접속 키</h3>
      <p>새 키는 발급 순간에만 표시됩니다. 필요한 권한만 선택하고 사용이 끝난 키는 즉시 폐기하세요.</p>
      <div class="form">
        <label for="client-label">라벨</label>
        <input id="client-label" bind:value={clientLabel} maxlength="128" placeholder="예: hermes" />
        <fieldset>
          <legend>필요한 권한</legend>
          {#each supportedScopes as [scope, label]}
            <label class="check"><input type="checkbox" value={scope} bind:group={clientScopes} /> {label} <small>{scope}</small></label>
          {/each}
        </fieldset>
        <button class="primary" onclick={issueClient} disabled={!adminToken || !clientLabel.trim() || clientScopes.length === 0 || mutating}>접속 키 발급</button>
      </div>
      {#if issuedToken}
        <div class="secret-wrap" role="status">
          <p><strong>지금만 표시되는 접속 키</strong> · 안전한 비밀 저장소에 보관한 뒤 화면에서 지우세요.</p>
          <pre class="secret" aria-label="일회성 접속 키">{issuedToken}</pre>
          <div class="actions"><button class="secondary" onclick={copyIssuedToken}>클립보드에 복사</button><button class="danger" onclick={() => (issuedToken = "")}>화면에서 지우기</button></div>
        </div>
      {/if}
      <div class="table">
        {#each clients as client}
          <div class="row">
            <span><strong>{client.label}</strong><small>{client.request_count}회 사용</small></span>
            <span>{client.active ? client.scopes.join(", ") : "폐기됨"}</span>
            {#if client.active}<button class="danger" onclick={() => revokeClient(client.id)} disabled={mutating}>폐기</button>{/if}
          </div>
        {:else}<p class="muted">발급된 접속 키가 없습니다.</p>{/each}
      </div>
    </section>
    <section id="models" class="panel" tabindex="-1" hidden={active !== "models"}><h3>모델 카탈로그</h3><p>텍스트·이미지·영상·음성·임베딩 요청에 사용하는 NVIDIA 프로필입니다.</p><div class="chips">{#each models as model}<span>{model}</span>{:else}<span>관리자 인증 후 모델을 확인하세요.</span>{/each}</div></section>
    <section id="evidence" class="panel" tabindex="-1" hidden={active !== "evidence"}>
      <h3>확인 기록</h3>
      <p>이 화면의 수치는 마지막 snapshot과 PostgreSQL 확인 시각 기준입니다.</p>
      <div class="facts"><span>마지막 확인 <strong>{updatedAt || "없음"}</strong></span><span>오류 <strong>{error ? "있음" : "없음"}</strong></span><span>상태 <strong>{ready ? "정상" : "확인 필요"}</strong></span></div>
      <div class="facts"><span>저장 원본 <strong>{evidence.source_of_truth}</strong></span><span>DB 키 <strong>{evidence.persisted_upstream_keys}</strong></span><span>DB 접속 키 <strong>{evidence.persisted_downstream_credentials}</strong></span><span>라우팅 프로필 <strong>{evidence.persisted_routing_profiles}</strong></span></div>
    </section>
  </main>
</div>

<style>
  :global(*) { box-sizing: border-box; } :global(body) { margin: 0; background: #f5f7fa; color: #17202b; font-family: Inter, ui-sans-serif, system-ui, sans-serif; } :global(button:focus-visible), :global(a:focus-visible), :global(input:focus-visible), :global([tabindex]:focus-visible) { outline: 3px solid #76b900; outline-offset: 3px; }
  .shell { min-height: 100vh; max-width: 1180px; margin: 0 auto; padding: max(24px, env(safe-area-inset-top)) 24px 48px; } .skip { position: absolute; left: -9999px; } .skip:focus { left: 16px; top: 12px; background: #fff; padding: 8px; z-index: 2; }
  .topbar { display: flex; align-items: start; justify-content: space-between; gap: 24px; padding: 20px 0; } h1, h2, h3, p { margin: 0; } h1 { font-size: clamp(1.4rem, 3vw, 2rem); } h2 { font-size: clamp(1.4rem, 3vw, 2.3rem); line-height: 1.15; } h3 { font-size: 1.25rem; }
  .eyebrow { color: #76b900; font-size: .72rem; font-weight: 800; letter-spacing: .12em; } .muted, small { color: #657386; } .status { border-radius: 999px; background: #fff0f0; color: #a12a2a; padding: 8px 14px; font-weight: 700; } .status.good { background: #edf8df; color: #427500; }
  .auth { display: flex; align-items: end; gap: 10px; padding: 12px 0; } label { display: grid; gap: 6px; color: #526273; font-size: .85rem; font-weight: 700; } input { min-height: 42px; border: 1px solid #c8d2dd; border-radius: 9px; padding: 9px 11px; font: inherit; background: #fff; } .auth label { flex: 1; }
  .nav { display: flex; gap: 8px; overflow-x: auto; border-bottom: 1px solid #dce3eb; } .nav a { color: #526273; padding: 14px 12px; text-decoration: none; white-space: nowrap; } .nav a[aria-current='page'] { color: #17202b; border-bottom: 3px solid #76b900; font-weight: 800; }
  .hero { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin: 42px 0 24px; padding: 28px; background: #fff; border: 1px solid #e0e6ed; border-radius: 18px; box-shadow: 0 12px 34px #1823320d; } .hero p { margin-top: 10px; }
  button { border: 0; border-radius: 10px; cursor: pointer; font: inherit; } button:disabled { opacity: .5; cursor: not-allowed; } .primary { padding: 12px 18px; background: #76b900; color: #102000; font-weight: 800; } .secondary { min-height: 42px; padding: 9px 14px; background: #e8edf2; color: #17202b; font-weight: 700; } .danger { padding: 7px 11px; background: #ffe8e8; color: #9b2525; }
  .alert, .notice { margin: 12px 0; padding: 12px 14px; border-radius: 10px; } .alert { background: #fff0f0; color: #9b2525; } .notice { background: #edf8df; color: #427500; }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; } article, .panel { display: grid; gap: 10px; padding: 22px; background: #fff; border: 1px solid #e0e6ed; border-radius: 14px; } article span { color: #657386; font-size: .85rem; } article strong { font-size: 1.4rem; }
  .panel { margin-top: 18px; } .facts, .chips { display: flex; flex-wrap: wrap; gap: 10px; } .facts span, .chips span { padding: 9px 12px; background: #f1f4f7; border-radius: 9px; } .facts strong { margin-left: 6px; } .table { display: grid; gap: 8px; } .row { display: grid; grid-template-columns: 1.2fr 1fr 1.5fr auto; align-items: center; gap: 10px; padding: 12px; border-bottom: 1px solid #edf0f3; } .form { display: grid; gap: 9px; max-width: 560px; } .secret { overflow: auto; padding: 12px; background: #17202b; color: #e7ffb5; border-radius: 8px; }
  .row span { display: grid; gap: 3px; } .row small { color: #657386; font-size: .76rem; } .subpanel, fieldset { display: grid; gap: 10px; margin-top: 18px; padding: 16px; border: 1px solid #e0e6ed; border-radius: 10px; background: #f8fafb; } fieldset { margin: 0; } legend { padding: 0 4px; color: #526273; font-weight: 800; } .check { display: flex; align-items: center; gap: 8px; font-weight: 600; } .check input { min-height: auto; inline-size: auto; } .secret-wrap { display: grid; gap: 8px; margin: 18px 0; } .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  @media (max-width: 700px) { .shell { padding-inline: 16px; } .auth, .hero { align-items: stretch; flex-direction: column; } .grid { grid-template-columns: 1fr; } .primary, .secondary { width: 100%; } .row { grid-template-columns: 1fr; gap: 6px; } }
  @media (prefers-reduced-motion: reduce) { :global(*) { scroll-behavior: auto !important; transition-duration: .01ms !important; } }
</style>
