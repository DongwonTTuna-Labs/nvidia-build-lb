# Security contract

- secret은 Docker secret file에서 Rust prestart가 검증·복사하며 일반 환경변수와
  command line으로 전달하지 않는다.
- vault는 AES-256-GCM, downstream credential은 SHA-256 digest만 저장한다.
- admin route는 configured host와 constant-time bearer 비교를 모두 요구한다.
- response/log/evidence DTO는 credential plaintext와 raw Authorization을 포함하지 않는다.
- runtime image는 non-root UID 65532, read-only filesystem, dropped capabilities,
  no-new-privileges로 실행한다.
- Svelte HTML inline bootstrap은 Rust가 산출물 hash를 CSP에 동적으로 추가한다.
- 실제로 노출된 NVIDIA API 키는 폐기된 것으로 취급하며 테스트에 사용하지 않는다.
