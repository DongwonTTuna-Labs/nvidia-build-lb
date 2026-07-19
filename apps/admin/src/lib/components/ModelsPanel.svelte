<script lang="ts">
import { modelInfo } from "$lib/admin-format";
import type { ProfileCapability, SnapshotState } from "$lib/admin-types";
import type { AdminRouteId } from "$lib/copy";

export let active: AdminRouteId;
export let state: SnapshotState;
export let profileCapabilities: ProfileCapability[];
export let models: string[];
</script>

<section id="models" class="panel" aria-labelledby="models-title" hidden={active !== "models"}>
  <h2 id="models-title">모달리티별 모델</h2>
  <p class="muted">각 모델의 요청 경로와 제공자 검증 상태를 표시합니다.</p>
  {#if state === "partial"}<p class="attention" role="status">모델별 준비 상태를 확인하지 못했습니다. 현재 요청 가능으로 해석하지 마세요.</p>{/if}
  <div class="model-list">
    {#each (profileCapabilities.length ? profileCapabilities : models.map((model) => ({ id: model, route: modelInfo(model).route, advertised: true, available_now: false, proof_status: "not_ready", modalities: modelInfo(model).modalities.split("·") }))) as capability}
      <article><h3>{capability.id}</h3><p>{capability.modalities.join(" · ")}</p><code>{capability.route}</code><small>{capability.available_now ? "제공자 검증 완료 · 현재 요청 가능" : capability.proof_status === "provider_proof_required" ? "현재 요청 전 제공자 proof 필요" : "현재 준비 안 됨 · 라우팅에서 원인 확인"}</small></article>
    {:else}
      <p class="muted">관리자 인증 후 모델을 확인하세요.</p>
    {/each}
  </div>
</section>

<style>
  .panel { display: grid; gap: 12px; padding: 24px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  .muted, small { color: #aab2ae; }
  .model-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  article { display: grid; gap: 12px; padding: 24px; background: #1d2124; border: 1px solid #6b746f; border-radius: 8px; min-width: 0; }
  .model-list h3 { overflow-wrap: anywhere; word-break: break-word; }
  code { color: #b8a1ff; overflow-wrap: anywhere; word-break: break-word; }
  .attention { display: flex; gap: 12px; align-items: center; margin: 12px 0; padding: 12px 14px; border-radius: 6px; color: #ffd166; border: 1px solid #ffd166; }
  @media (max-width: 767px) { .panel { padding: 16px; } }
</style>
