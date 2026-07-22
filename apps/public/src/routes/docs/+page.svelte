<script lang="ts">
import { onDestroy } from "svelte";

let copied = "";
let copyError = "";
let copyTimer: number | undefined;
let copyEpoch = 0;
async function copy(id: string, value: string) {
  const epoch = ++copyEpoch;
  if (copyTimer !== undefined) {
    window.clearTimeout(copyTimer);
    copyTimer = undefined;
  }
  copied = "";
  copyError = "";
  try {
    await navigator.clipboard.writeText(value);
    if (epoch !== copyEpoch) return;
    copied = id;
    copyTimer = window.setTimeout(() => {
      if (epoch !== copyEpoch) return;
      copied = "";
      copyTimer = undefined;
    }, 1600);
  } catch {
    if (epoch !== copyEpoch) return;
    copyError = "복사하지 못했습니다. 아래 명령을 직접 선택해 복사하세요.";
  }
}
onDestroy(() => {
  copyEpoch += 1;
  if (copyTimer !== undefined) window.clearTimeout(copyTimer);
});
const baseUrl = "https://nvidia-lb.dongwontuna.net/v1";
const openAiExample = [
  ["import", "OpenAI", "from", '"openai";'].join(" "),
  "",
  `const client = new OpenAI({
  apiKey: process.env.NVIDIA_LB_TOKEN,
  baseURL: "${baseUrl}",
});

const response = await client.chat.completions.create({
  model: "z-ai/glm-5.2",
  messages: [{ role: "user", content: "Hello" }],
});`,
].join("\n");
const hermesExample = `model:
  provider: nvidia
  default: z-ai/glm-5.2
  base_url: http://127.0.0.1:2456/v1`;
const vilaExample = `curl ${baseUrl}/nvidia/inference \\
  -H "Authorization: Bearer $NVIDIA_LB_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"nvidia/vila","messages":[{"role":"user","content":[{"type":"text","text":"Describe this image"},{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}],"max_tokens":32}'`;
const generationExample = `curl ${baseUrl}/images/generations \\
  -H "Authorization: Bearer $NVIDIA_LB_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"black-forest-labs/flux.1-kontext-dev","prompt":"A green square","n":1,"size":"1024x1024","response_format":"b64_json"}'`;
const audioExample = `curl ${baseUrl}/audio/transcriptions \\
  -H "Authorization: Bearer $NVIDIA_LB_TOKEN" \\
  -F model=nvidia/parakeet-ctc-1.1b \\
  -F file=@sample.wav`;
</script>

<svelte:head
  ><title>연결 가이드 · NVIDIA Build LB</title><meta
    name="description"
    content="OpenAI SDK, curl, Hermes에서 NVIDIA Build LB에 연결하는 방법."
  /></svelte:head
>
<header class="page-head">
  <p class="eyebrow">INTEGRATION GUIDE</p>
  <h1>생각할 것 없이<br />바로 연결합니다.</h1>
  <p class="lede">
    provider key는 client에 넣지 않습니다. 관리자가 발급한 downstream token과 이
    base URL만 사용하세요.
  </p>
</header>
<div class="docs-layout section">
  <aside aria-label="문서 목차">
    <a href="#quickstart">빠른 시작</a><a href="#openai">OpenAI SDK</a><a
      href="#streaming">Streaming</a
    ><a href="#media">멀티모달</a><a href="#hermes">Hermes</a><a href="#errors"
      >오류와 재시도</a
    ><a href="#privacy">개인정보</a>
  </aside>
  <article class="docs">
    <section id="quickstart">
      <p class="eyebrow">01 · BASE URL</p>
      <h2>빠른 시작</h2>
      <p>
        API token은 <code>nblb_ds_</code>로 시작하는 downstream
        credential입니다.
      </p>
      <div class="code-head">
        <span>curl</span><button
          type="button"
          onclick={() =>
            copy(
              "curl",
              `curl ${baseUrl}/models -H "Authorization: Bearer $NVIDIA_LB_TOKEN"`,
            )}>{copied === "curl" ? "복사됨" : "복사"}</button
        >
      </div>
      <p class="copy-feedback" aria-live="polite">
        {copied === "curl" ? "클립보드에 복사했습니다." : copyError}
      </p>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
      <pre role="region" aria-label="curl 빠른 시작 예제" tabindex="0"><code
          >curl {baseUrl}/models \
  -H "Authorization: Bearer $NVIDIA_LB_TOKEN"</code
        ></pre>
    </section>
    <section id="openai">
      <p class="eyebrow">02 · SDK</p>
      <h2>OpenAI SDK</h2>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
      <pre role="region" aria-label="OpenAI SDK 예제" tabindex="0"><code
          >{openAiExample}</code
        ></pre>
    </section>
    <section id="streaming">
      <p class="eyebrow">03 · STREAMING</p>
      <h2>첫 frame 이후에는 replay하지 않습니다</h2>
      <p>
        첫 유효 SSE frame 전에만 다른 slot으로 failover합니다. frame이 client에
        전달된 뒤 upstream이 중단되면 다른 출력을 이어붙이지 않습니다.
      </p>
    </section>
    <section id="media">
      <p class="eyebrow">04 · MULTIMODAL</p>
      <h2>입력 형식까지 바로 확인합니다</h2>
      <p class="notice">
        아래 예제는 canonical catalog 기준입니다. Chat, VILA, embedding,
        image/video, audio를 실행하기 전에 모두 <a href="/models">모델 화면</a
        >에서 <strong>지금 사용 가능</strong>과 provider proof를 확인하세요.
      </p>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable data region) -->
      <div
        class="error-table multimodal-contract"
        role="region"
        aria-label="멀티모달 endpoint 계약"
        tabindex="0"
      >
        <table class="multimodal-table">
          <thead><tr><th>경로</th><th>모델</th><th>입력 → 출력</th></tr></thead
          ><tbody
            ><tr
              ><td><code>/v1/nvidia/inference</code></td><td
                ><code>nvidia/vila</code></td
              ><td>text/image/video → text JSON</td></tr
            ><tr
              ><td><code>/v1/embeddings</code></td><td
                ><code>nvidia/nvclip</code></td
              ><td>text/image → embedding JSON</td></tr
            ><tr
              ><td><code>/v1/images/generations</code></td><td
                ><code>black-forest-labs/flux.1-kontext-dev</code></td
              ><td>text/image → base64 image JSON</td></tr
            ><tr
              ><td><code>/v1/videos/generations</code></td><td
                ><code>stabilityai/stable-video-diffusion</code></td
              ><td>image data URL → base64 video JSON</td></tr
            ><tr
              ><td><code>/v1/audio/speech</code></td><td
                ><code>nvidia/magpie-tts-multilingual</code></td
              ><td>JSON text → WAV</td></tr
            ><tr
              ><td><code>/v1/audio/transcriptions</code></td><td
                ><code>nvidia/parakeet-ctc-1.1b</code></td
              ><td>multipart WAV → text JSON</td></tr
            ></tbody
          >
        </table>
      </div>
      <p class="notice">
        Image/video generation은 provider side effect 뒤 다른 slot에 재전송하지
        않습니다. 비용이 생길 수 있으므로 실행 전 확인이 특히 중요합니다.
      </p>
      <details>
        <summary>VILA image 입력 예제</summary>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
        <pre role="region" aria-label="VILA image 입력 예제" tabindex="0"><code
            >{vilaExample}</code
          ></pre>
      </details>
      <details>
        <summary>Image generation 예제</summary>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
        <pre role="region" aria-label="Image generation 예제" tabindex="0"><code
            >{generationExample}</code
          ></pre>
      </details>
      <details>
        <summary>Audio transcription 예제</summary>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
        <pre role="region" aria-label="Audio transcription 예제" tabindex="0"><code
            >{audioExample}</code
          ></pre>
      </details>
    </section>
    <section id="hermes">
      <p class="eyebrow">05 · HERMES</p>
      <h2>Rust cutover helper가 원자 적용합니다</h2>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
      <pre role="region" aria-label="Hermes 설정 예제" tabindex="0"><code
          >{hermesExample}</code
        ></pre>
      <p>
        <code>NVIDIA_API_KEY</code>에는 provider key가 아니라 LB가 한 번 발급한
        downstream token이 들어갑니다. 파일을 직접 편집하지 마세요. root-only
        helper가 두 파일을 함께 백업·교체하고 doctor, exact marker, tool task,
        LB request correlation과 rollback rehearsal까지 성공해야 commit합니다.
      </p>
      <ol>
        <li>
          홈서버의 loopback 전용 Admin(<code>http://127.0.0.1:2456/admin/</code
          >)을 엽니다. 공개 hostname의 <code>/admin</code>은 의도적으로
          404입니다.
        </li>
        <li>
          <strong>QA → Live NVIDIA 검증 → Hermes E2E</strong>에서 run을
          생성합니다.
        </li>
        <li>
          running 화면에 표시된 실제 run UUID와 현재 app commit이 포함된 명령을
          그대로 복사해 실행합니다. 아래 placeholder를 추측해 채우지 마세요.
        </li>
      </ol>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable code region) -->
      <pre role="region" aria-label="Hermes cutover 명령" tabindex="0"><code
          >sudo NBLB_EXPECTED_COMMIT=&lt;full-app-commit-sha&gt; \
  /usr/local/sbin/nblb-hermes-cutover apply --qa-run &lt;QA_RUN_UUID&gt;</code
        ></pre>
    </section>
    <section id="errors">
      <p class="eyebrow">06 · ERRORS</p>
      <h2>오류와 재시도</h2>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (keyboard-scrollable table region) -->
      <div class="error-table" role="region" aria-label="오류 코드 표" tabindex="0">
        <table>
          <thead><tr><th>HTTP</th><th>의미</th><th>다음 행동</th></tr></thead
          ><tbody
            ><tr
              ><td>401</td><td>token이 유효하지 않거나 만료됨</td><td
                >credential 재발급</td
              ></tr
            ><tr
              ><td>403</td><td>scope 또는 model allowlist 부족</td><td
                >client 정책 확인</td
              ></tr
            ><tr
              ><td>429</td><td>client limit 또는 provider cooldown</td><td
                ><code>Retry-After</code> 준수</td
              ></tr
            ><tr
              ><td>503</td><td>현재 eligible provider 없음</td><td
                >상태 페이지 확인</td
              ></tr
            ></tbody
          >
        </table>
      </div>
    </section>
    <section id="privacy">
      <p class="eyebrow">07 · PRIVACY</p>
      <h2>내용은 운영 evidence에 저장하지 않습니다</h2>
      <p>
        prompt, tool arguments, image/audio/video, response body와
        Authorization은 DB와 운영 로그에 저장하지 않습니다. request UUID,
        endpoint, status class와 timing만 남깁니다.
      </p>
      <a class="button secondary" href="/security">전체 보안 계약 보기</a>
    </section>
  </article>
</div>

<style>
  summary {
    display: flex;
    align-items: center;
    min-height: 44px;
    cursor: pointer;
  }
  .docs-layout {
    display: grid;
    grid-template-columns: 220px minmax(0, 1fr);
    gap: clamp(25px, 6vw, 80px);
  }
  aside {
    position: sticky;
    top: 20px;
    align-self: start;
    display: grid;
  }
  aside a {
    min-height: 44px;
    padding: 12px;
    color: #9daba4;
    text-decoration: none;
    border-left: 1px solid #33413b;
  }
  aside a:hover {
    color: #dff4bd;
    border-color: #76b900;
  }
  .docs {
    min-width: 0;
    max-width: 820px;
  }
  .docs section {
    scroll-margin-top: 20px;
    padding: 0 0 70px;
  }
  .docs p {
    color: #aab7b0;
    line-height: 1.75;
  }
  .docs code {
    overflow-wrap: anywhere;
  }
  .code-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 44px;
    margin-top: 24px;
    border: 1px solid #2c3833;
    border-bottom: 0;
    padding: 0 12px;
    color: #89968f;
    font-size: 12px;
  }
  .code-head button {
    min-width: 44px;
    min-height: 44px;
    border: 0;
    background: transparent;
    color: #a9d66d;
  }
  .copy-feedback {
    min-height: 1.5em;
    margin: 6px 0;
    color: #d5dfda;
  }
  .error-table {
    max-width: 100%;
    min-width: 0;
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  .multimodal-table {
    min-width: 760px;
  }
  .multimodal-table code {
    white-space: nowrap;
    overflow-wrap: normal;
  }
  .multimodal-contract:focus-visible {
    outline: 3px solid #9cdb45;
    outline-offset: 3px;
  }
  th,
  td {
    border-bottom: 1px solid #2e3a35;
    padding: 14px;
    text-align: left;
  }
  th {
    color: #8e9b94;
    font-size: 11px;
    text-transform: uppercase;
  }
  @media (max-width: 760px) {
    .docs-layout {
      grid-template-columns: 1fr;
    }
    aside {
      position: static;
      grid-template-columns: repeat(2, 1fr);
    }
  }
</style>
