<script lang="ts">
import { onMount } from "svelte";
import { publicApi, startPolling } from "$lib/api";
import { age, observed } from "$lib/format";
import type { PublicModel, PublicModels } from "$lib/types";

let data: PublicModels | null = null;
let error = "";
let loading = true;
let refreshing = false;
let stale = false;
let proofFilter = "all";
let endpointFilter = "all";
let modalityFilter = "all";
let availabilityFilter = "available";
let advertisedFilter = "advertised";
let invalidFilters: string[] = [];
const endpoints = () => [...new Set((data?.items ?? []).map((model) => model.endpoint))].sort();
const modalities = () =>
  [
    ...new Set(
      (data?.items ?? []).flatMap((model) => [
        ...model.input_modalities,
        ...model.output_modalities,
      ]),
    ),
  ].sort();
const visible = () =>
  (data?.items ?? []).filter(
    (model) =>
      (proofFilter === "all" || model.proof_status === proofFilter) &&
      (endpointFilter === "all" || model.endpoint === endpointFilter) &&
      (modalityFilter === "all" ||
        model.input_modalities.includes(modalityFilter) ||
        model.output_modalities.includes(modalityFilter)) &&
      (availabilityFilter === "all" ||
        (availabilityFilter === "available") === model.available_now) &&
      (advertisedFilter === "all" || (advertisedFilter === "advertised") === model.advertised),
  );
const statusLabel = (model: PublicModel) =>
  ({
    pair_verified: "두 slot 검증",
    provider_verified: "한 slot 검증",
    proof_required: "proof 필요",
    unavailable: "현재 불가",
  })[model.proof_status];
const availabilityLabel = (model: PublicModel) =>
  model.available_now ? "지금 사용 가능" : "현재 사용 불가";
function resetFilters() {
  proofFilter = "all";
  endpointFilter = "all";
  modalityFilter = "all";
  availabilityFilter = "available";
  advertisedFilter = "advertised";
  syncFilterUrl();
}
function validateFilters() {
  const invalid: string[] = [];
  if (
    !["all", "pair_verified", "provider_verified", "proof_required", "unavailable"].includes(
      proofFilter,
    )
  )
    invalid.push("proof");
  if (!["all", "available", "unavailable"].includes(availabilityFilter))
    invalid.push("availability");
  if (!["all", "advertised", "hidden"].includes(advertisedFilter)) invalid.push("advertised");
  if (data && endpointFilter !== "all" && !endpoints().includes(endpointFilter))
    invalid.push("endpoint");
  if (data && modalityFilter !== "all" && !modalities().includes(modalityFilter))
    invalid.push("modality");
  invalidFilters = invalid;
}
function showAllCatalog() {
  proofFilter = "all";
  endpointFilter = "all";
  modalityFilter = "all";
  availabilityFilter = "all";
  advertisedFilter = "all";
  syncFilterUrl();
}
function readFilterUrl() {
  const params = new URLSearchParams(window.location.search);
  proofFilter = params.get("proof") ?? "all";
  endpointFilter = params.get("endpoint") ?? "all";
  modalityFilter = params.get("modality") ?? "all";
  availabilityFilter = params.get("availability") ?? "available";
  advertisedFilter = params.get("advertised") ?? "advertised";
  validateFilters();
}
function syncFilterUrl() {
  const params = new URLSearchParams();
  if (proofFilter !== "all") params.set("proof", proofFilter);
  if (endpointFilter !== "all") params.set("endpoint", endpointFilter);
  if (modalityFilter !== "all") params.set("modality", modalityFilter);
  if (availabilityFilter !== "available") params.set("availability", availabilityFilter);
  if (advertisedFilter !== "advertised") params.set("advertised", advertisedFilter);
  const query = params.toString();
  window.history.pushState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
  validateFilters();
}
function setFilter(
  kind: "proof" | "endpoint" | "modality" | "availability" | "advertised",
  value: string,
) {
  if (kind === "proof") proofFilter = value;
  else if (kind === "endpoint") endpointFilter = value;
  else if (kind === "modality") modalityFilter = value;
  else if (kind === "availability") availabilityFilter = value;
  else advertisedFilter = value;
  syncFilterUrl();
}
async function load(background = false) {
  if (background || data) refreshing = true;
  else loading = true;
  error = "";
  try {
    data = await publicApi.models();
    stale = data.snapshot.stale;
    validateFilters();
  } catch (caught) {
    error = caught instanceof Error ? caught.message : "모델 상태를 불러오지 못했습니다.";
    stale = data !== null;
  } finally {
    loading = false;
    refreshing = false;
  }
}
onMount(() => {
  readFilterUrl();
  const popstate = () => readFilterUrl();
  window.addEventListener("popstate", popstate);
  const stop = startPolling(() => load(data !== null));
  return () => {
    stop();
    window.removeEventListener("popstate", popstate);
  };
});
</script>

<svelte:head
  ><title>모델과 모달리티 · NVIDIA Build LB</title><meta
    name="description"
    content="catalogued, provider proof, available now를 분리한 NVIDIA Build LB 모델 매트릭스."
  /></svelte:head
>
<header class="page-head">
  <p class="eyebrow">MODEL PROOF MATRIX</p>
  <h1>등록과 실제 검증을<br />구분합니다.</h1>
  <p class="lede">
    목록에 있다는 사실을 호출 성공으로 표현하지 않습니다. 각 모델의 endpoint,
    모달리티, provider proof와 현재 가용성을 따로 확인하세요.
  </p>
</header>

<section class="section">
  {#if stale && data}<div class="notice" role="status">
      {#if error}새로 고침 실패: {error} · {/if}마지막 정상 snapshot {observed(
        data.snapshot.observed_at,
      )} ·
      <button type="button" onclick={() => load(true)} disabled={refreshing}
        >{refreshing ? "확인 중" : "다시 확인"}</button
      >
    </div>{/if}
  {#if data && data.items.length > 0 && invalidFilters.length === 0}<div class="filters" aria-label="모델 필터">
    <fieldset>
      <legend>Proof</legend
      >{#each [["all", "전체"], ["pair_verified", "두 slot"], ["provider_verified", "한 slot"], ["proof_required", "proof 필요"], ["unavailable", "현재 불가"]] as item}<button
          type="button"
          aria-pressed={proofFilter === item[0]}
          class:active={proofFilter === item[0]}
          onclick={() => {
            setFilter("proof", item[0]);
          }}>{item[1]}</button
        >{/each}
    </fieldset>
    <label
      >Endpoint<select value={endpointFilter} onchange={(event) => setFilter("endpoint", event.currentTarget.value)}
        ><option value="all">전체 endpoint</option
        >{#each endpoints() as endpoint}<option value={endpoint}
            >{endpoint}</option
          >{/each}</select
      ></label
    >
    <label
      >모달리티<select value={modalityFilter} onchange={(event) => setFilter("modality", event.currentTarget.value)}
        ><option value="all">전체 모달리티</option
        >{#each modalities() as modality}<option value={modality}
            >{modality}</option
          >{/each}</select
      ></label
    >
    <label
      >현재 가용성<select value={availabilityFilter} onchange={(event) => setFilter("availability", event.currentTarget.value)}
        ><option value="all">전체</option><option value="available"
          >Available now</option
        ><option value="unavailable">현재 불가</option></select
      ></label
    >
    <label
      >공개 상태<select value={advertisedFilter} onchange={(event) => setFilter("advertised", event.currentTarget.value)}
        ><option value="advertised">API에 공개</option><option value="all"
          >전체 catalog</option
        ><option value="hidden">API 미공개</option></select
      ></label
    >
  </div>{/if}
  {#if loading}<div class="panel" aria-busy="true">
      <h2>모델 상태 확인 중</h2>
    </div>
  {:else if error && !data}<div class="panel error-panel" role="alert">
      <h2>모델 상태를 불러오지 못했습니다</h2>
      <p>{error}</p>
      <button class="button secondary" type="button" onclick={() => load()}
        >다시 시도</button
      >
    </div>
  {:else if invalidFilters.length > 0}<div class="panel error-panel" role="alert">
      <h2>지원하지 않는 모델 필터입니다</h2>
      <p>{invalidFilters.join(" · ")} 조건을 확인하거나 기본 필터로 돌아가세요.</p>
      <button class="button secondary" type="button" onclick={resetFilters}>기본 필터로 돌아가기</button>
    </div>
  {:else if data && data.items.length === 0}<div class="panel empty-panel" role="status">
      <h2>아직 공개된 모델이 없습니다</h2>
      <p>provider proof가 확보되면 이 catalog에 자동으로 표시됩니다.</p>
      <a class="button secondary" href="/status">서비스 상태 확인</a>
    </div>
  {:else}<div class="result-head">
      <p class="muted observed">
        snapshot {observed(data?.snapshot.observed_at)} · {visible().length} / {data
          ?.items.length ?? 0}개 표시
      </p>
      <a href="/api/public/v1/openapi.json">OpenAPI JSON ↗</a>
    </div>
    <div class="model-list">
      {#each visible() as model (model.id)}<article class="panel model">
          <div class="model-head">
            <div>
              <code>{model.id}</code>
              <h2>{model.output_modalities.join(" · ")}</h2>
              <small>{model.advertised ? "API에 공개" : "API 미공개"}</small>
            </div>
            <span
              class="status-pill"
              data-tone={model.available_now ? "ok" : "danger"}
              >{availabilityLabel(model)}</span
            >
          </div>
          <dl>
            <div>
              <dt>Endpoint</dt>
              <dd><code>{model.endpoint}</code></dd>
            </div>
            <div>
              <dt>입력</dt>
              <dd>{model.input_modalities.join(" · ")}</dd>
            </div>
            <div>
              <dt>Streaming</dt>
              <dd>{model.streaming ? "지원" : "비지원"}</dd>
            </div>
            <div>
              <dt>Tool calling</dt>
              <dd>{model.tool_calling ? "지원" : "비지원"}</dd>
            </div>
            <div>
              <dt>Provider proof</dt>
              <dd>{statusLabel(model)} · {age(model.verified_age_seconds)}</dd>
            </div>
            <div>
              <dt>Available now</dt>
              <dd>{model.available_now ? "예" : "아니오"}</dd>
            </div>
          </dl>
        </article>{/each}
    </div>
    {#if data && data.items.length > 0 && visible().length === 0}<div class="panel empty-panel">
        <h2>이 조건의 모델이 없습니다</h2>
        <button class="button secondary" type="button" onclick={showAllCatalog}
          >전체 catalog 보기</button
        >
      </div>{/if}{/if}
</section>

<style>
  .notice {
    margin-bottom: 20px;
  }
  .notice button {
    min-height: 44px;
    border: 0;
    background: transparent;
    color: #b8e86f;
    text-decoration: underline;
  }
  .filters {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  fieldset {
    grid-column: 1 / -1;
    margin: 0;
    padding: 0;
    border: 0;
  }
  legend,
  .filters label {
    color: #8f9c95;
    font-size: 11px;
    font-weight: 750;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  fieldset button {
    margin: 7px 6px 0 0;
  }
  .filters button {
    min-height: 44px;
    padding: 9px 13px;
    border: 1px solid #35433d;
    background: transparent;
    color: #aab5af;
  }
  .filters button.active {
    border-color: #76b900;
    background: #16200f;
    color: #dff4bd;
  }
  .filters select {
    display: block;
    width: 100%;
    min-height: 44px;
    margin-top: 7px;
    border: 1px solid #35433d;
    border-radius: 3px;
    background: #0e1314;
    color: #e8eeeb;
    padding: 8px;
  }
  .result-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 18px;
    margin-bottom: 18px;
  }
  .result-head a {
    min-height: 44px;
    padding: 12px;
    color: #a9d66d;
  }
  .observed {
    margin: 0;
    font-size: 12px;
  }
  .model-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px;
  }
  .model-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 20px;
  }
  .model-head code {
    color: #a9d66d;
    overflow-wrap: anywhere;
  }
  .model-head small {
    display: block;
    margin-top: 8px;
    color: #829089;
  }
  .model h2 {
    margin: 10px 0 0;
    font-size: 21px;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    margin: 25px 0 0;
  }
  dt {
    color: #829089;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  dd {
    margin: 6px 0 0;
    overflow-wrap: anywhere;
    font-size: 13px;
  }
  dd code {
    color: #c2d0c8;
  }
  @media (max-width: 760px) {
    .filters {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .model-list {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 500px) {
    .filters {
      grid-template-columns: 1fr;
    }
    .result-head {
      align-items: flex-start;
      flex-direction: column;
    }
    .model-head {
      flex-direction: column;
    }
    dl {
      grid-template-columns: 1fr;
    }
  }
</style>
