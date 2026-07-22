<script lang="ts">
import { onDestroy, onMount } from "svelte";
import { ApiError, adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import PageHeader from "$lib/components/PageHeader.svelte";
import PaginationButton from "$lib/components/PaginationButton.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { QaRun, QaRunsPage } from "$lib/types";

const suites = [
  {
    id: "smoke",
    title: "기본 호출",
    copy: "readiness, model, chat stream/non-stream",
  },
  {
    id: "distribution",
    title: "분산",
    copy: "6회 요청, 양 slot 사용, skew ≤ 1",
  },
  {
    id: "failover",
    title: "장애 전환",
    copy: "QA 전용 1회 fixture, 첫 frame 전 failover와 이후 no-replay",
  },
  {
    id: "persistence",
    title: "지속성",
    copy: "암호문, receipt, cursor, owner lease",
  },
  {
    id: "multimodal",
    title: "멀티모달",
    copy: "image, embedding, generation, video, speech, transcription",
  },
  {
    id: "hermes-e2e",
    title: "Hermes E2E",
    copy: "doctor, marker, tool task, request correlation",
  },
];

let live = $state(false);
let confirmBillable = $state(false);
let current = $state<QaRun | null>(null);
let recent = $state<QaRun[]>([]);
let historyCursor = $state<string | null>(null);
let loadingMore = $state(false);
let completion = $state<QaRunsPage["completion"]>([]);
let deploymentCommit = $state("");
let error = $state("");
let actionError = $state("");
let actionRetrySuite = $state("");
let notice = $state("");
let busy = $state(false);
let timer: number | undefined;
let retryDelay = 1000;
let pollingRunId = "";
let activeRunId = "";
let viewGeneration = 0;
let stateRequestEpoch = 0;
let destroyed = false;
let lastSuccessAt = $state<string | null>(null);
let recentStale = $state(false);
let refreshingRecent = $state(false);
let invalidRun = $state("");
const helperCommitReady = () => /^[0-9a-f]{40}$/.test(deploymentCommit);

function beginView(runId = ""): number {
  viewGeneration += 1;
  stateRequestEpoch += 1;
  activeRunId = runId;
  pollingRunId = "";
  refreshingRecent = false;
  loadingMore = false;
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
  return viewGeneration;
}

function isCurrentView(runId: string, generation: number): boolean {
  return !destroyed && generation === viewGeneration && runId === activeRunId;
}

function rememberRun(id: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("run", id);
  window.history.replaceState({}, "", url);
}

async function poll(id: string, generation = viewGeneration): Promise<void> {
  if (!isCurrentView(id, generation)) return;
  const requestEpoch = ++stateRequestEpoch;
  const isCurrentRequest = () =>
    isCurrentView(id, generation) && requestEpoch === stateRequestEpoch;
  try {
    const value = await api<QaRun>(`/qa/runs/${id}`);
    if (!isCurrentRequest()) return;
    current = value;
    invalidRun = "";
    recent = [current, ...recent.filter((item) => item.id !== current?.id)];
    error = "";
    retryDelay = 1000;
    lastSuccessAt = new Date().toISOString();
    recentStale = false;
    if (["queued", "running"].includes(current.status)) {
      busy = true;
      timer = window.setTimeout(() => void poll(id, generation), retryDelay);
    } else {
      busy = false;
      pollingRunId = "";
      void loadRecent(generation);
    }
  } catch (caught) {
    if (!isCurrentRequest()) return;
    if (caught instanceof ApiError && [404, 422].includes(caught.status)) {
      invalidRun = id;
      current = null;
      busy = false;
      pollingRunId = "";
      error = "";
      return;
    }
    error = `${adminErrorMessage(caught, "QA 상태를 읽지 못했습니다.")} 자동으로 다시 연결합니다.`;
    recentStale = recent.length > 0;
    retryDelay = Math.min(retryDelay * 2, 15_000);
    timer = window.setTimeout(() => void poll(id, generation), retryDelay);
  }
}

async function loadRecent(generation = viewGeneration): Promise<void> {
  if (destroyed || generation !== viewGeneration) return;
  const requestEpoch = ++stateRequestEpoch;
  const isCurrentRequest = () =>
    !destroyed && generation === viewGeneration && requestEpoch === stateRequestEpoch;
  if (timer !== undefined) {
    window.clearTimeout(timer);
    timer = undefined;
  }
  let restartPollId = "";
  refreshingRecent = true;
  try {
    const targetCount = Math.max(50, recent.length);
    const page = await api<QaRunsPage>("/qa/runs?limit=50");
    if (!isCurrentRequest()) return;
    const loaded = [...page.items];
    let nextBefore = page.next_before ?? null;
    while (nextBefore && loaded.length < targetCount) {
      const next = await api<QaRunsPage>(
        `/qa/runs?limit=50&before=${encodeURIComponent(nextBefore)}`,
      );
      if (!isCurrentRequest()) return;
      for (const run of next.items) {
        if (!loaded.some((item) => item.id === run.id)) loaded.push(run);
      }
      nextBefore = next.next_before ?? null;
    }
    recent = loaded;
    historyCursor = nextBefore;
    completion = page.completion;
    deploymentCommit = page.deployment_commit;
    lastSuccessAt = page.snapshot.observed_at;
    recentStale = page.snapshot.stale;
    const requested = new URL(window.location.href).searchParams.get("run");
    let requestedRun = requested ? recent.find((item) => item.id === requested) : undefined;
    if (requested && !requestedRun && requested !== invalidRun) {
      try {
        requestedRun = await api<QaRun>(`/qa/runs/${requested}`);
        if (!isCurrentRequest()) return;
        recent = [requestedRun, ...recent];
      } catch (caught) {
        if (!isCurrentRequest()) return;
        if (caught instanceof ApiError && [404, 422].includes(caught.status)) {
          invalidRun = requested;
          current = null;
          busy = false;
          pollingRunId = "";
          error = "";
          return;
        }
        throw caught;
      }
    }
    const selected =
      requestedRun ??
      (requested
        ? undefined
        : (recent.find((item) => ["queued", "running"].includes(item.status)) ??
          current ??
          recent[0]));
    if (selected) {
      invalidRun = "";
      current = selected;
      activeRunId = selected.id;
      rememberRun(selected.id);
      busy = ["queued", "running"].includes(selected.status);
      if (!busy) pollingRunId = "";
      if (busy) {
        pollingRunId = selected.id;
        restartPollId = selected.id;
      }
    }
    error = "";
  } catch (caught) {
    if (!isCurrentRequest()) return;
    error = adminErrorMessage(caught, "최근 QA 실행을 읽지 못했습니다.");
    recentStale = recent.length > 0;
    if (current && ["queued", "running"].includes(current.status)) {
      pollingRunId = current.id;
      restartPollId = current.id;
    }
  } finally {
    if (isCurrentRequest()) {
      refreshingRecent = false;
      if (restartPollId) void poll(restartPollId, generation);
    }
  }
}

async function loadMoreHistory(): Promise<void> {
  if (!historyCursor || loadingMore) return;
  const generation = viewGeneration;
  const cursor = historyCursor;
  loadingMore = true;
  try {
    const page = await api<QaRunsPage>(`/qa/runs?limit=50&before=${encodeURIComponent(cursor)}`);
    if (destroyed || generation !== viewGeneration || cursor !== historyCursor) return;
    recent = [...recent, ...page.items.filter((run) => !recent.some((item) => item.id === run.id))];
    historyCursor = page.next_before ?? null;
  } catch (caught) {
    if (destroyed || generation !== viewGeneration) return;
    error = adminErrorMessage(caught, "이전 QA 실행을 더 불러오지 못했습니다.");
    recentStale = recent.length > 0;
  } finally {
    if (!destroyed && generation === viewGeneration) loadingMore = false;
  }
}

function selectRun(run: QaRun): void {
  const generation = beginView(run.id);
  invalidRun = "";
  current = run;
  actionError = "";
  actionRetrySuite = "";
  notice = "";
  rememberRun(run.id);
  busy = ["queued", "running"].includes(run.status);
  if (busy) {
    pollingRunId = run.id;
    void poll(run.id, generation);
  }
}

function showRecentRuns(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("run");
  window.history.replaceState({}, "", url);
  invalidRun = "";
  error = "";
  actionError = "";
  actionRetrySuite = "";
  notice = "";
  const next = recent.find((item) => ["queued", "running"].includes(item.status)) ?? recent[0];
  if (next) selectRun(next);
  else current = null;
}

function completionFor(suite: string): QaRunsPage["completion"][number] | undefined {
  return completion.find((item) => item.suite === suite);
}

async function run(suite: string): Promise<void> {
  if (!live && suite === "hermes-e2e") {
    actionError =
      "Hermes E2E는 실제 cutover 증거가 필요합니다. 상단에서 Live 모드를 선택한 뒤 다시 실행하세요.";
    actionRetrySuite = "";
    return;
  }
  if (live && suite === "multimodal" && !confirmBillable) {
    actionError = "Live 멀티모달 카드에서 provider 비용 확인을 선택한 뒤 다시 실행하세요.";
    actionRetrySuite = "";
    return;
  }
  const generation = beginView();
  busy = true;
  error = "";
  actionError = "";
  actionRetrySuite = "";
  notice = "";
  invalidRun = "";
  current = null;
  try {
    const value = await api<{ item: QaRun }>("/qa/runs", {
      method: "POST",
      body: JSON.stringify({
        suite,
        live,
        confirm_billable: live && suite === "multimodal" && confirmBillable,
      }),
    });
    if (destroyed || generation !== viewGeneration) return;
    activeRunId = value.item.id;
    current = value.item;
    recent = [value.item, ...recent.filter((item) => item.id !== value.item.id)];
    rememberRun(value.item.id);
    pollingRunId = value.item.id;
    await poll(value.item.id, generation);
  } catch (caught) {
    if (destroyed || generation !== viewGeneration) return;
    if (caught instanceof ApiError && caught.status === 409 && caught.code === "qa_run_active") {
      const activeId = caught.details?.active_run_id;
      if (typeof activeId === "string") {
        activeRunId = activeId;
        try {
          const existing = await api<QaRun>(`/qa/runs/${activeId}`);
          if (!isCurrentView(activeId, generation)) return;
          current = existing;
          recent = [existing, ...recent.filter((item) => item.id !== existing.id)];
          rememberRun(existing.id);
          busy = ["queued", "running"].includes(existing.status);
          notice = "이미 실행 중인 QA run으로 이동했습니다.";
          if (busy) {
            pollingRunId = existing.id;
            await poll(existing.id, generation);
          }
          return;
        } catch (loadError) {
          if (!isCurrentView(activeId, generation)) return;
          actionError = adminErrorMessage(loadError, "실행 중인 QA run을 읽지 못했습니다.");
          actionRetrySuite = suite;
          busy = false;
          return;
        }
      }
    }
    actionError = adminErrorMessage(caught, "QA 실행을 시작하지 못했습니다.");
    actionRetrySuite = suite;
    busy = false;
    pollingRunId = "";
  }
}

onDestroy(() => {
  destroyed = true;
  beginView();
});
onMount(() => {
  destroyed = false;
  return startPolling(() => loadRecent(viewGeneration));
});
</script>

<PageHeader
  eyebrow="OPERATIONS"
  title="QA runs"
  description="suite를 실행하고 terminal 상태와 case별 scalar evidence까지 확인합니다."
/>

{#if error}
  <p role={recentStale ? "status" : "alert"} class="error">
    {error}{#if recentStale}
      · 마지막 확인 {displayTime(lastSuccessAt)}{/if}
    <button
      type="button"
      onclick={() => void loadRecent(viewGeneration)}
      disabled={refreshingRecent}>{refreshingRecent ? "다시 확인 중…" : "다시 시도"}</button
    >
  </p>
{/if}

{#if actionError}
  <p role="alert" class="error">
    {actionError}
    {#if actionRetrySuite}<button
        type="button"
        onclick={() => void run(actionRetrySuite)}
        disabled={busy}>실행 다시 시도</button
      >{/if}
  </p>
{/if}

{#if notice}<p role="status" class="notice">{notice}</p>{/if}

{#if recentStale && !error}
  <p role="status" class="stale">
    마지막 정상 snapshot을 표시 중입니다 · 마지막 확인 {displayTime(lastSuccessAt)}
    <button
      type="button"
      onclick={() => void loadRecent(viewGeneration)}
      disabled={refreshingRecent}>{refreshingRecent ? "다시 확인 중…" : "다시 확인"}</button
    >
  </p>
{/if}

{#if invalidRun}
  <section class="invalid-run" role="alert">
    <strong>QA run을 찾을 수 없습니다.</strong>
    <span
      ><code>{invalidRun}</code>은 삭제되었거나 올바른 run ID가 아닙니다.</span
    >
    <button type="button" onclick={showRecentRuns}>최근 실행 보기</button>
  </section>
{/if}

<section class="mode">
  <label
    ><input
      type="radio"
      name="mode"
      checked={!live}
      onchange={() => (live = false)}
    /> Fake/mock 검증</label
  >
  <label
    ><input
      type="radio"
      name="mode"
      checked={live}
      onchange={() => (live = true)}
    /> Live NVIDIA 검증</label
  >
  {#if live}
    <p>
      Live는 실제 provider 요청을 생성합니다. failover suite는 이 run의 임시
      client에만 연결된 1회 fixture로 첫 전송 실패와 첫 frame 이후 종료를 만들고
      request/key 상관 evidence를 남깁니다.
    </p>
    <label class="confirm"
      ><input type="checkbox" bind:checked={confirmBillable} /> 멀티모달 image/video
      최소 호출 비용을 확인했습니다.</label
    >
  {/if}
</section>

<aside class="external">
  <strong>외부 작업이 필요한 suite</strong>
  <span
    >지속성은 run이 running이 된 뒤 외부에서 app container를 실제 재시작해야
    완료됩니다. Hermes E2E는 root 전용 Rust helper가 doctor, marker, tool,
    request correlation과 rollback을 수행하고 이 run에 evidence를 제출해야
    완료됩니다.</span
  >
</aside>

<section class="completion" aria-label="Live QA 완료 현황">
  <h2>현재 배포 Live 완료 현황</h2>
  {#if !lastSuccessAt}
    <p role="status" aria-busy="true">
      권위 있는 QA 상태와 배포 identity를 확인한 뒤 실행할 수 있습니다.
    </p>
  {:else}
    <p><code>{deploymentCommit}</code></p>
    <ul>
    {#each suites as suite}
      {@const state = completionFor(suite.id)}
      <li>
        <div>
          <strong>{suite.title}</strong><small
            >{state?.passed_run
              ? `통과 ${displayTime(state.passed_run.finished_at ?? state.passed_run.created_at)}`
              : "통과 증거 없음"}{#if state?.latest_run && state.latest_run.id !== state.passed_run?.id}
              · 최근 {state.latest_run.status}
              {displayTime(
                state.latest_run.finished_at ?? state.latest_run.created_at,
              )}{/if}</small
          >
        </div>
        <StatusBadge
          value={state?.passed
            ? "passed"
            : (state?.latest_run?.status ?? "not_run")}
        />
        <div class="evidence-actions">
          {#if state?.latest_run}<button
              type="button"
              onclick={() => selectRun(state.latest_run!)}
              >최근 실행 보기</button
            >{/if}
          {#if state?.passed_run && state.passed_run.id !== state.latest_run?.id}<button
              type="button"
              onclick={() => selectRun(state.passed_run!)}
              >통과 증거 보기</button
            >{/if}
        </div>
      </li>
    {/each}
    </ul>
  {/if}
</section>

{#if recent.length}
  <section class="history" aria-label="최근 QA 실행">
    <h2>최근 실행</h2>
    <ul>
      {#each recent as run}
        <li>
          <button
            type="button"
            class:active={current?.id === run.id}
            onclick={() => selectRun(run)}
          >
            <span
              ><strong
                >{suites.find((suite) => suite.id === run.suite)?.title ??
                  run.suite}</strong
              ><small
                >{run.live ? "LIVE" : "FAKE"} · {displayTime(
                  run.finished_at ?? run.created_at,
                )}</small
              ></span
            >
            <StatusBadge value={run.status} />
          </button>
        </li>
      {/each}
    </ul>
  </section>
  <PaginationButton
    cursor={historyCursor}
    busy={loadingMore}
    loadMore={() => void loadMoreHistory()}
  />
{/if}

<div class="grid">
  {#each suites as suite}
    <article>
      <h2>{suite.title}</h2>
      <p>{suite.copy}</p>
      <button
        class="primary"
        onclick={() => void run(suite.id)}
        disabled={busy || !lastSuccessAt || (!live && suite.id === "hermes-e2e")}
      >
        {busy && current?.suite === suite.id
          ? "실행 중…"
          : `${live ? "Live" : "Fake"} 검증 시작`}
      </button>
      {#if !live && suite.id === "hermes-e2e"}<small
          >Hermes E2E는 실제 agent cutover를 검증하므로 Live 모드에서만 실행할 수
          있습니다.</small
        >{/if}
    </article>
  {/each}
</div>

{#if current?.suite === "persistence" && current.status === "running"}
  <aside class="next" role="status">
    <strong>다음 행동 · app을 한 번 재시작</strong>
    <code
      >cd /home/dongwonttuna/Documents/Programming/home-server-infra &amp;&amp;
      sudo docker compose --env-file stacks/nvidia-build-lb/.env -f
      stacks/nvidia-build-lb/compose.yaml restart app</code
    >
    <span
      >새 owner lease가 시작되면 이 화면이 hash와 cursor 보존 여부를 자동
      판정합니다.</span
    >
  </aside>
{/if}

{#if current?.suite === "hermes-e2e" && current.status === "running"}
  <aside class="next" role="status">
    <strong>다음 행동 · 이 run ID로 root helper 실행</strong>
    {#if helperCommitReady()}
      <code
        >sudo NBLB_EXPECTED_COMMIT={deploymentCommit}
        /usr/local/sbin/nblb-hermes-cutover apply --qa-run {current.id}</code
      >
    {:else}
      <span
        >현재 app commit을 확인하지 못해 helper 명령을 만들 수 없습니다. QA
        상태를 다시 불러온 뒤 실행하세요.</span
      >
    {/if}
    <span
      >30분 안에 완료되지 않거나 gateway가 재시작되면 run은 안전하게 failed로
      닫힙니다.</span
    >
  </aside>
{/if}

{#if current}
  <section class="run" aria-live="polite">
    <div class="run-head">
      <div>
        <p>{current.suite} · {current.live ? "LIVE" : "FAKE"}</p>
        <h2>{current.id}</h2>
        <small
          >시작 {displayTime(current.started_at ?? current.created_at)} · 종료 {displayTime(
            current.finished_at,
          )}</small
        >
      </div>
      <StatusBadge value={current.status} />
    </div>
    <ol>
      {#each current.cases as item}
        <li>
          <div>
            <strong>{item.name}</strong><small
              >{displayTime(item.finished_at)}</small
            >
          </div>
          <StatusBadge value={item.status} />
          <dl>
            {#each Object.entries(item.evidence) as [key, value]}
              <div>
                <dt>{key}</dt>
                <dd>{String(value)}</dd>
              </div>
            {/each}
          </dl>
        </li>
      {/each}
    </ol>
  </section>
{/if}

<style>
  .mode,
  .grid article,
  .run,
  .external,
  .next,
  .completion {
    padding: 18px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .mode {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .mode label {
    display: flex;
    align-items: center;
    min-height: 44px;
    gap: 6px;
    padding: 7px 8px;
  }
  .mode input {
    min-height: auto;
  }
  .mode p {
    width: 100%;
    margin: 0;
    color: #ffd08a;
  }
  .confirm {
    width: 100%;
  }
  .external,
  .next {
    display: grid;
    gap: 6px;
    margin-bottom: 12px;
  }
  .external span,
  .next span {
    color: #aeb7b2;
  }
  .next {
    margin: 14px 0 0;
    border-left: 4px solid #f1b454;
  }
  .next code {
    padding: 8px;
    background: #0d1110;
    overflow-wrap: anywhere;
  }
  .completion {
    margin-bottom: 12px;
  }
  .completion h2 {
    margin: 0;
  }
  .completion > p {
    color: #aeb7b2;
    overflow-wrap: anywhere;
  }
  .completion ul {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    list-style: none;
    padding: 0;
    margin-bottom: 0;
  }
  .completion li {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px;
    padding: 10px;
    background: #101415;
  }
  .completion small {
    display: block;
    color: #aeb7b2;
  }
  .evidence-actions {
    grid-column: 1/-1;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .history,
  .invalid-run {
    padding: 18px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
    margin-bottom: 12px;
  }
  .invalid-run {
    display: grid;
    gap: 8px;
    border-color: #d99b48;
  }
  .invalid-run code {
    overflow-wrap: anywhere;
  }
  .invalid-run button {
    justify-self: start;
  }
  .history h2 {
    margin-top: 0;
  }
  .history ul {
    display: grid;
    gap: 6px;
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .history button {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    text-align: left;
  }
  .history button.active {
    border-color: #76b900;
    background: #1d2915;
  }
  .history small {
    display: block;
    color: #aeb7b2;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
  }
  .grid article {
    display: flex;
    align-items: flex-start;
    flex-direction: column;
  }
  .grid h2 {
    margin: 0;
  }
  .grid p {
    color: #aeb7b2;
    flex: 1;
  }
  .error {
    padding: 12px;
    border: 1px solid #d65e5e;
  }
  .run {
    margin-top: 14px;
  }
  .run-head {
    display: flex;
    justify-content: space-between;
    gap: 20px;
  }
  .run-head p,
  .run-head h2 {
    margin: 0;
  }
  .run-head h2 {
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }
  .run-head small,
  .run li small {
    color: #aeb7b2;
  }
  .run ol {
    list-style: none;
    padding: 0;
  }
  .run li {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
    padding: 14px 0;
    border-top: 1px solid #3f4843;
  }
  .run li small {
    display: block;
  }
  .run dl {
    grid-column: 1/-1;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0;
  }
  .run dl div {
    padding: 7px 9px;
    background: #101415;
  }
  .run dt {
    color: #8f9a94;
    font-size: 0.7rem;
  }
  .run dd {
    margin: 3px 0 0;
  }
  @media (max-width: 850px) {
    .grid,
    .completion ul {
      grid-template-columns: 1fr 1fr;
    }
  }
  @media (max-width: 520px) {
    .grid,
    .completion ul {
      grid-template-columns: 1fr;
    }
    .run-head {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
