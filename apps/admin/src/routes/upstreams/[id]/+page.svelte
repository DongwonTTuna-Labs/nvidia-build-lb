<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { page } from "$app/state";
import { ApiError, adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import type { Upstream } from "$lib/types";

let item = $state<Upstream | null>(null),
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
    const value = await api<Upstream>(`/upstreams/${id}`);
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
    } else error = adminErrorMessage(e, "Upstream 상세 상태를 읽지 못했습니다.");
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

<a class="back" href={`${base}/upstreams`}>← Upstream 목록</a><PageHeader
  title={item ? `Slot ${item.slot_no} · ${item.label}` : "Upstream 상세"}
  description="현재 routing 자격과 profile별 최신 proof만 확인합니다."
/>{#if notFound}<section class="missing" role="alert">
    <strong>Upstream을 찾을 수 없습니다.</strong><span
      >폐기되었거나 URL이 올바르지 않습니다.</span
    ><a href={`${base}/upstreams`}>Upstream 목록으로 돌아가기</a>
  </section>{:else}<DataState
    {loading}
    {error}
    hasData={item !== null}
    {lastSuccessAt}
    retry={load}
  />{/if}{#if item && !notFound}<section class="summary">
    <div>
      <span>현재 상태</span><StatusBadge
        value={item.eligible_now
          ? "eligible"
          : item.retired
            ? "retired"
            : "unavailable"}
      />
    </div>
    <div>
      <span>Credential</span><strong
        >{item.verified ? "verified" : "probe required"}</strong
      >
    </div>
    <div>
      <span>라우팅</span><strong
        >{item.enabled ? "included" : "excluded"}</strong
      >
    </div>
    <div>
      <span>Cooldown 종료</span><strong
        >{displayTime(item.cooldown_until)}</strong
      >
    </div>
  </section>
  {#if !item.eligible_now}<section class="next-step" role="status">
      <strong>다음 행동</strong>
      <span
        >{!item.verified
          ? "credential 검증이 필요합니다."
          : !item.enabled
            ? "검증된 slot을 라우팅에 포함해야 합니다."
            : "기본 chat profile의 fresh proof가 필요합니다."}</span
      >
      <a href={`${base}/upstreams`}>Upstreams에서 복구</a>
    </section>{/if}
  <section class="proofs">
    <h2>Profile proof</h2>
    {#each item.proofs as proof}<article>
        <div>
          <strong>{proof.profile_id}</strong><small
            >{displayTime(proof.last_verified_at)}</small
          >
        </div>
        <StatusBadge value={proof.stale ? "stale" : proof.status} />
      </article>{/each}
  </section>{/if}

<style>
  .back,
  .missing a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    margin-bottom: 12px;
  }
  .missing {
    display: grid;
    gap: 8px;
    padding: 20px;
    border: 1px solid #d99b48;
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .summary div {
    padding: 18px;
    border-right: 1px solid #3d4541;
  }
  .summary span,
  .proofs small {
    display: block;
    color: #9da7a2;
    margin-bottom: 6px;
  }
  .proofs {
    margin-top: 14px;
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .next-step {
    display: grid;
    gap: 7px;
    margin-top: 14px;
    padding: 16px;
    border-left: 3px solid #d99b48;
    background: #171b1c;
  }
  .next-step a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #b9e87a;
  }
  .proofs article {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    padding: 13px 0;
    border-top: 1px solid #3d4541;
  }
  .proofs small {
    margin-top: 4px;
  }
  @media (max-width: 750px) {
    .summary {
      grid-template-columns: 1fr 1fr;
    }
  }
  @media (max-width: 430px) {
    .summary {
      grid-template-columns: 1fr;
    }
    .proofs article {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
