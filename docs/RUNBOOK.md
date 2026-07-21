# Runbook

## 배포 전

1. `admin_token`(64 hex suffix), `vault_master_key`(32 bytes), `db_password`를 root
   전용 secret directory에 만들고 각각 0400으로 둔다.
2. `docker compose -f compose.yml config`로 digest와 secret 경로를 확인한다.
   publish workflow의 `NBLB_*_REGISTRY_DIGEST`는 `sha256:` 없는 raw hex를 사용한다.
3. `cargo test --workspace`와 Svelte check/build를 최종 게이트에서 실행한다.

## 기동/관찰

```bash
docker compose -f compose.yml up -d
docker compose -f compose.yml ps
docker compose -f compose.yml logs --since 10m app migrate db
curl -fsS https://nvidia-lb.dongwontuna.net/health
```

컨테이너 상태와 traffic readiness는 분리합니다.

```bash
curl -fsS http://127.0.0.1:2456/health/live
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2456/health/ready
```

첫 명령은 process가 살아 있으면 key 수와 무관하게 `200`입니다. 두 번째와 호환
`/health`는 DB와 eligible upstream이 있어야 `200`이며 초기 설정 중에는 `503`이
정상입니다.

gateway readiness가 false면 `/admin/api/v1/operator-readiness`를 admin bearer로
조회해 `readiness_cause`와 다음 조치를 확인한다. 키 plaintext를 로그·명령행·스크린샷에
넣지 않는다.

## 운영 증거와 보존

gateway는 5초마다 terminal `proxy_requests`를 `metric_buckets_minute`에 batch로
집계하고, 한 시간마다 retention을 적용한다. 두 worker는 PostgreSQL transaction-scoped
advisory lock을 사용하므로 여러 gateway instance가 동시에 떠 있어도 한 instance만
작업한다. 정상 상태에서는 완료된 request의 `rolled_up_at`이 채워지고 동일 request를
다시 집계해도 bucket count가 늘지 않는다. rollup과 retention은 같은 maintenance
lock을 사용하며, 30일이 지난 request도 `rolled_up_at`이 비어 있으면 90일 metric
보존 경계까지 삭제하지 않는다.

- terminal request와 attempt: 30일
- minute metric과 완료 probe: 90일
- audit event와 완료 QA run: 180일

열린 row는 retention 대상이 아니다. owner heartbeat가 30초보다 오래 끊긴 gateway의
열린 request와 attempt는 watchdog이 `abandoned_after_restart`로 닫는다. 이 정리가
반복 실패하면 gateway는 fail-closed로 종료한다.

장애 조사에는 request UUID, endpoint/profile, outcome/status/error class, timing과
attempt 순번만 사용한다. prompt, media, tool argument, body/header, plaintext credential을
SQL, 로그, incident update 또는 QA evidence에 복사하지 않는다. DB dump나 log scan에서
이 값이 발견되면 운영 증거를 공유하지 말고 즉시 노출 사고로 취급한다.

## 장애

- DB unhealthy: app은 watchdog으로 종료된다. DB를 먼저 복구하고 `migrate` 완료 후
  app을 재기동한다.
- migration 실행 전 기존 gateway를 quiesce한다. owner lease 도입 전 생성된
  `started` attempt는 migration에서 `abandoned_after_restart`로 한 번 정리되므로,
  구 프로세스가 streaming 중인 상태에서 migration을 병행하지 않는다.
- upstream 401/402/429/5xx: 해당 key cooldown과 failover evidence를 확인한다.
- UI 404: `/admin` route, Cloudflare tunnel route, app static artifact를 순서대로
  확인한다.

## 복구 원칙

PostgreSQL volume과 vault volume은 스냅샷 정책으로 보존한다. 복구 후에는
`/admin/api/v1/evidence`에서 routing cursor와 attempt 상태를 확인하고, 실제 NVIDIA
요청은 두 키 probe가 통과한 뒤에만 재개한다.
