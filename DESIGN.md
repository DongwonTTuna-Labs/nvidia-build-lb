# NVIDIA Build LB canonical design

이 문서는 구현·리뷰·QA가 공유하는 기준축이다. 구현 소스는 Rust와
Svelte/TypeScript로만 유지하며, 폐기된 레거시 계약을 복구하지 않는다.

## 사용자 여정

1. `/admin`에서 현재 readiness와 다음 조치를 한 화면에서 확인한다.
2. upstream 키를 한 번만 입력하고, 저장·probe·enable 순서가 명시적으로 보인다.
3. `/v1/models`에서 제공 가능한 profile과 modality를 확인한다.
4. OpenAI 호환 chat/streaming 및 embeddings·images·videos·audio 요청은 downstream
   scope와 profile compatibility를 검증한 후 두 키에 공정하게 분산한다.
5. 실패 시 민감한 본문/키는 노출하지 않고 cooldown·failover·재시도 결과만 evidence로
   남긴다.

## 경계와 권위

- PostgreSQL `nblb.upstream_keys`, `downstream_credentials`, `routing_state`,
  `request_attempts`가 재시작 후에도 유지되는 권위 상태다.
- AES-256-GCM ciphertext와 nonce만 저장하고 API·UI·로그에는 credential plaintext를
  반환하지 않는다.
- Rust `nblb-prestart`가 Docker secret의 형상·소유자·권한을 확인하고 UID 65532로
  gateway/migration을 exec한다.
- Rust `nblb-healthcheck`와 `pg_isready`가 컨테이너 readiness를 판단한다.
- `request_attempts.owner_id`와 `gateway_instances.last_seen_at`가 프로세스 lease를
  나타낸다. 재시작 정리는 30초 이상 heartbeat가 끊긴 owner의 미완료 시도만 닫고,
  살아 있는 다른 gateway의 streaming attempt는 건드리지 않는다.

## UI 원칙

정보 우선순위는 `지금 상태 → 막힌 이유 → 다음 조치 → 상세 evidence` 순서다.
모든 상태는 텍스트와 색 대비를 함께 제공하고, 키 plaintext는 발급 응답 한 번에서만
보인다. SvelteKit generated JS는 브라우저 실행 artifact이며 소스 계약이 아니다.

## 검증 계약

- 변경된 Rust crate만 먼저 테스트한다.
- Svelte 변경은 `svelte-check`와 build를 먼저 실행한다.
- Docker/compose 변경은 해당 image build와 `docker compose -f compose.yml config`를
  실행한다.
- publish workflow의 digest 출력은 `sha256:` 없는 64자리 hex이고, Compose가
  immutable image reference에 단 한 번만 접두사를 붙인다.
- 최종 merge 전 한 번만 전체 workspace·정적 검사·라이브 NVIDIA/Cloudflare/Hermes
  증거를 수집한다. PR은 자동 merge하지 않는다.
