# Backup and restore

백업 대상은 PostgreSQL `nblb` schema와 `vault-data` volume이다. app/migrate를 먼저
quiesce한 뒤 하나의 불변 `snapshot_id`로 DB dump와 vault snapshot을 함께 기록하고,
각 파일의 SHA-256 manifest를 fsync한다. 암호화된 ciphertext·nonce·fingerprint만
온라인 backup bundle에 보관하며 downstream bearer plaintext는 포함하지 않는다.

`vault_master_key`는 DB/vault bundle과 분리된 sealed offline custody에 보관한다. 이
별도 사본은 최소 두 명시적 복구 책임자 또는 동등한 복구 통제 아래 checksum, 생성 시각,
대상 `snapshot_id`를 기록하고 평시 host와 backup storage 어느 쪽에도 평문으로 두지 않는다.
master key 사본이 없으면 ciphertext는 복구할 수 없으므로 DB/vault backup만 성공한 상태를
복구 가능 백업으로 보고하지 않는다.

복구 순서는 다음과 같다.

1. production app과 migrate를 중지하고 동일 `snapshot_id`의 DB dump·vault snapshot
   manifest와 checksum을 검증해 별도 restore volume에 복원한다.
2. sealed offline custody에서 정확한 `snapshot_id`에 대응하는 `vault_master_key`를
   root-only host secret으로 복원하고 `root:root 0600`을 확인한다. 이 값은 로그,
   command argv, DB/vault bundle에 복사하지 않는다.
3. `docker compose -f compose.yml run --rm migrate`로 SQLx migration을 적용한다.
4. app을 기동하고 `/health/live`, `/health/ready`, `/admin/api/v2/overview`,
   `/admin/api/v2/requests`, `/admin/api/v2/probes`를 확인한다.
5. 각 upstream key를 provider probe로 확인한 뒤에만 downstream traffic을 연다.
6. `/admin/qa` persistence run을 arm하고 app을 실제 재시작해 ciphertext/client
   digest/profile receipt/routing cursor 보존과 새 owner lease를 확인한다.

Hermes cutover backup과 receipt는 DB/vault backup과 별도로 root-only 보존합니다.
restore 뒤 helper를 실행할 때는 새 QA run ID와 복원된 배포의 정확한 embedded commit을
사용합니다. `.env` 또는 `config.yaml` 하나만 수동 복원하는 partial rollback은 금지합니다.
provider에서 기존 직접 NVIDIA key revoke를 확인하고 generation이 reconciled인 경우에만
`nblb-hermes-cutover retire-backup --generation <UUID> --confirm-provider-revoked`로
upstream-bearing snapshot을 폐기합니다. durable retirement receipt 없는 수동 삭제는
복구·감사 증거를 깨뜨리므로 금지합니다.

복구 실패 시 기존 volume을 삭제하지 않고 restore volume을 격리해 원인을 조사한다.
