# Rollback

1. 이전 immutable `NBLB_APP_REGISTRY_DIGEST`와 PostgreSQL digest를 선택한다.
2. `docker compose -f compose.yml up -d migrate`로 schema 호환성을 확인한다.
3. `docker compose -f compose.yml up -d --force-recreate app` 후 `/health`와 admin
   readiness를 확인한다.
4. failover·재시작·secret non-exposure smoke가 통과하기 전에는 public tunnel을
   이전 상태로 간주한다.

Rollback은 데이터 삭제나 PR merge를 포함하지 않는다.
