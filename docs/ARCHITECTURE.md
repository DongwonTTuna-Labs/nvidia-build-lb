# Architecture

## Runtime graph

```text
Cloudflare tunnel
        ↓
Actix Rust gateway :2456 ── HTTPS ── NVIDIA hosted API
        │
        ├── PostgreSQL (SQLx migrations, durable routing/evidence)
        └── SvelteKit static admin (/admin)
```

`nblb-prestart`가 secret을 runtime tmpfs로 복사한 뒤 gateway와 migration을 UID
65532로 실행한다. gateway는 PostgreSQL 연결이 끊기면 supervisor가 재시작할 수
있도록 fail-closed로 종료한다.

## HTTP surface

- `/health`: readiness와 eligible upstream 수를 secret 없이 반환
- `/v1/models`, `/v1/chat/completions`: OpenAI 호환 text/vision/streaming
- `/v1/embeddings`, `/v1/images/generations`, `/v1/videos/generations`,
  `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/nvidia/inference`: profile별
  modality validation과 provider response validation
- `/admin/api/v1/*`: admin bearer + host boundary를 통과한 운영 DTO
- `/admin`: Svelte static UI. Rust가 build HTML inline bootstrap hash를 계산해
  CSP에 반영한다.

## 상태 모델

라우터는 profile별 cursor를 PostgreSQL에 저장한다. upstream 호출 실패는 outcome에
따라 key counter/cooldown을 갱신하며, 다른 eligible key가 있으면 한 번만 failover
한다. 재시작 시 미완료 attempt는 `abandoned_after_restart`로 닫고 cursor·key 상태를
복원한다.

## 운영 경계

운영 명령은 Rust 바이너리와 `docker compose` 선언만 사용한다. 이미지에는 Node가
runtime dependency로 들어가지 않으며 Svelte build stage에서만 사용한다.
