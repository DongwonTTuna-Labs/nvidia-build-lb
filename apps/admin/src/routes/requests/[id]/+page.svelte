<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { page } from "$app/state";
import { ApiError, adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { ProxyRequest } from "$lib/types";

let item = $state<ProxyRequest | null>(null),
  loading = $state(true),
  error = $state(""),
  lastSuccessAt = $state<string | null>(null),
  notFound = $state(false);
let activeRouteId = "";
let routeEpoch = 0;
let loadEpoch = 0;
function resetForRoute(id: string): number {
  activeRouteId = id;
  routeEpoch += 1;
  loadEpoch += 1;
  item = null;
  loading = true;
  error = "";
  lastSuccessAt = null;
  notFound = false;
  return routeEpoch;
}
async function load(id = activeRouteId, epoch = routeEpoch): Promise<void> {
  if (!id || epoch !== routeEpoch || id !== activeRouteId) return;
  const requestEpoch = ++loadEpoch;
  loading = !item;
  if (!item) error = "";
  try {
    const value = await api<ProxyRequest>(`/requests/${id}`);
    if (epoch !== routeEpoch || id !== activeRouteId || requestEpoch !== loadEpoch) return;
    item = value;
    lastSuccessAt = new Date().toISOString();
    error = "";
    notFound = false;
  } catch (e) {
    if (epoch !== routeEpoch || id !== activeRouteId || requestEpoch !== loadEpoch) return;
    if (e instanceof ApiError && [404, 422].includes(e.status)) {
      notFound = true;
      error = "";
    } else error = adminErrorMessage(e, "요청 상세를 읽지 못했습니다.");
  } finally {
    if (epoch === routeEpoch && id === activeRouteId && requestEpoch === loadEpoch) loading = false;
  }
}
$effect(() => {
  const id = page.params.id;
  if (!id || id === activeRouteId) return;
  const epoch = resetForRoute(id);
  void load(id, epoch);
});
onMount(() => {
  const stop = startPolling(load, 30_000, false);
  return () => {
    routeEpoch += 1;
    stop();
  };
});
</script>

<a class="back" href={`${base}/requests`}>← 요청 목록</a><PageHeader
  title="요청 상세"
  description="prompt와 body 없이 routing, timing, first-frame boundary를 판정합니다."
/>{#if notFound}<section class="missing" role="alert">
    <strong>요청 evidence를 찾을 수 없습니다.</strong><span
      >보존 기간이 지났거나 URL이 올바르지 않습니다.</span
    ><a href={`${base}/requests`}>요청 목록으로 돌아가기</a>
  </section>{:else}<DataState
    {loading}
    {error}
    hasData={item !== null}
    {lastSuccessAt}
    retry={load}
  />{/if}{#if item && !notFound}<section class="summary">
    <div class="heading">
      <div>
        <code>{item.request_id}</code>
        <h2>{item.profile_id}</h2>
      </div>
      <StatusBadge value={item.outcome} />
    </div>
    <dl>
      <div>
        <dt>Endpoint / modality</dt>
        <dd>{item.endpoint} · {item.modality}</dd>
      </div>
      <div>
        <dt>Downstream client</dt>
        <dd><code>{item.client_id ?? "anonymous"}</code></dd>
      </div>
      <div>
        <dt>전송 방식</dt>
        <dd>{item.stream ? "SSE stream" : "non-stream"}</dd>
      </div>
      <div>
        <dt>HTTP / 오류</dt>
        <dd>{item.status_code ?? "—"} / {item.error_class ?? "없음"}</dd>
      </div>
      <div>
        <dt>Duration / TTFB</dt>
        <dd>{item.duration_ms ?? "—"}ms / {item.ttfb_ms ?? "—"}ms</dd>
      </div>
      <div>
        <dt>Failover</dt>
        <dd>{item.failover_count}</dd>
      </div>
      <div>
        <dt>시작 / 종료</dt>
        <dd>
          {displayTime(item.started_at)} / {displayTime(item.finished_at)}
        </dd>
      </div>
    </dl>
  </section>
  <section class="timeline">
    <h2>Attempt timeline</h2>
    <ol>
      {#each item.attempts ?? [] as attempt}<li>
          <div class="rail"><span>{attempt.attempt_no}</span></div>
          <article>
            <div class="attempt-head">
              <strong
                >Slot {attempt.upstream_slot_no} · {attempt.upstream_label}</strong
              ><StatusBadge
                value={attempt.outcome}
              />
            </div>
            <dl>
              <div>
                <dt>Upstream evidence ID</dt>
                <dd><code>{attempt.upstream_id}</code></dd>
              </div>
              <div>
                <dt>HTTP / 오류</dt>
                <dd>
                  {attempt.status_code ?? "—"} / {attempt.error_class ?? "없음"}
                </dd>
              </div>
              <div>
                <dt>Latency / TTFB</dt>
                <dd>
                  {attempt.latency_ms ?? "—"}ms / {attempt.ttfb_ms ?? "—"}ms
                </dd>
              </div>
              <div>
                <dt>Response 시작</dt>
                <dd>
                  {attempt.response_started
                    ? "예 · 이후 replay 금지"
                    : "아니오 · failover 가능"}
                </dd>
              </div>
              <div>
                <dt>Bytes / cooldown</dt>
                <dd>
                  {attempt.bytes_out ?? "—"} / {displayTime(
                    attempt.cooldown_applied_until,
                  )}
                </dd>
              </div>
              <div>
                <dt>시간</dt>
                <dd>
                  {displayTime(attempt.started_at)} → {displayTime(
                    attempt.finished_at,
                  )}
                </dd>
              </div>
            </dl>
          </article>
        </li>{/each}
    </ol>
  </section>{/if}

<style>
  .back,
  .missing a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
  }
  .missing {
    display: grid;
    gap: 8px;
    padding: 20px;
    border: 1px solid #d99b48;
  }
  .summary,
  .timeline {
    padding: 22px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .timeline {
    margin-top: 12px;
  }
  .heading,
  .attempt-head {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }
  .heading h2 {
    margin: 5px 0;
  }
  .heading code {
    color: #aab4af;
    overflow-wrap: anywhere;
  }
  dl {
    display: grid;
    grid-template-columns: 1fr 1fr;
    margin: 14px 0 0;
  }
  dl div {
    padding: 11px;
    border-top: 1px solid #3f4843;
  }
  dt {
    color: #96a19b;
    font-size: 0.72rem;
  }
  dd {
    margin: 4px 0 0;
    overflow-wrap: anywhere;
  }
  .timeline ol {
    list-style: none;
    padding: 0;
  }
  .timeline li {
    display: grid;
    grid-template-columns: 40px 1fr;
  }
  .rail {
    position: relative;
  }
  .rail:after {
    content: "";
    position: absolute;
    top: 34px;
    bottom: -15px;
    left: 15px;
    width: 1px;
    background: #4a554f;
  }
  .timeline li:last-child .rail:after {
    display: none;
  }
  .rail span {
    display: grid;
    place-items: center;
    width: 31px;
    height: 31px;
    border-radius: 50%;
    background: #76b900;
    color: #081000;
    font-weight: 900;
  }
  .timeline article {
    padding: 0 0 24px;
  }
  @media (max-width: 650px) {
    dl {
      grid-template-columns: 1fr;
    }
    .heading,
    .attempt-head {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
