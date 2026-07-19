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

gateway readiness가 false면 `/admin/api/v1/operator-readiness`를 admin bearer로
조회해 `readiness_cause`와 다음 조치를 확인한다. 키 plaintext를 로그·명령행·스크린샷에
넣지 않는다.

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
