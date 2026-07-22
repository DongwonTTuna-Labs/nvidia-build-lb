# Runbook

## 배포 전

1. `admin_token`(64 hex suffix), `vault_master_key`(32 bytes), `db_password`를 root
   전용 host secret directory에 만들고 `root:root 0600`으로 둔다. Compose secret은
   container 내부 target에서만 `0400`으로 mount한다. host 파일을 `0400`으로 바꾸면
   Rust preflight 계약과 충돌한다.
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

gateway readiness가 false면 loopback에서 `/admin/api/v2/overview`와
`/admin/api/v2/attentions`를 admin bearer로 조회해 reason code와 다음 조치를 확인합니다.
키 plaintext를 로그·명령행·스크린샷에 넣지 않습니다.

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

Live QA는 canonical NVIDIA/NVCF HTTPS origin에서 생성·실행된 run만
`provider_identity=nvidia_hosted`로 기록합니다. mock/custom provider와 migration 전
`unverified` live history는 현재 배포 완료 증거로 집계하지 않습니다.

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
- migration 실행 전 기존 gateway를 quiesce하고
  `SELECT count(*) FROM nblb.request_attempts WHERE finished_at IS NULL AND owner_id IS NULL`
  결과가 0인지 확인한다. owner lease 도입 전 열린 attempt가 하나라도 남아 있으면
  migration은 증거 손상을 피하려고 실패한다. 이 경우 구 gateway가 완전히 종료됐는지
  로그와 연결 상태로 확인하고, 열린 row의 실제 결과를 조사·정리한 뒤 migration을 다시
  실행한다. age만 보고 자동으로 `abandoned_after_restart` 처리하지 않는다.
- upstream 401/402/429/5xx: 해당 key cooldown과 failover evidence를 확인한다.
- UI 404: `/admin` route, Cloudflare tunnel route, app static artifact를 순서대로
  확인한다.

## 복구 원칙

PostgreSQL volume과 vault volume은 스냅샷 정책으로 보존한다. 복구 후에는
`/admin/api/v2/routing/policy`, `/admin/api/v2/requests`와 `/admin/api/v2/probes`에서
cursor 의미·attempt·proof 상태를 확인하고, 실제 NVIDIA 요청은 두 키의 fresh profile
probe가 통과한 뒤에만 재개합니다.

## Persistence와 Hermes E2E

Persistence QA는 `/admin/qa`에서 run을 생성해 `running`을 확인한 뒤 app container만
한 번 재시작합니다. 새 owner ID가 관찰되어야 하며 encrypted key ciphertext,
downstream digest, profile receipt, routing cursor hash가 재시작 전후 같아야 합니다.
resume 판정 오류는 자동으로 terminal `failed`가 됩니다.

Hermes E2E는 UI에서 생성한 `hermes-e2e` run ID를 그대로 전달합니다.
해당 suite는 `live=true`만 생성되며, 다른 queued/running QA가 있으면 API가 409
`qa_run_active`와 `active_run_id`를 반환하므로 그 run을 먼저 확인합니다.

```bash
sudo NBLB_EXPECTED_COMMIT=<배포된_40자리_COMMIT> \
  /usr/local/sbin/nblb-hermes-cutover apply --qa-run <RUN_UUID>
```

helper는 `/opt/agent-apps/data/hermes/.env`와 `config.yaml` secure-file preflight,
snapshot, candidate client 발급/회전, Hermes doctor/exact marker/tool exactly-once,
LB request correlation, rollback rehearsal, 재적용, 이전 cutover client revoke를 수행합니다.
receipt는 `/opt/nvidia-build-lb/hermes-cutover-receipts/<generation>.json`에 root-only로
저장됩니다. run이 30분 안에 terminal이 되지 않으면 새 helper를 임의 재실행하지 말고
run 상태, helper journal, container health를 먼저 확인합니다.

`apply --qa-run`은 exact ID, `hermes-e2e`, `live=true`, `running`, 배포/embedded/expected
commit 일치를 lock과 모든 host mutation 전에 조회합니다. receipt와 `committed` journal이
생긴 뒤 실패한 generation은 rollback하거나 candidate를 revoke하지 않습니다. 다음
동일 `apply --qa-run`은 exact local committed receipt/journal을 검사해 이전 client revoke와
idempotent PASS completion을 끝내고 `reconciled`로 닫습니다. lock을 기다린 동시 apply는
run 상태를 다시 조회하고 먼저 완료된 동일 run의 reconciled receipt를 확인한 뒤 새
generation을 만들지 않고 성공 종료합니다. 이미 PASS가 저장된 뒤 마지막 journal write만
실패한 경우도 동일 generation/app commit completion은 안전하게 재시도됩니다.

cutover와 provider console의 기존 NVIDIA key revoke를 모두 확인한 뒤에만 해당 snapshot을
폐기합니다. `--confirm-provider-revoked`는 provider console 확인을 대신하지 않으며,
운영자가 그 확인을 완료했다는 명시적 선언입니다.

```bash
sudo NBLB_EXPECTED_COMMIT=<배포된_40자리_COMMIT> \
  /usr/local/sbin/nblb-hermes-cutover retire-backup \
  --generation <GENERATION_UUID> --confirm-provider-revoked
```

helper는 reconciled journal, 동일 generation/client의 cutover receipt, snapshot manifest
해시와 embedded commit을 다시 검증합니다. 그 후
`/opt/nvidia-build-lb/hermes-backup-retirement-receipts/<generation>.json`을 먼저 fsync하고
알려진 snapshot 파일만 삭제합니다. receipt가 없는 수동 `rm -rf`는 금지합니다.
