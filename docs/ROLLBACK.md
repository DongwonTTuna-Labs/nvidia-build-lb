# Rollback

1. 이전 immutable `NBLB_APP_REGISTRY_DIGEST`와 PostgreSQL digest를 선택한다.
2. `docker compose -f compose.yml up -d migrate`로 schema 호환성을 확인한다.
3. `docker compose -f compose.yml up -d --force-recreate app` 후 `/health/live`,
   `/health/ready`, loopback `/admin/api/v2/overview`를 확인한다.
4. Hermes도 함께 되돌려야 하면 검증된 동일 generation의 `.env`와 `config.yaml` pair만
   root helper journal 절차로 복원하고 `agent-hermes`만 재시작한다.
5. failover·재시작 persistence·Hermes E2E·secret non-exposure smoke가 통과하기 전에는 public tunnel을
   이전 상태로 간주한다.

Rollback은 volume 삭제, `down --volumes`, plaintext 출력, partial Hermes file 교체,
PR merge를 포함하지 않습니다.
