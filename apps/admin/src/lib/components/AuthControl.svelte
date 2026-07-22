<script lang="ts">
import { onMount } from "svelte";
import { ApiError, api, authGeneration, saveToken, token } from "$lib/api";
import type { Overview } from "$lib/types";

let { compact = false } = $props<{ compact?: boolean }>();
let draft = $state("");
let authState = $state<"locked" | "checking" | "ready" | "unavailable">("locked");
let message = $state("");
let verifyEpoch = 0;

function publish(state: "locked" | "checking" | "ready" | "unavailable"): void {
  window.dispatchEvent(new CustomEvent("nblb-auth-state", { detail: { state } }));
}

async function verify(): Promise<void> {
  const epoch = ++verifyEpoch;
  if (!token()) {
    authState = "locked";
    publish("locked");
    return;
  }
  const generation = authGeneration();
  authState = "checking";
  publish("checking");
  message = "";
  try {
    await api<Overview>("/overview");
    if (epoch !== verifyEpoch || generation !== authGeneration()) return;
    authState = "ready";
    publish("ready");
  } catch (error) {
    if (epoch !== verifyEpoch || generation !== authGeneration()) return;
    if (error instanceof ApiError && error.status === 401) {
      authState = "locked";
      message = "token이 유효하지 않거나 만료되었습니다.";
      publish("locked");
      return;
    }
    authState = "unavailable";
    message = "서버에 연결하지 못했습니다. token은 유지됩니다.";
    publish("unavailable");
  }
}
function login(): void {
  authState = "checking";
  message = "";
  saveToken(draft.trim());
  draft = "";
}
function logout(): void {
  verifyEpoch += 1;
  saveToken("");
  authState = "locked";
  message = "";
}

onMount(() => {
  void verify();
  const auth = () => void verify();
  const invalid = (event: Event) => {
    const generation = (event as CustomEvent<{ generation?: number }>).detail?.generation;
    if (generation !== authGeneration()) return;
    verifyEpoch += 1;
    authState = "locked";
    message = "인증이 만료되었습니다.";
    publish("locked");
  };
  window.addEventListener("nblb-auth", auth);
  window.addEventListener("nblb-auth-invalid", invalid);
  return () => {
    window.removeEventListener("nblb-auth", auth);
    window.removeEventListener("nblb-auth-invalid", invalid);
  };
});
</script>

<div class:compact class="auth-control">
  {#if authState === "ready"}
    <span class="ready">● 서버 인증 완료</span><button
      type="button"
      onclick={logout}>잠금</button
    >
  {:else if authState === "unavailable"}
    <span class="unavailable">● 서버 확인 불가</span>
    <div class="recovery-actions">
      <button type="button" onclick={() => void verify()}>다시 확인</button>
      <button type="button" onclick={logout}>잠금</button>
    </div>
    <small>{message}</small>
  {:else}
    <form
      onsubmit={(event) => {
        event.preventDefault();
        login();
      }}
      aria-busy={authState === "checking"}
    >
      <label for={compact ? "admin-token-mobile" : "admin-token"}
        >관리 token</label
      >
      <input
        id={compact ? "admin-token-mobile" : "admin-token"}
        type="password"
        bind:value={draft}
        autocomplete="current-password"
        required
        disabled={authState === "checking"}
      />
      <button type="submit" disabled={authState === "checking"}
        >{authState === "checking" ? "확인 중" : "열기"}</button
      >
    </form>
    {#if message}<small>{message}</small>{/if}
  {/if}
</div>

<style>
  .auth-control {
    display: grid;
    gap: 4px;
  }
  .auth-control form {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .auth-control label {
    font-size: 0.78rem;
    color: #b4bdb8;
  }
  .ready {
    color: #b7ef68;
    font-size: 0.8rem;
    font-weight: 800;
  }
  .unavailable {
    color: #ffd08a;
    font-size: 0.8rem;
    font-weight: 800;
  }
  .recovery-actions {
    display: flex;
    gap: 8px;
  }
  .auth-control small {
    color: #ffb6b6;
  }
  .compact {
    padding: 12px 0 18px;
    border-bottom: 1px solid #3e4642;
  }
  .compact form {
    display: grid;
    grid-template-columns: 1fr auto;
  }
  .compact label {
    grid-column: 1/-1;
  }
  .compact input {
    min-width: 0;
  }
</style>
