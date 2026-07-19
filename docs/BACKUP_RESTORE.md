# Backup and restore

백업 대상은 PostgreSQL `nblb` schema와 `vault-data` volume이다. 암호화된
ciphertext·nonce·fingerprint만 보관하며 master key와 downstream bearer plaintext는
백업 산출물에 포함하지 않는다.

복구 순서는 다음과 같다.

1. production app을 중지하고 PostgreSQL snapshot을 별도 restore volume에 복원한다.
2. `docker compose -f compose.yml run --rm migrate`로 SQLx migration을 적용한다.
3. app을 기동하고 `/health`, `/admin/api/v1/evidence`, `/admin/api/v1/operator-readiness`를
   확인한다.
4. 각 upstream key를 provider probe로 확인한 뒤에만 downstream traffic을 연다.

복구 실패 시 기존 volume을 삭제하지 않고 restore volume을 격리해 원인을 조사한다.
