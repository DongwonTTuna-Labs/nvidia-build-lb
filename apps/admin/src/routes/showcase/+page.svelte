<svelte:head>
  <title>상태 구성요소 · NVIDIA Build LB</title>
  <meta name="description" content="NVIDIA Build LB 관리 화면의 상태와 접근성 구성요소" />
</svelte:head>

<main class="showcase" aria-labelledby="showcase-title">
  <p class="eyebrow">NVIDIA BUILD LB</p>
  <h1 id="showcase-title">상태 구성요소</h1>
  <p class="intro">관리 화면에서 반복되는 준비·주의·오류 상태를 같은 언어와 대비로 표시합니다.</p>
  <section aria-labelledby="status-title">
    <h2 id="status-title">상태 예시</h2>
    <div class="grid">
      <article><span class="status good" aria-label="정상"><span aria-hidden="true">●</span> 요청 가능</span><p>현재 검증된 모델이 요청을 받을 수 있습니다.</p></article>
      <article><span class="status warning" aria-label="주의"><span aria-hidden="true">▲</span> 확인 필요</span><p>다음 작업을 진행하기 전에 증거를 확인하세요.</p></article>
      <article><span class="status error" aria-label="오류"><span aria-hidden="true">■</span> 확인 실패</span><p>새 작업을 시작하지 말고 상태를 다시 확인하세요.</p></article>
    </div>
  </section>
</main>

<style>
  :global(*) { box-sizing: border-box; }
  :global(html) { background: #0b0d0e; color: #f5f7f6; }
  :global(body) { margin: 0; font-family: system-ui, sans-serif; line-height: 1.5; }
  .showcase { max-width: 72ch; margin: 0 auto; padding: 32px 16px; }
  .eyebrow { color: #76b900; font-size: .75rem; font-weight: 800; letter-spacing: .12em; }
  h1, h2, p { margin: 0; }
  h1 { margin-top: 8px; font-size: clamp(1.75rem, 4vw, 2.25rem); }
  .intro { margin-top: 12px; color: #d0d5d2; }
  section { margin-top: 32px; }
  h2 { font-size: 1.25rem; }
  .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
  article { display: grid; gap: 12px; min-height: 148px; padding: 16px; background: #171a1d; border: 1px solid #6b746f; border-radius: 8px; }
  article p { color: #d0d5d2; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-weight: 800; }
  .good { color: #76b900; }
  .warning { color: #ffd166; }
  .error { color: #ff8a8a; }
  @media (max-width: 767px) { .showcase { padding-block: 24px; } .grid { grid-template-columns: 1fr; } }
  @media (forced-colors: active) { article { border-color: ButtonText; } .good, .warning, .error { color: ButtonText; } }
</style>
