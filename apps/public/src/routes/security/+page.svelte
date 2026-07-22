<svelte:head
  ><title>보안과 개인정보 · NVIDIA Build LB</title><meta
    name="description"
    content="NVIDIA Build LB가 저장하는 데이터와 저장하지 않는 데이터, 공개/admin 경계."
  /></svelte:head
>
<header class="page-head">
  <p class="eyebrow">SECURITY & PRIVACY</p>
  <h1>비밀과 내용은 숨기고,<br />운영 증거만 남깁니다.</h1>
  <p class="lede">
    보안 경계를 모호한 약속이 아니라 저장 형식, network boundary와 retention
    규칙으로 설명합니다.
  </p>
</header>
<section class="section grid principles">
  <article class="panel">
    <span>01</span>
    <h2>Provider credential</h2>
    <p>
      NVIDIA key는 AES-256-GCM ciphertext와 random nonce로만 PostgreSQL에
      저장합니다. master key는 root-only secret mount에 있습니다.
    </p>
  </article>
  <article class="panel">
    <span>02</span>
    <h2>Downstream token</h2>
    <p>
      token plaintext는 생성·rotate 응답에서 한 번만 표시합니다. 이후에는
      digest와 non-secret prefix만 조회할 수 있습니다.
    </p>
  </article>
  <article class="panel">
    <span>03</span>
    <h2>Admin boundary</h2>
    <p>
      관리 UI/API는 loopback host에서만 열립니다. 공개 hostname의 <code
        >/admin*</code
      >는 인증 화면이 아니라 빈 404입니다.
    </p>
  </article>
</section>
<section class="section compare">
  <article class="panel">
    <p class="eyebrow">NEVER STORED</p>
    <h2>저장하지 않는 데이터</h2>
    <ul>
      <li>prompt와 messages</li>
      <li>tool arguments</li>
      <li>image URL·bytes</li>
      <li>audio·video bytes</li>
      <li>generated output</li>
      <li>provider response body</li>
      <li>Authorization·cookie·request headers</li>
    </ul>
  </article>
  <article class="panel">
    <p class="eyebrow">SANITIZED EVIDENCE</p>
    <h2>저장하는 운영 정보</h2>
    <ul>
      <li>server-generated request UUID</li>
      <li>endpoint와 model profile</li>
      <li>stream/modality boolean</li>
      <li>status와 normalized error class</li>
      <li>duration·TTFB·bytes count</li>
      <li>attempt 순서와 failover count</li>
      <li>aggregate metric과 incident update</li>
    </ul>
  </article>
</section>
<section class="section">
  <div class="panel retention">
    <div>
      <p class="eyebrow">RETENTION</p>
      <h2>필요한 기간만</h2>
      <p>
        request evidence 기본 30일, aggregate metric 기본 90일입니다. cleanup은
        PostgreSQL advisory lock을 가진 단일 worker가 처리합니다.
      </p>
    </div>
    <a class="button secondary" href="/docs#privacy"
      >연결 가이드의 privacy 계약</a
    >
  </div>
</section>

<style>
  .principles article {
    grid-column: span 4;
  }
  .principles span {
    color: #76b900;
    font-weight: 900;
  }
  .principles h2 {
    margin: 20px 0 12px;
  }
  .principles p,
  .compare li,
  .retention p {
    color: #a7b3ad;
    line-height: 1.7;
  }
  .compare {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px;
  }
  .compare ul {
    display: grid;
    gap: 11px;
    padding-left: 20px;
  }
  .retention {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 30px;
  }
  .retention p {
    max-width: 700px;
    margin-bottom: 0;
  }
  @media (max-width: 760px) {
    .principles article {
      grid-column: 1/-1;
    }
    .compare {
      grid-template-columns: 1fr;
    }
    .retention {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
