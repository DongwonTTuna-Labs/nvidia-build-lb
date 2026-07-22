<script lang="ts">
import { onMount } from "svelte";
import { base } from "$app/paths";
import { adminErrorMessage, api, displayTime, startPolling } from "$lib/api";
import DataState from "$lib/components/DataState.svelte";
import PageHeader from "$lib/components/PageHeader.svelte";
import StatusBadge from "$lib/components/StatusBadge.svelte";
import {
  credentialProbeNextAction,
  isCredentialProbeFailure,
  isProfileProbeFailure,
  parseCredentialProbeMutation,
  parseProfileProbeResponse,
  probeErrorClass,
  withoutProbeResult,
} from "$lib/operations";
import type { Page, ProbeRun, Upstream } from "$lib/types";

const profiles = [
  "z-ai/glm-5.2",
  "microsoft/phi-4-multimodal-instruct",
  "nvidia/vila",
  "nvidia/nvclip",
  "black-forest-labs/flux.1-kontext-dev",
  "stabilityai/stable-video-diffusion",
  "nvidia/magpie-tts-multilingual",
  "nvidia/parakeet-ctc-1.1b",
];
const billable = new Set([
  "black-forest-labs/flux.1-kontext-dev",
  "stabilityai/stable-video-diffusion",
]);
let items = $state<Upstream[]>([]),
  loading = $state(true),
  queryError = $state(""),
  actionError = $state(""),
  notice = $state(""),
  lastSuccessAt = $state<string | null>(null),
  snapshotStale = $state(false),
  label = $state(""),
  credential = $state(""),
  busy = $state("");
let selected = $state<Record<string, string[]>>({}),
  confirmed = $state<Record<string, boolean>>({}),
  credentialProbeResults = $state<Record<string, ProbeRun>>({}),
  profileProbeResults = $state<Record<string, ProbeRun[]>>({});
async function load(): Promise<void> {
  loading = items.length === 0;
  if (!items.length) queryError = "";
  try {
    const page = await api<Page<Upstream>>("/upstreams");
    items = page.items;
    lastSuccessAt = page.snapshot.observed_at;
    snapshotStale = page.snapshot.stale;
    queryError = "";
    for (const item of items) {
      if (!(item.id in selected) && item.verified && !hasRoutingProof(item)) {
        selected = { ...selected, [item.id]: ["z-ai/glm-5.2"] };
      }
    }
  } catch (e) {
    queryError = adminErrorMessage(e, "Upstream 목록을 읽지 못했습니다.");
  } finally {
    loading = false;
  }
}
async function create(): Promise<void> {
  busy = "create";
  actionError = "";
  notice = "";
  try {
    await api("/upstreams", {
      method: "POST",
      body: JSON.stringify({ label, credential }),
    });
    credential = "";
    label = "";
    notice = "slot을 암호화 저장했습니다. credential probe와 profile proof를 완료하세요.";
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Upstream 저장에 실패했습니다.");
  } finally {
    busy = "";
  }
}
async function action(item: Upstream, name: "enable" | "disable" | "retire"): Promise<void> {
  if (name === "retire" && !confirm(`${item.label}을 영구 폐기할까요? 과거 evidence는 보존됩니다.`))
    return;
  busy = `${item.id}:${name}`;
  actionError = "";
  notice = "";
  try {
    await api(`/upstreams/${item.id}/${name}`, { method: "POST" });
    notice = `${item.label}: ${name} 완료`;
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Upstream 작업에 실패했습니다.");
  } finally {
    busy = "";
  }
}
async function probeCredential(item: Upstream): Promise<void> {
  busy = `${item.id}:probe`;
  actionError = "";
  notice = "";
  credentialProbeResults = withoutProbeResult(credentialProbeResults, item.id);
  try {
    const value = await api<unknown>(
      `/upstreams/${item.id}/probe`,
      { method: "POST" },
      [422],
      (payload) => isCredentialProbeFailure(payload, item.id),
    );
    const mutation = parseCredentialProbeMutation(value, item.id);
    credentialProbeResults = { ...credentialProbeResults, [item.id]: mutation.item };
    if (mutation.item.status === "passed") {
      notice = `SLOT ${item.slot_no} · ${item.label}: credential 검증 완료`;
    } else {
      actionError = `SLOT ${item.slot_no} · ${item.label}: credential 검증 실패. 저장된 evidence를 확인하세요.`;
    }
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Credential probe에 실패했습니다.");
  } finally {
    busy = "";
  }
}
async function probeProfiles(item: Upstream): Promise<void> {
  const profileIds = selected[item.id] ?? [];
  if (!profileIds.length) {
    notice = "";
    actionError = "검증할 profile을 하나 이상 선택하세요.";
    return;
  }
  const needsBilling = profileIds.some((id) => billable.has(id));
  if (needsBilling && !confirmed[item.id]) {
    notice = "";
    actionError = "Image/video generation은 비용이 생길 수 있습니다. 확인란을 선택하세요.";
    return;
  }
  busy = `${item.id}:profiles`;
  actionError = "";
  notice = "";
  profileProbeResults = withoutProbeResult(profileProbeResults, item.id);
  try {
    const value = await api<unknown>(
      `/upstreams/${item.id}/probe-profiles`,
      {
        method: "POST",
        body: JSON.stringify({
          profile_ids: profileIds,
          confirm_billable: needsBilling,
        }),
      },
      [422],
      (payload) => isProfileProbeFailure(payload, item.id, profileIds),
    );
    const evidence = parseProfileProbeResponse(value, item.id, profileIds);
    const runs = evidence.runs;
    profileProbeResults = { ...profileProbeResults, [item.id]: runs };
    if (runs.some((run) => run.status !== "passed")) {
      actionError = `${item.label}: ${evidence.failedProfile ?? "선택한 profile"} 검증에 실패했습니다. slot의 probe evidence를 확인하세요.`;
    } else {
      notice = `${item.label}: ${profileIds.length}개 profile proof 완료`;
    }
    await load();
  } catch (e) {
    actionError = adminErrorMessage(e, "Profile probe에 실패했습니다.");
  } finally {
    busy = "";
  }
}
function hasRoutingProof(item: Upstream): boolean {
  return item.proofs.some(
    (proof) => proof.profile_id === "z-ai/glm-5.2" && !proof.stale && proof.verified_key_count > 0,
  );
}
function toggle(id: string, profile: string): void {
  const values = selected[id] ?? [];
  selected = {
    ...selected,
    [id]: values.includes(profile) ? values.filter((v) => v !== profile) : [...values, profile],
  };
}
onMount(() => {
  const stop = startPolling(() => (busy ? Promise.resolve() : load()));
  const auth = () => void load();
  window.addEventListener("nblb-auth", auth);
  return () => {
    stop();
    window.removeEventListener("nblb-auth", auth);
  };
});
</script>

<PageHeader
  eyebrow="CAPACITY"
  title="Upstream slots"
  description="저장 → credential 검증 → profile proof → 활성화 순서만 따라가면 됩니다."
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
  {#if items.filter((item) => !item.retired).length < 2 &&
  items.filter((item) => !item.retired).every((item) => item.verified && hasRoutingProof(item))}<section class="form">
      <h2>빈 slot 추가</h2>
      <form
        onsubmit={(e) => {
          e.preventDefault();
          void create();
        }}
        aria-busy={busy === "create"}
      >
        <label
          >표시 이름<input
            bind:value={label}
            required
            maxlength="128"
            disabled={busy !== ""}
          /></label
        ><label
          >새 NVIDIA credential<input
            bind:value={credential}
            type="password"
            required
            autocomplete="off"
            disabled={busy !== ""}
          /></label
        ><button class="primary" type="submit" disabled={busy !== ""}
          >{busy === "create" ? "저장 중…" : "암호화 저장"}</button
        >
      </form>
      <p>
        credential은 다시 표시되지 않으며 DB와 vault에는 암호문만 저장됩니다.
      </p>
    </section>{/if}
  <p class="next-step">
    <strong>필수 기본 선택</strong> · <code>z-ai/glm-5.2</code> proof가 있어야 slot을
    라우팅에 포함할 수 있습니다.
  </p>
  <div class="slots">
    {#each [1, 2] as slot}{@const item = items.find(
        (candidate) => candidate.slot_no === slot && !candidate.retired,
      )}
      <article>
        <p class="slot">SLOT {slot}</p>
        {#if item}<div class="title">
            <div>
              <h2><a href={`${base}/upstreams/${item.id}`}>{item.label}</a></h2>
              <span>{item.id}</span>
            </div>
            <StatusBadge
              value={item.eligible_now
                ? "eligible"
                : item.verified
                  ? "proof required"
                  : "probe required"}
            />
          </div>
          <dl>
            <div>
              <dt>Credential</dt>
              <dd>{item.verified ? "검증됨" : "검증 필요"}</dd>
            </div>
            <div>
              <dt>라우팅</dt>
              <dd>{item.enabled ? "포함" : "제외"}</dd>
            </div>
            <div>
              <dt>Cooldown</dt>
              <dd>{displayTime(item.cooldown_until)}</dd>
            </div>
            <div>
              <dt>요청 / 실패</dt>
              <dd>{item.request_count} / {item.failure_count}</dd>
            </div>
          </dl>
          <p class="next-step">
            {!item.verified
              ? "다음: credential probe로 키가 실제 호출되는지 확인하세요."
              : !hasRoutingProof(item)
                ? "다음: 기본 chat profile proof를 만든 뒤 라우팅에 포함하세요."
                : item.enabled
                  ? "현재 라우팅 중입니다. 필요하면 제외하거나 다른 profile proof를 추가하세요."
                  : "다음: 라우팅에 포함하세요."}
          </p>
          <div class="actions">
            {#if !item.verified}<button
                class="primary"
                onclick={() => void probeCredential(item)}
                disabled={busy !== ""}
                >{busy === `${item.id}:probe`
                  ? "검증 중…"
                  : "1. credential probe"}</button
              >{:else if item.enabled || hasRoutingProof(item)}<button
                class:primary={!item.enabled}
                onclick={() =>
                  void action(item, item.enabled ? "disable" : "enable")}
                disabled={busy !== ""}
                >{item.enabled ? "라우팅 제외" : "3. 라우팅 포함"}</button
              >{/if}<button
              class="danger"
              onclick={() => void action(item, "retire")}
              disabled={busy !== ""}>폐기</button
            >
          </div>
          {#if credentialProbeResults[item.id]}{@const run = credentialProbeResults[item.id]}
            <section
              class="probe-evidence"
              aria-label={`SLOT ${item.slot_no} ${item.label} credential probe evidence`}
              aria-live="polite"
            >
              <h3>방금 실행한 credential probe</h3>
              <dl class="probe-facts">
                <div><dt>대상</dt><dd>SLOT {item.slot_no} · {item.label}</dd></div>
                <div><dt>상태</dt><dd>{run.status}</dd></div>
                <div><dt>HTTP</dt><dd>{run.status_code ?? "—"}</dd></div>
                <div><dt>오류 분류</dt><dd><code>{probeErrorClass(run)}</code></dd></div>
              </dl>
              <p>{credentialProbeNextAction(run)}</p>
              <a href={`${base}/probes`}>전체 probe evidence</a>
            </section>
          {/if}
          <details open={item.verified && !hasRoutingProof(item)}>
            <summary
              >2. Profile proof 선택 <small
                >{item.proofs.filter(
                  (proof) => !proof.stale && proof.verified_key_count > 0,
                ).length}/{profiles.length} fresh</small
              ></summary
            >
            <fieldset disabled={!item.verified || busy !== ""}>
              <legend>실제 호출로 검증할 profile</legend
              >{#each profiles as profile}{@const proof = item.proofs.find(
                  (value) => value.profile_id === profile,
                )}<label class="check"
                  ><input
                    type="checkbox"
                    checked={(selected[item.id] ?? []).includes(profile)}
                    onchange={() => toggle(item.id, profile)}
                  /><span
                    ><strong>{profile}</strong><small
                      >{proof?.stale
                        ? "stale"
                        : proof?.verified_key_count
                          ? `verified · ${displayTime(proof.last_verified_at)}`
                          : "proof 없음"}{billable.has(profile)
                        ? " · 비용 가능"
                        : ""}</small
                    ></span
                  ></label
                >{/each}<label class="confirm"
                ><input type="checkbox" bind:checked={confirmed[item.id]} /> Image/video
                provider 비용 발생 가능성을 확인했습니다.</label
              ><button
                class="primary"
                type="button"
                onclick={() => void probeProfiles(item)}
                disabled={!item.verified || busy !== ""}
                >{busy === `${item.id}:profiles`
                  ? "검증 중…"
                  : "선택 profile 검증"}</button
              >
            </fieldset>
            {#if (profileProbeResults[item.id] ?? []).length}<section
                class="probe-evidence"
                aria-label={`${item.label} 방금 실행한 profile probe evidence`}
                aria-live="polite"
              >
                <h3>방금 실행한 probe evidence</h3>
                <ul>
                  {#each profileProbeResults[item.id] ?? [] as run (run.id)}<li>
                      <strong>{run.profile_id ?? "credential"}</strong>
                      <span
                        >{run.status}{run.status_code
                          ? ` · HTTP ${run.status_code}`
                          : ""}{run.error_class ? ` · ${run.error_class}` : ""}</span
                      >
                    </li>{/each}
                </ul>
              </section>{/if}
          </details>{:else}<h2>비어 있음</h2>
          <span>위 폼에서 credential을 저장하세요.</span>{/if}
      </article>{/each}
  </div>
{/if}

<style>
  .form,
  .slots article {
    padding: 20px;
    border: 1px solid #4c5650;
    border-radius: 8px;
    background: #171b1c;
  }
  .form {
    margin-bottom: 14px;
  }
  .form form {
    display: flex;
    gap: 10px;
    align-items: end;
    flex-wrap: wrap;
  }
  .form p,
  .title span,
  small {
    color: #aeb7b2;
  }
  label {
    display: grid;
    gap: 5px;
  }
  .slots {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .slot {
    color: #8fc63e;
    font-weight: 900;
  }
  .probe-evidence {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid #3f4743;
  }
  .probe-evidence h3 {
    margin: 0 0 8px;
    font-size: 1rem;
  }
  .probe-evidence ul {
    margin: 0;
    padding-left: 20px;
  }
  .probe-evidence li span {
    display: block;
    color: #aeb7b2;
    overflow-wrap: anywhere;
  }
  .probe-facts {
    margin: 0;
  }
  .probe-facts dd {
    overflow-wrap: anywhere;
  }
  .probe-evidence a {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    color: #c9f28e;
  }
  .title {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }
  .title > div {
    min-width: 0;
  }
  .title h2 {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .title h2 a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    color: #fff;
  }
  .title span {
    font-size: 0.7rem;
    overflow-wrap: anywhere;
  }
  dl {
    display: grid;
    grid-template-columns: 1fr 1fr;
    margin: 18px 0;
  }
  dl div {
    padding: 10px;
    border-top: 1px solid #39423e;
  }
  dt {
    color: #929d97;
    font-size: 0.75rem;
  }
  dd {
    margin: 4px 0 0;
  }
  .next-step {
    min-height: 44px;
    padding: 10px;
    border-left: 3px solid #76b900;
    background: #111516;
    color: #c8d1cc;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  details {
    margin-top: 18px;
    border-top: 1px solid #49534e;
    padding-top: 12px;
  }
  summary {
    display: flex;
    align-items: center;
    min-height: 44px;
    cursor: pointer;
    font-weight: 800;
  }
  fieldset {
    display: grid;
    gap: 6px;
    margin: 14px 0 0;
    border: 0;
    padding: 0;
  }
  .check {
    grid-template-columns: auto 1fr;
    align-items: start;
    min-height: 44px;
    padding: 8px;
    background: #111516;
  }
  .check input,
  .confirm input {
    min-height: auto;
  }
  .check small {
    display: block;
  }
  .check strong {
    overflow-wrap: anywhere;
  }
  .confirm {
    grid-template-columns: auto 1fr;
    align-items: center;
    min-height: 44px;
    margin: 8px 0;
    padding: 10px;
    border: 1px solid #725f38;
  }
  .notice,
  .error {
    padding: 12px;
  }
  .notice {
    border: 1px solid #76b900;
  }
  .error {
    border: 1px solid #d65e5e;
  }
  @media (max-width: 850px) {
    .slots {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 600px) {
    .form form label {
      width: 100%;
    }
    dl {
      grid-template-columns: 1fr;
    }
    .title {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
