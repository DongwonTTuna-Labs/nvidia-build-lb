<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { adminErrorMessage, api, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import { isModelProbeFailure, parseModelProbeResponse } from "$lib/operations";
import type { Model, Page, ProbeRun, Upstream } from "$lib/types";

const billable = new Set([
  "black-forest-labs/flux.1-kontext-dev",
  "stabilityai/stable-video-diffusion",
]);
let items = $state<Model[]>([]),
  upstreams = $state<Upstream[]>([]),
  loading = $state(true),
  queryError = $state(""),
  upstreamsError = $state(""),
  actionError = $state(""),
  notice = $state(""),
  busy = $state(""),
  selectedModel = $state(""),
  selectedUpstreams = $state<string[]>([]),
  confirmBillable = $state(false),
  results = $state<ProbeRun[]>([]),
  resultUpstreams = $state<Record<string, { label: string; slotNo: number }>>({}),
  lastSuccessAt = $state<string | null>(null),
  snapshotStale = $state(false),
  refreshing = $state(false),
  mutationGeneration = 0;
async function load() {
  if (refreshing) return;
  refreshing = true;
  loading = !items.length;
  if (!items.length) queryError = "";
  try {
    const [modelsResult, slotsResult] = await Promise.allSettled([
      api<Page<Model>>("/models"),
      api<Page<Upstream>>("/upstreams"),
    ]);
    if (modelsResult.status === "fulfilled") {
      items = modelsResult.value.items;
      lastSuccessAt = modelsResult.value.snapshot.observed_at;
      snapshotStale = modelsResult.value.snapshot.stale;
      queryError = "";
    } else {
      queryError = adminErrorMessage(modelsResult.reason, "모델 상태를 읽지 못했습니다.");
    }
    if (slotsResult.status === "fulfilled") {
      upstreams = slotsResult.value.items.filter((item) => !item.retired && item.verified);
      const eligibleIds = new Set(upstreams.map((item) => item.id));
      selectedUpstreams = selectedUpstreams.filter((id) => eligibleIds.has(id));
      upstreamsError = "";
    } else {
      upstreamsError = adminErrorMessage(
        slotsResult.reason,
        "Probe 대상 upstream을 읽지 못했습니다.",
      );
    }
  } catch (e) {
    queryError = adminErrorMessage(e, "모델 상태를 읽지 못했습니다.");
  } finally {
    loading = false;
    refreshing = false;
  }
}
async function sync() {
  const generation = ++mutationGeneration;
  busy = "sync";
  actionError = "";
  notice = "";
  try {
    await api("/models/sync", { method: "POST" });
    if (generation === mutationGeneration) notice = "Canonical model catalog를 동기화했습니다.";
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Model catalog 동기화에 실패했습니다.");
  } finally {
    busy = "";
  }
}
function selectUpstream(id: string) {
  selectedUpstreams = selectedUpstreams.includes(id)
    ? selectedUpstreams.filter((value) => value !== id)
    : selectedUpstreams.length < 2
      ? [...selectedUpstreams, id]
      : selectedUpstreams;
}
async function probe() {
  if (!selectedModel || selectedUpstreams.length < 1) {
    actionError = "모델과 upstream 1~2개를 선택하세요.";
    return;
  }
  const targetModel = selectedModel;
  const targetUpstreams = [...selectedUpstreams];
  const targetDescriptors = Object.fromEntries(
    upstreams
      .filter((item) => targetUpstreams.includes(item.id))
      .map((item) => [item.id, { label: item.label, slotNo: item.slot_no }]),
  );
  if (billable.has(targetModel) && !confirmBillable) {
    actionError = "비용 발생 가능성을 확인하세요.";
    return;
  }
  const generation = ++mutationGeneration;
  busy = "probe";
  actionError = "";
  notice = "";
  results = [];
  resultUpstreams = {};
  try {
    const value = await api<unknown>(
      "/models/probe",
      {
        method: "POST",
        body: JSON.stringify({
          model_id: targetModel,
          upstream_ids: targetUpstreams,
          confirm_billable: billable.has(targetModel),
        }),
      },
      [422],
      (payload) => isModelProbeFailure(payload, targetModel, targetUpstreams),
    );
    if (generation === mutationGeneration) {
      results = parseModelProbeResponse(value, targetModel, targetUpstreams).runs;
      resultUpstreams = targetDescriptors;
      if (results.some((run) => run.status !== "passed")) {
        actionError = `${targetModel} 검증에 실패한 upstream이 있습니다. 아래 probe evidence를 확인하세요.`;
      } else {
        notice = `${targetModel} 검증을 완료했습니다.`;
      }
    }
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Model probe에 실패했습니다.");
  } finally {
    busy = "";
  }
}
function resultUpstream(result: ProbeRun): { label: string; slotNo: number } | undefined {
  return result.upstream_id ? resultUpstreams[result.upstream_id] : undefined;
}
onMount(() => startPolling(() => (busy ? Promise.resolve() : load())));
</script>

<PageHeader
  eyebrow="CAPACITY"
  title="Model capabilities"
  description="광고됨, provider proof, 지금 사용 가능을 분리해 확인합니다."
/>
<DataState
  {loading}
  error={queryError}
  hasData={items.length > 0}
  stale={snapshotStale}
  {lastSuccessAt}
  empty={!loading && !queryError && !items.length}
  retry={load}
/>
{#if actionError}<p class="error" role="alert">{actionError}</p>{/if}
{#if notice}<p class="notice" role="status">{notice}</p>{/if}
{#if lastSuccessAt}
  <div class="toolbar">
    <button onclick={() => void sync()} disabled={busy !== ""}>
      {busy === "sync" ? "동기화 중…" : "Catalog 동기화"}
    </button>
  </div>
  <section class="probe">
    <h2>실제 model probe</h2>
    {#if upstreamsError}<p class="partial-error" role="status">
        {upstreamsError} 모델 matrix는 정상 표시하며 probe만 잠급니다.
        <button
          type="button"
          onclick={() => void load()}
          disabled={refreshing || busy !== ""}
          aria-busy={refreshing}>{refreshing ? "확인 중…" : "Upstream 다시 확인"}</button
        >
      </p>{/if}
    {#if !upstreamsError && upstreams.length === 0}<div class="recovery" role="status">
        <strong>검증된 upstream이 없습니다.</strong>
        <span>먼저 credential probe를 통과시킨 뒤 이 화면에서 모델을 검증하세요.</span>
        <a href={`${base}/upstreams`}>Upstreams에서 검증 시작</a>
      </div>{/if}
    <div class="fields">
      <label
        >모델<select
          bind:value={selectedModel}
          disabled={busy !== "" || !!upstreamsError || upstreams.length === 0}
          ><option value="">선택</option>{#each items as item}<option
              value={item.id}
              >{item.id}{billable.has(item.id) ? " · 비용 가능" : ""}</option
            >{/each}</select
        ></label
      >
      <fieldset disabled={busy !== "" || !!upstreamsError || upstreams.length === 0}>
        <legend>대상 upstream · 최대 2개</legend>
        {#each upstreams as item}<label class="check"
            ><input
              type="checkbox"
              checked={selectedUpstreams.includes(item.id)}
              onchange={() => selectUpstream(item.id)}
            />{item.label} · slot {item.slot_no}</label
          >{/each}
      </fieldset>
    </div>
    {#if billable.has(selectedModel)}<label class="confirm"
        ><input
          type="checkbox"
          bind:checked={confirmBillable}
          disabled={busy !== ""}
        /> 최소 image/video 요청으로 provider 비용이 생길 수 있음을 확인했습니다.</label
      >{/if}
    <button
      class="primary"
      onclick={() => void probe()}
      disabled={busy !== "" || !!upstreamsError || upstreams.length === 0}
      >{busy === "probe" ? "검증 중…" : "선택 모델 검증"}</button
    >
    {#if results.length}<ul>
        {#each results as result (result.id)}{@const upstream = resultUpstream(result)}<li>
            <StatusBadge value={result.status} /><span>
              SLOT {upstream?.slotNo ?? "—"} · {upstream?.label ?? "알 수 없는 upstream"}<br />
              HTTP {result.status_code ?? "—"} · 오류 분류 <code
                >{result.error_class ?? "없음"}</code
              >
            </span>
          </li>{/each}
      </ul>
      <a class="probe-link" href={`${base}/probes`}>전체 probe evidence</a>{/if}
  </section>
  <div class="models">
    {#each items as item}
      <article>
        <div class="model-title">
          <div>
            <h2>{item.id}</h2>
            <code>{item.endpoint}</code>
          </div>
          <StatusBadge
            value={item.available_now ? "available" : item.proof_status}
          />
        </div>
        <p>
          <strong>{item.input_modalities.join(" + ")}</strong> → {item.output_modalities.join(
            " + ",
          )}
        </p>
        <div class="flags">
          <span>{item.advertised ? "광고됨" : "비공개"}</span><span
            >{item.streaming ? "streaming" : "non-stream"}</span
          ><span>{item.tool_calling ? "tool calling" : "no tools"}</span><span
            >{item.verified_key_count}/2 fresh proof</span
          >
        </div>
      </article>
    {/each}
  </div>
{/if}

<style>
  .toolbar {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 10px;
  }
  .notice,
  .error,
  .partial-error {
    padding: 12px;
  }
  .recovery {
    display: grid;
    gap: 8px;
    padding: 14px;
    border: 1px solid #d99b48;
    color: #ffd08a;
  }
  .recovery a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #c9f28e;
  }
  .notice {
    border: 1px solid #76b900;
  }
  .error {
    border: 1px solid #d65e5e;
  }
  .partial-error {
    border: 1px solid #d99b48;
    color: #ffd08a;
  }
  .probe,
  .models article {
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .probe {
    margin-bottom: 12px;
  }
  .fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  .fields > *,
  .model-title > * {
    min-width: 0;
  }
  .fields select {
    width: 100%;
  }
  label {
    display: grid;
    gap: 6px;
  }
  fieldset {
    border: 0;
    padding: 0;
  }
  .check,
  .confirm {
    display: flex;
    align-items: center;
    min-height: 44px;
    gap: 8px;
    padding: 7px 8px;
  }
  .check input,
  .confirm input {
    min-height: auto;
  }
  .confirm {
    margin: 10px 0;
    color: #ffd08a;
  }
  .probe ul {
    display: grid;
    gap: 5px;
    list-style: none;
    padding: 10px 0;
  }
  .probe li {
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .probe li span {
    overflow-wrap: anywhere;
  }
  .probe-link {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #c9f28e;
  }
  .models {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .model-title {
    display: flex;
    justify-content: space-between;
    gap: 14px;
  }
  .models h2 {
    font-size: 1rem;
    margin: 0 0 7px;
    overflow-wrap: anywhere;
  }
  .models code {
    color: #9daba4;
  }
  .models p {
    color: #b9c3be;
  }
  .flags {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .flags span {
    padding: 5px 8px;
    background: #252b28;
    color: #b7c0bb;
    font-size: 0.75rem;
  }
  @media (max-width: 800px) {
    .models,
    .fields {
      grid-template-columns: 1fr;
    }
  }
</style>
