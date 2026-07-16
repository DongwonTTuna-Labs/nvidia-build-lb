"""Static candidate image, secret prestart, compose, and QA recipe contract."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts.qa.source_manifest import build_manifest

_ROOT = Path(__file__).resolve().parents[2]


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_candidate_dockerfile_is_multistage_and_has_a_nonroot_runtime_handoff() -> None:
    dockerfile = _text("Dockerfile")

    assert dockerfile.count("FROM ") >= 2
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/nblb-app-entrypoint"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "65532" in dockerfile
    assert dockerfile.count("python:3.13.14-alpine3.23@sha256:") == 2
    assert "ghcr.io/astral-sh/uv:0.11.24@sha256:" in dockerfile
    assert dockerfile.startswith("# syntax=docker/dockerfile:1.18@sha256:")
    assert "apt-get" not in dockerfile
    assert "SOURCE_DATE_EPOCH=0" in dockerfile
    assert "PYTHONHASHSEED=0" in dockerfile
    assert "UV_COMPILE_BYTECODE=0" in dockerfile
    assert "UV_COMPILE_BYTECODE=1" not in dockerfile
    assert "find .venv -type d -name __pycache__" in dockerfile
    assert "-name '*.pyc' -o -name '*.pyo'" in dockerfile
    assert 'rm "$dist_info/uv_cache.json"' in dockerfile
    assert "sed -i '\\#/uv_cache.json,#d'" in dockerfile
    assert "libcap-ng=0.8.5-r0 setpriv=2.41.4-r0" in dockerfile
    assert "/sbin/apk add --no-cache" in dockerfile
    assert '"/bin/setpriv", "--reuid=65532"' in dockerfile
    assert '"/usr/local/bin/nblb-healthcheck", "65532", "65532", "app"' in dockerfile
    # Dockerfile frontend 1.18 serializes EXPOSE with a process-local pointer in
    # image history, making an otherwise identical local candidate ID drift.
    # Compose owns the actual loopback-only publication contract.
    assert "\nEXPOSE " not in dockerfile


def test_app_and_postgres_prestarts_copy_only_fixed_secret_names() -> None:
    app = _text("docker/app-entrypoint.sh")
    database = _text("docker/postgres-prestart.sh")
    database_dockerfile = _text("docker/postgres.Dockerfile")
    fake_upstream = _text("scripts/qa/fake-entrypoint.sh")

    assert "admin_token" in app
    assert "vault_master_key" in app
    assert "db_password" in app
    assert "setpriv" in app
    assert "--clear-groups" in app
    assert "--bounding-set=-all" in app
    assert "0:0:444" in app
    assert "db_password" in database
    assert "docker-entrypoint.sh" in database
    assert "prestart_failed" in app
    assert "prestart_failed" in database
    assert app.index('chmod 0400 "$temporary"') < app.index('chown 65532:65532 "$temporary"')
    assert database.index('chmod 0400 "$temporary"') < database.index('chown 70:70 "$temporary"')
    assert "export POSTGRES_PASSWORD_FILE=$destination" in database
    assert 'chown 65532:65532 "$runtime"' in app
    assert "export HOME=/" in app
    assert 'chown 70:70 "$runtime"' in database
    assert "exec /bin/setpriv" in database
    assert "--clear-groups" in database
    assert "--bounding-set=-all" in database
    assert "70:70:700" in database
    assert "0:0:444" in fake_upstream
    assert database_dockerfile.startswith("# syntax=docker/dockerfile:1.18@sha256:")
    for package in (
        "libcrypto3=3.5.7-r0",
        "libssl3=3.5.7-r0",
        "libxml2=2.13.9-r1",
        "libcap-ng=0.8.5-r0",
        "setpriv=2.41.4-r0",
    ):
        assert package in database_dockerfile
    assert "rm /usr/local/bin/gosu" in database_dockerfile
    assert "test ! -e /usr/local/bin/gosu" in database_dockerfile
    assert "util-linux" not in database_dockerfile
    assert "/bin/busybox" in database_dockerfile
    assert fake_upstream.index('chmod 0400 "$destination"') < fake_upstream.index(
        'chown 65532:65532 "$destination"'
    )
    assert 'chown 65532:65532 "$runtime"' in fake_upstream


def test_qa_compose_is_labelled_bounded_and_does_not_publish_production_port() -> None:
    compose = _text("compose.qa.yml")
    proxy = _text("scripts/qa/loopback_proxy.py")
    healthcheck = _text("docker/healthcheck.sh")
    verify = _text("scripts/qa/verify-local.sh")

    assert compose.count("${NBLB_QA_TASK_LABEL:-todo6a-candidate}") == 8
    assert "127.0.0.1:${NBLB_QA_PORT:-32456}:2456" in compose
    assert "NVIDIA_BUILD_LB_PUBLIC_PORT: ${NBLB_QA_PUBLIC_PORT:-2456}" in compose
    assert "127.0.0.1:2456:2456" not in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert compose.count("- SETPCAP") == 5
    assert "read_only: true" in compose
    assert "tmpfs:" in compose
    assert "service_completed_successfully" in compose
    assert "loopback_proxy.py" in compose
    assert "internal: true" in compose
    assert '_TARGET_HOST = "app"' in proxy
    assert "integrate.api.nvidia.com" not in proxy
    assert "nblb-loopback-entrypoint" in compose
    assert "--clear-groups" in _text("scripts/qa/loopback-entrypoint.sh")
    assert '"/bin/setpriv", "--reuid=70"' in compose
    assert compose.count('"/bin/setpriv", "--reuid=65532"') == 2
    assert '"65532", "65532", "fake"' in compose
    assert '"65532", "65532", "loopback"' in compose
    assert '"70", "70", "postgres"' in compose
    assert "CapEff:" in healthcheck
    assert "CapBnd:" in healthcheck
    assert "NoNewPrivs:" in healthcheck
    assert "valid = valid && NF == 1" in healthcheck
    assert "NVIDIA_BUILD_LB_PUBLIC_PORT" in healthcheck
    assert "urllib.request.Request(" in healthcheck
    assert "'http://127.0.0.1:2456/health'" in healthcheck
    assert "headers={'Host': f'127.0.0.1:{port}'}" in healthcheck
    assert "port.isascii()" in healthcheck
    assert "port.isdecimal()" in healthcheck
    assert "not port.startswith('0')" in healthcheck
    assert 'NBLB_QA_PUBLIC_PORT="$PRIMARY_PORT"' in verify
    assert 'NBLB_QA_PUBLIC_PORT="$RESTORE_PORT"' in verify
    assert 'header "Host: 127.0.0.1:$port"' in verify
    assert 'printf \'header = "Host: 127.0.0.1:%s"\\n\' "$port"' in verify
    assert '"$CLIENT_DIR/persistence-restore.curl" "$RESTORE_PORT"' in verify
    assert '"$CLIENT_DIR/admin-next-restore.curl" "$RESTORE_PORT"' in verify
    assert "Host: 127.0.0.1:2456" not in verify


def test_production_keeps_internal_bind_separate_from_public_authority() -> None:
    production = _text("src/nvidia_build_lb/production.py")
    compose = _text("compose.yml")

    assert "port=2456" in production
    assert "port=settings.public_port" not in production
    assert "127.0.0.1:${NBLB_PORT:-2456}:2456" in compose
    assert "NVIDIA_BUILD_LB_PUBLIC_PORT: ${NBLB_PORT:-2456}" in compose


def test_build_candidate_recipe_owns_evidence_and_cleanup() -> None:
    script = _text("scripts/qa/build-candidate.sh")
    stage_evidence = _text("scripts/qa/admin-stage-evidence.sh")

    assert "manual-qa.json" in script
    assert "adversarial.json" in script
    assert "cleanup.json" in script
    assert "docker compose" in script
    assert "down --volumes --remove-orphans" in script
    assert "trap cleanup EXIT" in script
    assert "trap 'exit 129' HUP" in script
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert script.index("trap cleanup EXIT") < script.index("SECRET_DIR=$(mktemp")
    assert 'SECRET_DIR=""' in script
    assert 'CLIENT_DIR=""' in script
    assert 'chmod -R u+w -- "$CLIENT_DIR"' in script
    assert script.index('chmod -R u+w -- "$CLIENT_DIR"') < script.index('rm -rf -- "$CLIENT_DIR"')
    assert "trigger_exit_status" in script
    assert "final_exit_status" in script
    assert "temp_secret_directories:$secret_directories" in script
    assert "temp_client_directories:$client_directories" in script
    assert 'mkdir -p "$(dirname "$EVIDENCE_DIR")"' in script
    assert 'exec 9>"$EVIDENCE_DIR.claim"' in script
    assert "if ! flock -n 9; then" in script
    assert 'if [ -e "$EVIDENCE_DIR" ] || ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then' in script
    assert 'mkdir -p "$EVIDENCE_DIR"' not in script
    assert "candidate_postgres_image_digest" in script
    assert 'docker image rm "$POSTGRES_TAG"' not in script
    assert "curl sha256sum ss uv" in script
    assert "NBLB_QA_PORT" in script
    assert "export NBLB_QA_PUBLIC_PORT=$QA_PORT" in script
    assert 'header "Host: 127.0.0.1:$QA_PORT"' in script
    assert 'printf \'header = "Host: 127.0.0.1:%s"\\n\' "$QA_PORT"' in script
    assert "Host: 127.0.0.1:2456" not in script
    assert '--argjson qa_port "$QA_PORT"' in script
    assert "qa_port:$qa_port" in script
    assert "compose up --detach --wait" not in script
    assert "wait_bootstrap_degraded" in script
    assert "NBLB_QA_TASK_LABEL" not in script
    assert 'source "$ROOT/scripts/qa/admin-stage-evidence.sh"' in script
    assert script.count('write_admin_stage_evidence "$EVIDENCE_DIR"') == 3
    assert script.count('assert_admin_stage_evidence "$EVIDENCE_DIR"') == 3
    assert "safe_error_code=unavailable" in stage_evidence
    assert 'test("^[a-z_]+$")' in stage_evidence
    assert "safe_error_code=none" in stage_evidence
    assert "admin-stage-${stage}.json" in stage_evidence
    assert "response_file" in stage_evidence
    assert "response_body" not in stage_evidence


def test_admin_stage_evidence_persists_only_bounded_safe_results(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    helper = _ROOT / "scripts/qa/admin-stage-evidence.sh"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    response = tmp_path / "response.json"

    def record(stage: str, expected: str, observed: str, valid: str) -> None:
        completed = subprocess.run(  # noqa: S603 - resolved shell and fixed helper.
            [
                bash,
                "-c",
                'source "$1"; write_admin_stage_evidence "$2" "$3" "$4" "$5" "$6" "$7"',
                "admin-stage-evidence-test",
                str(helper),
                str(evidence),
                stage,
                expected,
                observed,
                valid,
                str(response),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == ""

    _ = response.write_text("{}", encoding="utf-8")
    record("create", "201", "201", "true")
    success = (evidence / "admin-stage-create.json").read_text(encoding="utf-8")
    assert (
        success
        == json.dumps(
            {
                "schema_version": 1,
                "stage": "create",
                "expected_http": 201,
                "observed_http": 201,
                "contract_valid": True,
                "safe_error_code": "none",
                "status": "PASS",
            },
            separators=(",", ":"),
        )
        + "\n"
    )

    _ = response.write_text(
        json.dumps(
            {
                "error": {
                    "code": "resource_conflict",
                    "message": "sensitive detail",
                    "request_id": "request",
                }
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    record("probe", "200", "409", "false")
    failed = (evidence / "admin-stage-probe.json").read_text(encoding="utf-8")
    assert (
        failed
        == json.dumps(
            {
                "schema_version": 1,
                "stage": "probe",
                "expected_http": 200,
                "observed_http": 409,
                "contract_valid": False,
                "safe_error_code": "resource_conflict",
                "status": "FAIL",
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    assert "sensitive detail" not in failed
    assert "request" not in failed

    _ = response.write_text('{"error":{"code":"NOT_SAFE"}}', encoding="utf-8")
    record("enable", "204", "000", "false")
    unavailable = (evidence / "admin-stage-enable.json").read_text(encoding="utf-8")
    assert (
        unavailable
        == json.dumps(
            {
                "schema_version": 1,
                "stage": "enable",
                "expected_http": 204,
                "observed_http": None,
                "contract_valid": False,
                "safe_error_code": "unavailable",
                "status": "FAIL",
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def test_build_candidate_probes_before_enable_and_persists_stage_contracts() -> None:
    script = _text("scripts/qa/build-candidate.sh")

    create = script.index('"http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys")')
    probe = script.index('"http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys/$key_id/probe")')
    enable = script.index('"http://127.0.0.1:$QA_PORT/admin/api/v1/upstream-keys/$key_id/enable")')
    assert create < probe < enable < script.index("wait_healthy app")
    assert '.id == $key_id and .enabled == false and .probe_status == "valid"' in script
    assert 'write_admin_stage_evidence "$EVIDENCE_DIR" create 201' in script
    assert 'write_admin_stage_evidence "$EVIDENCE_DIR" probe 200' in script
    assert 'write_admin_stage_evidence "$EVIDENCE_DIR" enable 204' in script
    assert "admin_upstream_create_contract:true" in script
    assert "admin_upstream_probe_contract:true" in script
    assert "admin_upstream_enable_contract:true" in script
    assert "admin_stage_evidence_persisted:true" in script


def test_build_candidate_recipe_owns_deterministic_runtime_gate() -> None:
    script = _text("scripts/qa/build-candidate.sh")

    assert script.count("docker buildx build --pull --no-cache --provenance=false") == 2
    assert script.count("--build-arg SOURCE_DATE_EPOCH=0") == 2
    assert script.count("rewrite-timestamp=true") == 2
    assert script.count("docker load --input") == 2
    assert "uv run python -m scripts.qa.source_manifest" in script
    assert "uv run python -m scripts.qa.source_snapshot" in script
    assert '"$CLIENT_DIR/source-snapshot"' in script
    assert 'build_context:"manifest-bound-read-only-snapshot"' in script
    assert 'cmp "$CLIENT_DIR/source-manifest-before.json"' in script
    assert "postgres_image_digest" in script
    assert 'assert_image_metadata_secret_free "$IMAGE_TAG"' in script
    assert "docker image inspect --format '{{json .Config}}'" in script
    assert "docker history --no-trunc" in script
    assert 'chmod 0444 "$metadata_file"' in script
    for name in ("admin_token", "vault_master_key", "db_password", "server_key"):
        assert f"source=$SECRET_DIR/{name},target=/canonical-secrets/{name},readonly" in script
    assert '--mount "type=bind,source=$metadata_file,target=/metadata/image.txt,readonly"' in script
    assert 'for name in ("admin_token", "vault_master_key", "db_password", "server_key")' in script
    assert ".HostConfig.Tmpfs" in script
    assert "rw,noexec,nosuid,nodev,size=64k,mode=0700,uid=0,gid=0" in script
    assert script.count("-eo uid,pid,comm") == 1
    assert script.count("/^(Uid|Gid|Groups|CapEff|CapBnd|NoNewPrivs):/") == 4
    assert script.count("'^NoNewPrivs:[[:space:]]+1$'") == 4
    assert "steady_state_no_new_privileges:true" in script
    assert 'wait_process_owner "$app_container" 65532' in script
    assert 'wait_process_owner "$db_container" 70' in script
    assert 'wait_process_owner "$proxy_container" 65532' in script
    assert 'stable_samples" -ge 3' in script
    assert 'docker exec --user 65532 "$app_container" touch' in script
    assert 'docker exec --user 70 "$db_container" touch' in script
    assert "! docker exec" not in script
    assert script.count(" test ! -r /run/") == 4
    assert script.count(" test ! -e /run/nvidia-build-lb/secrets/.qa-marker") == 2
    assert script.count('assert_runtime_secret_exact "$app_container"') == 6
    assert script.count('assert_runtime_secret_exact "$db_container"') == 2
    assert "ADMIN_TOKEN_SHA256=$(sha256sum" in script
    assert "VAULT_MASTER_KEY_SHA256=$(sha256sum" in script
    assert "DB_PASSWORD_SHA256=$(sha256sum" in script
    assert '[[ "$expected_digest" =~ ^[0-9a-f]{64}$ ]]' in script
    assert "runtime_secret_content_exact:true" in script
    assert "restart_runtime_secret_content_exact:true" in script
    assert script.index("wait_bootstrap_degraded", script.index("COMPOSE_STARTED")) < script.index(
        "upstream.json"
    )
    assert "run_postgres_missing_db_secret" in script
    assert "prestart_failed:source_missing" in script
    assert "missing_app_database_secret_exit" in script
    assert "missing_postgres_database_secret_exit" in script
    assert script.count("--cap-add SETPCAP") == 2
    assert (
        script.count("assert_healthcheck_identity"),
        script.count('assert_healthcheck_identity "$app_container" /bin/setpriv'),
        script.count('assert_healthcheck_identity "$fake_container" /bin/setpriv'),
        script.count('assert_healthcheck_identity "$proxy_container" /bin/setpriv'),
        "/usr/bin/setpriv" in script,
    ) == (5, 1, 1, 1, False)
    assert "2455" not in script


def test_source_manifest_uses_git_surface_and_observes_mode_type_and_payload(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    assert git is not None
    _ = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "init", "-q"], cwd=tmp_path, check=True
    )
    _ = (tmp_path / ".gitignore").write_text(".coverage\n.omo/\n", encoding="utf-8")
    candidate = tmp_path / "candidate.sh"
    _ = candidate.write_text("echo candidate\n", encoding="utf-8")
    candidate.chmod(0o644)
    _ = (tmp_path / ".coverage").write_text("runtime output", encoding="utf-8")
    hidden = tmp_path / ".omo"
    hidden.mkdir()
    _ = (hidden / "evidence.json").write_text("ignored", encoding="utf-8")

    baseline = build_manifest(tmp_path)
    paths = {entry["path"] for entry in baseline["entries"]}
    assert paths == {".gitignore", "candidate.sh"}

    _ = (tmp_path / ".coverage").write_text("changed runtime output", encoding="utf-8")
    assert build_manifest(tmp_path)["source_tree_sha256"] == baseline["source_tree_sha256"]

    candidate.chmod(0o600)
    assert build_manifest(tmp_path)["source_tree_sha256"] == baseline["source_tree_sha256"]

    candidate.chmod(0o755)
    executable = build_manifest(tmp_path)
    assert executable["source_tree_sha256"] != baseline["source_tree_sha256"]

    candidate.chmod(0o700)
    assert build_manifest(tmp_path)["source_tree_sha256"] == executable["source_tree_sha256"]

    candidate.chmod(0o4755)
    with pytest.raises(ValueError, match="special mode bits"):
        _ = build_manifest(tmp_path)
    candidate.chmod(0o755)

    _ = (tmp_path / "candidate-link").symlink_to("candidate.sh")
    linked = build_manifest(tmp_path)
    link_entry = next(entry for entry in linked["entries"] if entry["path"] == "candidate-link")
    assert link_entry["type"] == "symlink"
    assert linked["source_tree_sha256"] != executable["source_tree_sha256"]
