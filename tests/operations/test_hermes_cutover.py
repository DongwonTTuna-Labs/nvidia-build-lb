# pyright: reportPrivateUsage=false, reportUnusedCallResult=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID

import pytest
from scripts.ops import hermes_cutover
from scripts.qa import live_smoke


def _token() -> str:
    return "nblb_ds_" + ("a" * 64)


def _settings(root: Path) -> hermes_cutover.Settings:
    return hermes_cutover.Settings(
        data_dir=root / "mounted-hermes",
        state_root=root / "host-only-state",
        agent_compose=Path("/fixture/agent-compose"),
        admin_token_file=Path("/fixture/admin"),
        api_env_file=Path("/fixture/api"),
        docker="docker",
        lb_base_url="http://127.0.0.1:2456",
        hermes_base_url="http://127.0.0.1:8642",
        container_name="agent-hermes",
    )


def _set_manifest_status(
    backup: Path,
    status_value: str,
    *,
    env_payload: bytes | None = None,
) -> None:
    manifest_path = backup / "manifest.json"
    manifest = hermes_cutover._json_body(manifest_path.read_bytes())
    manifest["status"] = status_value
    if env_payload is not None:
        env_path = backup / "env.before"
        env_path.write_bytes(env_payload)
        env_path.chmod(0o600)
        source = manifest.get("source")
        assert isinstance(source, dict)
        env_state = source.get("env")
        assert isinstance(env_state, dict)
        env_state["sha256"] = hashlib.sha256(env_payload).hexdigest()
    hermes_cutover._atomic_json(manifest_path, manifest)


def test_env_replacement_removes_direct_upstream_credential() -> None:
    upstream = "nvapi-" + ("x" * 40)
    original = f"OTHER=value\nNVIDIA_API_KEY={upstream}\n".encode()

    replaced = hermes_cutover._replace_env(original, _token())

    assert upstream.encode() not in replaced
    assert replaced.count(b"NVIDIA_API_KEY=") == 1


def test_model_replacement_changes_only_top_level_model_block() -> None:
    original = (
        b"model:\n"
        b"  default: old\n"
        b"  provider: old-provider\n"
        b"  base_url: https://example.invalid/v1\n"
        b"auxiliary:\n"
        b"  provider: preserve-me\n"
    )

    replaced = hermes_cutover._replace_model_config(original)

    assert b"  default: z-ai/glm-5.2\n" in replaced
    assert b"  provider: nvidia\n" in replaced
    assert b"  base_url: http://127.0.0.1:2456/v1\n" in replaced
    assert b"  provider: preserve-me\n" in replaced


def test_fixture_rehearsal_replaces_and_rolls_back_pair(tmp_path: Path) -> None:
    receipt = hermes_cutover._rehearse(tmp_path / "happy")

    assert receipt == {
        "status": "PASS",
        "operation": "rehearse",
        "atomic_replace": True,
        "pair_rollback": True,
        "backup_outside_mount": True,
    }


def test_fixture_rehearsal_recovers_first_rename_failure(tmp_path: Path) -> None:
    receipt = hermes_cutover._rehearse(
        tmp_path / "injected",
        inject_after_env=True,
    )

    assert receipt["status"] == "PASS"
    assert receipt["injected_failure_recovered"] is True
    data_dir = tmp_path / "injected" / "mounted-hermes"
    assert (data_dir / ".env").read_text() == ("OTHER=value\nNVIDIA_API_KEY=legacy-placeholder\n")
    assert "https://example.invalid/v1" in (data_dir / "config.yaml").read_text()
    assert not tuple(data_dir.glob(".nblb-*.candidate"))


def test_candidate_write_failure_removes_partial_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidate"
    calls = 0
    real_fsync = os.fsync

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = "injected fsync failure"
            raise OSError(message)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        hermes_cutover._write_file(
            path,
            b"partial",
            mode=0o600,
            uid=os.geteuid(),
            gid=os.getegid(),
        )

    assert not path.exists()


def test_backup_retirement_requires_provider_revocation_for_upstream_copy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retire"
    hermes_cutover._rehearse(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    upstream = "nvapi-" + ("x" * 40)
    _set_manifest_status(
        backup,
        "applied",
        env_payload=f"NVIDIA_API_KEY={upstream}\n".encode(),
    )
    settings = _settings(root)

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="provider_revocation_confirmation_required",
    ):
        hermes_cutover._retire_backup(
            settings,
            "fixture-attempt",
            provider_credential_revoked=False,
        )

    receipt = hermes_cutover._retire_backup(
        settings,
        "fixture-attempt",
        provider_credential_revoked=True,
    )

    assert receipt["backup_absent"] is True
    assert not backup.exists()


def test_backup_retirement_resumes_from_partial_tombstone(tmp_path: Path) -> None:
    root = tmp_path / "retire-resume"
    hermes_cutover._rehearse(root)
    backup_root = root / "hermes-cutover-backups"
    backup = backup_root / "fixture-attempt"
    _set_manifest_status(backup, "rolled_back")
    manifest = hermes_cutover._load_manifest(backup)
    manifest["retirement_requires_provider_confirmation"] = False
    hermes_cutover._atomic_json(backup / "manifest.json", manifest)
    tombstone = backup_root / ".retiring-fixture-attempt"
    backup.rename(tombstone)
    (tombstone / "env.before").unlink()

    receipt = hermes_cutover._retire_backup(
        _settings(root),
        "fixture-attempt",
        provider_credential_revoked=False,
    )

    assert receipt["backup_absent"] is True
    assert not tombstone.exists()


def test_existing_retirement_tombstone_is_fsynced_before_first_unlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "retire-fsync-before-unlink"
    hermes_cutover._rehearse(root)
    backup_root = root / "hermes-cutover-backups"
    backup = backup_root / "fixture-attempt"
    _set_manifest_status(backup, "rolled_back")
    manifest = hermes_cutover._load_manifest(backup)
    manifest["retirement_requires_provider_confirmation"] = False
    hermes_cutover._atomic_json(backup / "manifest.json", manifest)
    tombstone = backup_root / ".retiring-fixture-attempt"
    backup.rename(tombstone)
    parent_fsynced = False
    real_fsync_directory = hermes_cutover._fsync_directory
    real_retirement_tombstone = hermes_cutover._retirement_tombstone

    def track_fsync(path: Path) -> None:
        nonlocal parent_fsynced
        real_fsync_directory(path)
        if path == backup_root and not parent_fsynced:
            assert not backup.exists()
            assert tombstone.is_dir()
            parent_fsynced = True

    def open_tombstone(
        settings: hermes_cutover.Settings,
        backup_id: str,
    ) -> tuple[Path, dict[str, object] | None]:
        assert parent_fsynced
        return real_retirement_tombstone(settings, backup_id)

    monkeypatch.setattr(hermes_cutover, "_fsync_directory", track_fsync)
    monkeypatch.setattr(hermes_cutover, "_retirement_tombstone", open_tombstone)

    receipt = hermes_cutover._retire_backup(
        _settings(root),
        "fixture-attempt",
        provider_credential_revoked=False,
    )

    assert receipt["backup_absent"] is True
    assert parent_fsynced is True


def test_backup_retirement_recovers_terminal_output_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "retire-output-gap"
    hermes_cutover._rehearse(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    _set_manifest_status(backup, "rolled_back")
    settings = _settings(root)
    first = hermes_cutover._retire_backup(
        settings,
        "fixture-attempt",
        provider_credential_revoked=False,
    )
    assert first["retirement_receipt"] == "complete"
    receipt = hermes_cutover._load_retirement_receipt(settings, "fixture-attempt")
    assert receipt is not None
    hermes_cutover._write_retirement_receipt(
        settings,
        "fixture-attempt",
        status_value="pending",
        requires_provider_confirmation=False,
        provider_revocation_confirmed=True,
    )
    fsynced: list[Path] = []
    real_fsync_directory = hermes_cutover._fsync_directory

    def track_fsync(path: Path) -> None:
        fsynced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(hermes_cutover, "_fsync_directory", track_fsync)

    recovered = hermes_cutover._retire_backup(
        settings,
        "fixture-attempt",
        provider_credential_revoked=False,
    )

    assert recovered["retirement_receipt"] == "complete"
    assert recovered["backup_absent"] is True
    backup_root = root / "hermes-cutover-backups"
    receipt_root = root / "host-only-state" / "retirements"
    assert fsynced.index(backup_root) < fsynced.index(receipt_root)


def test_backup_hash_tamper_fails_before_any_retirement(tmp_path: Path) -> None:
    root = tmp_path / "tampered"
    hermes_cutover._rehearse(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    _set_manifest_status(backup, "rolled_back")
    (backup / "env.before").write_bytes(b"NVIDIA_API_KEY=tampered\n")

    with pytest.raises(hermes_cutover.CutoverError, match="backup_env_hash_mismatch"):
        hermes_cutover._retire_backup(
            _settings(root),
            "fixture-attempt",
            provider_credential_revoked=True,
        )

    assert {path.name for path in backup.iterdir()} == {
        "env.before",
        "config.before",
        "manifest.json",
    }


def test_backup_unexpected_entry_fails_before_any_retirement(tmp_path: Path) -> None:
    root = tmp_path / "unexpected-entry"
    hermes_cutover._rehearse(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    _set_manifest_status(backup, "rolled_back")
    fifo = backup / "unexpected.fifo"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(hermes_cutover.CutoverError, match="backup_entries_invalid"):
        hermes_cutover._retire_backup(
            _settings(root),
            "fixture-attempt",
            provider_credential_revoked=True,
        )

    assert fifo.exists()
    assert (backup / "env.before").is_file()
    assert (backup / "config.before").is_file()
    assert (backup / "manifest.json").is_file()


def test_state_root_symlink_into_hermes_mount_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "symlink-root"
    data = root / "mounted-hermes"
    data.mkdir(parents=True)
    (root / "host-only-state").symlink_to(data, target_is_directory=True)

    with pytest.raises(hermes_cutover.CutoverError, match="private_directory_invalid"):
        hermes_cutover._prepare_state_root(_settings(root))


def test_bind_backed_state_root_inside_hermes_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "mounted-hermes"
    state = tmp_path / "host-only-state"
    backup = tmp_path / "hermes-cutover-backups"
    data.mkdir()
    state.mkdir()
    backup.mkdir()

    def identity(path: Path) -> tuple[str, Path]:
        if path == data:
            return "8:1", Path("/physical/hermes")
        return "8:1", Path("/physical/hermes/bind-backed-state")

    monkeypatch.setattr(hermes_cutover, "_mount_identity", identity)

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="host_only_root_backed_by_hermes_mount",
    ):
        hermes_cutover._require_host_only_roots(_settings(tmp_path), state, backup)


def test_mount_source_below_host_only_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "mounted-hermes"
    state = tmp_path / "host-only-state"
    backup = tmp_path / "hermes-cutover-backups"
    data.mkdir()
    state.mkdir()
    backup.mkdir()
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        hermes_cutover.Settings,
        "production",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_mount_identity",
        lambda path: ("8:1", Path("/physical") / path.name),
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_container_mount_sources",
        lambda _settings_value: (backup / "specific-generation",),
    )

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="host_only_root_mounted_into_hermes",
    ):
        hermes_cutover._require_host_only_roots(settings, state, backup)


def test_nested_backup_mount_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setattr(
        hermes_cutover,
        "_mountinfo_entries",
        lambda: (("8:1", Path("/"), backup / "mounted-child"),),
    )

    with pytest.raises(hermes_cutover.CutoverError, match="backup_nested_mount_forbidden"):
        hermes_cutover._reject_nested_mounts(backup)


def test_header_validation_rejects_newline_without_echoing_value() -> None:
    secret = "nblb_admin_" + ("a" * 64) + "\nInjected: value"

    with pytest.raises(hermes_cutover.CutoverError) as raised:
        hermes_cutover._safe_header_value(secret, "admin")

    assert secret not in str(raised.value)
    assert str(raised.value) == "admin_header_invalid"


def test_bearer_token_binding_requires_exact_id_counter_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=1))
    counts = iter((7, 8))

    def token_item(_settings_value: object, observed_id: str) -> dict[str, object]:
        assert observed_id == token_id
        return {
            "id": token_id,
            "label": "expected",
            "scopes": ["models:read", "chat:write"],
            "revoked_at": None,
            "request_count": next(counts),
        }

    monkeypatch.setattr(hermes_cutover, "_token_item", token_item)
    monkeypatch.setattr(
        hermes_cutover,
        "_request",
        lambda *_args, **_kwargs: ("application/json", b'{"data":[{"id":"model"}]}'),
    )

    hermes_cutover._bind_bearer_to_token_id(
        _settings(tmp_path),
        token_id,
        _token(),
        expected_label="expected",
    )


def test_bearer_token_binding_rejects_mismatched_id_counter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=1))

    monkeypatch.setattr(
        hermes_cutover,
        "_token_item",
        lambda *_args: {
            "id": token_id,
            "label": "expected",
            "scopes": ["models:read", "chat:write"],
            "revoked_at": None,
            "request_count": 7,
        },
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_request",
        lambda *_args, **_kwargs: ("application/json", b'{"data":[{"id":"model"}]}'),
    )

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="downstream_bearer_token_id_mismatch",
    ):
        hermes_cutover._bind_bearer_to_token_id(
            _settings(tmp_path),
            token_id,
            _token(),
            expected_label="expected",
        )


def test_revoke_response_loss_reconciles_from_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=2))
    states = iter((None, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    monkeypatch.setattr(
        hermes_cutover,
        "_token_item",
        lambda *_args: {"revoked_at": next(states)},
    )

    def lost_response(*_args: object, **_kwargs: object) -> dict[str, object]:
        message = "response_lost"
        raise hermes_cutover.CutoverError(message)

    monkeypatch.setattr(hermes_cutover, "_admin", lost_response)

    hermes_cutover._revoke_and_verify(_settings(tmp_path), token_id)


def test_cutover_does_not_claim_candidate_ownership_before_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "binding-intent"
    data = root / "mounted-hermes"
    data.mkdir(parents=True)
    env = data / ".env"
    config = data / "config.yaml"
    env.write_text("NVIDIA_API_KEY=nvapi-" + ("x" * 40) + "\n")
    config.write_text(
        "model:\n  default: old\n  provider: nvidia\n  base_url: https://example.invalid/v1\n"
    )
    env.chmod(0o600)
    config.chmod(0o640)
    settings = _settings(root)
    token_id = str(UUID(int=3))
    phases: list[str] = []

    def update(
        _settings_value: hermes_cutover.Settings,
        journal: dict[str, object],
        phase: str,
    ) -> None:
        journal["phase"] = phase
        phases.append(phase)

    def bind(
        _settings_value: hermes_cutover.Settings,
        _token_id: str,
        _bearer: str,
        **_kwargs: object,
    ) -> None:
        assert journal["candidate_owned"] is False
        assert "candidate_provenance" not in journal
        assert phases[-1] == "candidate_binding_pending"
        message = "injected_binding_stop"
        raise hermes_cutover.CutoverError(message)

    journal: dict[str, object] = {"candidate_owned": False}
    monkeypatch.setattr(hermes_cutover, "_update_journal", update)
    monkeypatch.setattr(hermes_cutover, "_other_container_ids", lambda _settings_value: {})
    monkeypatch.setattr(
        hermes_cutover,
        "_create_backup",
        lambda *_args, **_kwargs: root / "hermes-cutover-backups" / "attempt",
    )
    monkeypatch.setattr(hermes_cutover, "_close_intake_locked", lambda *_args: None)
    monkeypatch.setattr(
        hermes_cutover,
        "_validate_token_item",
        lambda *_args, **_kwargs: {"request_count": 4},
    )
    monkeypatch.setattr(hermes_cutover, "_bind_bearer_to_token_id", bind)

    with pytest.raises(hermes_cutover.CutoverError, match="injected_binding_stop"):
        hermes_cutover._perform_cutover_locked(
            settings,
            _token(),
            token_id,
            None,
            journal,
            attempt_id="attempt",
            terminal_phase="applied",
            expected_label=None,
        )

    assert journal["candidate_owned"] is False
    assert "candidate_provenance" not in journal


def test_rehearsal_refuses_nonempty_fixture_root(tmp_path: Path) -> None:
    root = tmp_path / "occupied"
    root.mkdir()
    (root / "owned").touch()

    with pytest.raises(hermes_cutover.CutoverError, match="fixture_root_must_be_empty"):
        hermes_cutover._rehearse(root)


def test_recovery_repairs_only_a_recorded_mixed_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "mixed-recovery"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    record = hermes_cutover._validate_backup(settings, backup)
    token_id = str(UUID(int=1))
    target_env = hermes_cutover._replace_env(
        (settings.data_dir / ".env").read_bytes(),
        _token(),
    )
    (settings.data_dir / ".env").write_bytes(target_env)
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "attempt_id": "fixture-recovery",
        "backup_id": backup.name,
        "candidate_token_id": token_id,
        "previous_token_id": None,
        "candidate_owned": True,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "phase": "cutover_env_replaced",
    }
    revoked: list[str] = []
    monkeypatch.setattr(hermes_cutover, "_container_running", lambda _settings_value: False)
    monkeypatch.setattr(
        hermes_cutover,
        "_verify_generation",
        lambda *_args, **_kwargs: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings_value, observed_id: revoked.append(observed_id),
    )

    receipt = hermes_cutover._recover_locked(settings, journal)

    assert receipt["phase"] == "rolled_back"
    assert receipt["next_action"] == "issue_new_candidate"
    assert receipt["backup_id"] == record.path.name
    assert receipt["candidate_token_id"] == token_id
    assert hermes_cutover._pair_state(settings.data_dir) == record.source
    assert revoked == [token_id]
    assert journal["phase"] == "rolled_back"


def test_recovery_does_not_overwrite_unknown_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "unknown-drift"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    record = hermes_cutover._validate_backup(settings, backup)
    drift = b"OTHER=user-change\nNVIDIA_API_KEY=user-owned\n"
    (settings.data_dir / ".env").write_bytes(drift)
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "attempt_id": "fixture-drift",
        "backup_id": backup.name,
        "candidate_token_id": str(UUID(int=1)),
        "previous_token_id": None,
        "candidate_owned": True,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "phase": "cutover_intake_closed",
    }

    def close_intake(
        settings_value: hermes_cutover.Settings,
        journal_value: dict[str, object],
        phase: str,
    ) -> None:
        hermes_cutover._update_journal(settings_value, journal_value, phase)

    monkeypatch.setattr(hermes_cutover, "_close_intake_locked", close_intake)

    with pytest.raises(hermes_cutover.CutoverError, match="unknown_pair_recovery_required"):
        hermes_cutover._recover_locked(settings, journal)

    assert (settings.data_dir / ".env").read_bytes() == drift
    assert journal["phase"] == "recovery_required"


def test_recovery_discards_recorded_partial_backup_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "partial-backup"
    data = root / "mounted-hermes"
    state = root / "host-only-state"
    backup_root = root / "hermes-cutover-backups"
    data.mkdir(parents=True)
    state.mkdir(mode=0o700)
    backup_root.mkdir(mode=0o700)
    partial = backup_root / "attempt"
    partial.mkdir(mode=0o700)
    partial_file = partial / "env.before"
    partial_file.write_bytes(b"partial")
    partial_file.chmod(0o600)
    abandoned = partial / f".manifest.json.{'a' * 32}.tmp"
    abandoned.write_bytes(b"partial manifest")
    abandoned.chmod(0o600)
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "phase": "backup_creating",
        "backup_id": "attempt",
        "candidate_owned": False,
    }
    monkeypatch.setattr(hermes_cutover, "_ensure_hermes_started", lambda _settings: None)
    monkeypatch.setattr(
        hermes_cutover,
        "_update_journal",
        lambda _settings, document, phase: document.update(phase=phase),
    )

    receipt = hermes_cutover._recover_locked(_settings(root), journal)

    assert receipt["phase"] == "aborted"
    assert not partial.exists()


def test_backup_validation_removes_only_valid_abandoned_manifest_temp(tmp_path: Path) -> None:
    root = tmp_path / "manifest-temp"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    abandoned = backup / f".manifest.json.{'b' * 32}.tmp"
    abandoned.write_bytes(b"interrupted atomic replacement")
    abandoned.chmod(0o600)

    record = hermes_cutover._validate_backup(settings, backup)

    assert record.path == backup
    assert not abandoned.exists()


def test_manual_binding_pending_never_authorizes_candidate_revocation() -> None:
    journal: dict[str, object] = {
        "operation": "cutover",
        "candidate_owned": True,
        "phase": "candidate_binding_pending",
    }

    assert hermes_cutover._candidate_revocable(journal) is False
    journal["phase"] = "candidate_bound"
    assert hermes_cutover._candidate_revocable(journal) is True


def test_manual_binding_response_loss_requires_owner_revoke_before_new_issuance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "manual-candidate-reconciliation"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    record = hermes_cutover._validate_backup(settings, backup)
    token_id = str(UUID(int=61))
    revoked = False
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "attempt_id": "fixture-manual-reconciliation",
        "backup_id": backup.name,
        "candidate_token_id": token_id,
        "previous_token_id": None,
        "candidate_owned": False,
        "candidate_revoked_confirmed": False,
        "manual_candidate_requires_review": True,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "phase": "candidate_binding_pending",
    }
    monkeypatch.setattr(
        hermes_cutover,
        "_verify_generation",
        lambda *_args, **_kwargs: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_token_item",
        lambda *_args: {
            "id": token_id,
            "revoked_at": "2026-01-01T00:00:00Z" if revoked else None,
        },
    )

    first = hermes_cutover._recover_locked(settings, journal)
    repeated = hermes_cutover._recover_locked(settings, journal)

    assert first["status"] == "ACTION_REQUIRED"
    assert first["phase"] == "candidate_reconciliation_required"
    assert first["next_action"] == "review_and_revoke_candidate_token"
    assert first["candidate_token_id"] == token_id
    assert first["candidate_revoked_confirmed"] is False
    assert first["manual_candidate_requires_review"] is True
    assert repeated == {**first, "changed": False}
    assert journal["phase"] == "candidate_reconciliation_required"

    revoked = True
    recovered = hermes_cutover._recover_locked(settings, journal)

    assert recovered["status"] == "PASS"
    assert recovered["phase"] == "rolled_back"
    assert recovered["next_action"] == "issue_new_candidate"
    assert recovered["candidate_revoked_confirmed"] is True
    assert recovered["manual_candidate_requires_review"] is False
    assert journal["phase"] == "rolled_back"


def test_preflight_blocks_issuance_for_unresolved_manual_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "preflight-issuance-gate"
    hermes_cutover._rehearse(root)
    recovery = {
        "status": "ACTION_REQUIRED",
        "operation": "recover",
        "phase": "candidate_reconciliation_required",
        "changed": False,
        "next_action": "review_and_revoke_candidate_token",
        "candidate_token_id": str(UUID(int=63)),
        "candidate_revoked_confirmed": False,
        "manual_candidate_requires_review": True,
        "reconciliation_terminal_phase": "aborted",
    }
    monkeypatch.setattr(
        hermes_cutover,
        "_journal_document",
        lambda _settings_value: {"phase": "candidate_reconciliation_required"},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_recover_locked",
        lambda *_args: recovery,
    )

    receipt = hermes_cutover._preflight(_settings(root))

    assert receipt["status"] == "ACTION_REQUIRED"
    assert receipt["operation"] == "preflight"
    assert receipt["next_action"] == "review_and_revoke_candidate_token"
    assert "issuance_allowed" not in receipt


def test_recorded_helper_candidate_must_remain_observable_during_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "recorded-helper-candidate-missing"
    hermes_cutover._rehearse(root)
    candidate_id = str(UUID(int=64))
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cycle",
        "attempt_id": "recorded-helper-candidate-missing",
        "issuance_label": "hermes-cutover:recorded-helper-candidate-missing",
        "candidate_token_id": candidate_id,
        "candidate_provenance": "helper_issued",
        "candidate_owned": True,
        "candidate_revoked_confirmed": False,
        "phase": "issued_unreferenced",
    }
    monkeypatch.setattr(hermes_cutover, "_tokens_with_label", lambda *_args: [])

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="issuance_reconciliation_recorded_candidate_missing",
    ):
        hermes_cutover._recover_locked(_settings(root), journal)

    assert journal["phase"] == "issued_unreferenced"
    assert journal["candidate_revoked_confirmed"] is False


def test_recovery_required_reenters_helper_issuance_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "issuance-recovery-required"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    candidate_id = str(UUID(int=65))
    label = "hermes-cutover:issuance-recovery-required"
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cycle",
        "attempt_id": "issuance-recovery-required",
        "issuance_label": label,
        "candidate_token_id": None,
        "candidate_owned": False,
        "commit_decided": False,
        "failed_phase": "issuing",
        "phase": "recovery_required",
    }
    revoked: list[str] = []
    monkeypatch.setattr(
        hermes_cutover,
        "_tokens_with_label",
        lambda *_args: [{"id": candidate_id, "label": label, "revoked_at": None}],
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings_value, token_id: revoked.append(token_id),
    )

    receipt = hermes_cutover._recover_locked(settings, journal)

    assert receipt["status"] == "PASS"
    assert receipt["phase"] == "aborted"
    assert receipt["candidate_token_id"] == candidate_id
    assert receipt["candidate_revoked_confirmed"] is True
    assert receipt["manual_candidate_requires_review"] is False
    assert revoked == [candidate_id]
    assert journal["candidate_provenance"] == "helper_issued"
    assert journal["phase"] == "aborted"


@pytest.mark.parametrize("operation", ["preflight", "cycle", "rollback"])
def test_entrypoints_reconcile_terminal_unconfirmed_manual_candidate_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / f"terminal-manual-{operation}"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    candidate_id = str(UUID(int=66))
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "attempt_id": f"terminal-manual-{operation}",
        "candidate_token_id": candidate_id,
        "candidate_owned": False,
        "candidate_revoked_confirmed": False,
        "manual_candidate_requires_review": True,
        "phase": "rolled_back",
    }
    issued: list[str] = []
    monkeypatch.setattr(hermes_cutover, "_journal_document", lambda _settings_value: journal)
    monkeypatch.setattr(
        hermes_cutover,
        "_token_item",
        lambda *_args: {"id": candidate_id, "revoked_at": None},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_issue_for_cycle",
        lambda *_args: issued.append("issued") or (candidate_id, _token()),
    )

    if operation == "preflight":
        receipt = hermes_cutover._preflight(settings)
    elif operation == "cycle":
        receipt = hermes_cutover._cycle(settings)
    else:
        receipt = hermes_cutover._rollback(settings, "unused-backup", candidate_id)

    assert receipt["status"] == "ACTION_REQUIRED"
    assert receipt["operation"] == operation
    assert receipt["candidate_token_id"] == candidate_id
    assert receipt["candidate_revoked_confirmed"] is False
    assert issued == []


def test_operator_parser_has_no_plaintext_manual_cutover_command() -> None:
    with pytest.raises(SystemExit) as raised:
        hermes_cutover._parser().parse_args(["cutover", "--token-id", str(UUID(int=64))])

    assert raised.value.code == 2


def test_production_preflight_requires_inactive_locked_delayed_updater(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = hermes_cutover.Settings.production()
    monkeypatch.setattr(
        hermes_cutover,
        "_run",
        lambda *_args, **_kwargs: (
            "ActiveState=inactive\n"
            "ExecStart={ path=/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update ; }\n"
        ),
    )

    assert hermes_cutover._require_delayed_update_guard(settings) is True

    monkeypatch.setattr(
        hermes_cutover,
        "_run",
        lambda *_args, **_kwargs: (
            "ActiveState=active\n"
            "ExecStart={ path=/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update ; }\n"
        ),
    )
    with pytest.raises(
        hermes_cutover.CutoverError,
        match="delayed_update_service_not_inactive",
    ):
        hermes_cutover._require_delayed_update_guard(settings)


def test_fresh_recovery_before_commit_restores_source_and_revokes_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "recover-before-commit"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    record = hermes_cutover._validate_backup(
        settings,
        root / "hermes-cutover-backups" / "fixture-attempt",
    )
    candidate_id = str(UUID(int=30))
    previous_id = str(UUID(int=31))
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cycle",
        "attempt_id": "recover-before-commit",
        "backup_id": record.path.name,
        "candidate_token_id": candidate_id,
        "previous_token_id": previous_id,
        "candidate_owned": True,
        "commit_decided": False,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "phase": "cycle_cutover_applied",
    }
    hermes_cutover._atomic_json(settings.state_root / "journal.json", journal)
    revoked: list[str] = []
    monkeypatch.setattr(
        hermes_cutover,
        "_verify_generation",
        lambda *_args, **_kwargs: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings, token_id: revoked.append(token_id),
    )

    receipt = hermes_cutover._recover(settings)

    assert receipt["phase"] == "rolled_back"
    assert revoked == [candidate_id]
    assert hermes_cutover._pair_state(settings.data_dir) == record.source


def test_fresh_recovery_after_commit_keeps_target_and_revokes_previous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "recover-after-commit"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    record = hermes_cutover._validate_backup(
        settings,
        root / "hermes-cutover-backups" / "fixture-attempt",
    )
    candidate_id = str(UUID(int=32))
    previous_id = str(UUID(int=33))
    token = _token()
    target_env = hermes_cutover._replace_env(
        (settings.data_dir / ".env").read_bytes(),
        token,
    )
    target_config = hermes_cutover._replace_model_config(
        (settings.data_dir / "config.yaml").read_bytes()
    )
    hermes_cutover._atomic_write(
        settings.data_dir / ".env",
        target_env,
        mode=record.target.env.mode,
        uid=record.target.env.uid,
        gid=record.target.env.gid,
    )
    hermes_cutover._atomic_write(
        settings.data_dir / "config.yaml",
        target_config,
        mode=record.target.config.mode,
        uid=record.target.config.uid,
        gid=record.target.config.gid,
    )
    reapply_id = "fixture-reapply"
    reapply_path = hermes_cutover._create_backup(
        settings,
        reapply_id,
        record.target,
        record.source,
        active_token_id=candidate_id,
        candidate_token_id=candidate_id,
    )
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cycle",
        "attempt_id": "recover-after-commit",
        "backup_id": record.path.name,
        "reapply_backup_id": reapply_path.name,
        "candidate_token_id": candidate_id,
        "previous_token_id": previous_id,
        "candidate_owned": True,
        "commit_decided": True,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "phase": "cycle_previous_revoke_pending",
    }
    hermes_cutover._atomic_json(settings.state_root / "journal.json", journal)
    revoked: list[str] = []
    monkeypatch.setattr(
        hermes_cutover,
        "_verify_generation",
        lambda *_args, **_kwargs: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings, token_id: revoked.append(token_id),
    )

    receipt = hermes_cutover._recover(settings)

    assert receipt["phase"] == "reapplied"
    assert receipt["next_action"] == "retire_backups_after_revocations"
    assert receipt["backup_id"] == record.path.name
    assert receipt["reapply_backup_id"] == reapply_path.name
    assert receipt["candidate_token_id"] == candidate_id
    assert revoked == [previous_id]
    assert hermes_cutover._pair_state(settings.data_dir) == record.target


def test_recovery_removes_only_hash_bound_recorded_candidate_before_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate-recovery"
    hermes_cutover._rehearse(root)
    settings = _settings(root)
    backup = root / "hermes-cutover-backups" / "fixture-attempt"
    record = hermes_cutover._validate_backup(settings, backup)
    candidate = settings.data_dir / ".nblb-fixture.config.candidate"
    config_payload = hermes_cutover._replace_model_config(
        (settings.data_dir / "config.yaml").read_bytes()
    )
    candidate.write_bytes(config_payload)
    candidate.chmod(record.target.config.mode)
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cutover",
        "attempt_id": "fixture-candidate-recovery",
        "backup_id": backup.name,
        "candidate_token_id": str(UUID(int=1)),
        "previous_token_id": None,
        "candidate_owned": True,
        "source": asdict(record.source),
        "target": asdict(record.target),
        "candidate_files": {
            "env": {
                "name": ".nblb-fixture.env.candidate",
                "state": asdict(record.target.env),
            },
            "config": {"name": candidate.name, "state": asdict(record.target.config)},
        },
        "phase": "cutover_candidate_staging",
    }

    def verify(*_args: object, **_kwargs: object) -> dict[str, bool]:
        assert not candidate.exists()
        return {"health": True}

    monkeypatch.setattr(hermes_cutover, "_verify_generation", verify)
    monkeypatch.setattr(hermes_cutover, "_revoke_and_verify", lambda *_args: None)

    receipt = hermes_cutover._recover_locked(settings, journal)

    assert receipt["phase"] == "rolled_back"
    assert journal["candidate_files_cleaned"] is True
    assert not tuple(settings.data_dir.glob(".nblb-*.candidate"))


def test_unrecorded_candidate_blocks_terminal_recovery(tmp_path: Path) -> None:
    root = tmp_path / "unrecorded-candidate"
    data = root / "mounted-hermes"
    data.mkdir(parents=True)
    (data / ".nblb-unknown.env.candidate").write_text("untrusted")

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="unrecorded_candidate_file_present",
    ):
        hermes_cutover._recover_locked(
            _settings(root),
            {"schema_version": 1, "phase": "aborted"},
        )


@pytest.mark.parametrize("upstream_start", [False, True])
def test_cycle_holds_one_lock_from_issuance_through_revoke(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    upstream_start: bool,
) -> None:
    root = tmp_path / "cycle-lock"
    data = root / "mounted-hermes"
    data.mkdir(parents=True)
    credential = "nvapi-" + ("u" * 40) if upstream_start else _token()
    (data / ".env").write_text(f"NVIDIA_API_KEY={credential}\n")
    settings = _settings(root)
    previous_id = None if upstream_start else str(UUID(int=1))
    candidate_id = str(UUID(int=2))
    original = hermes_cutover.PairState(
        env=hermes_cutover.FileState("1" * 64, os.geteuid(), os.getegid(), 0o600),
        config=hermes_cutover.FileState("2" * 64, os.geteuid(), os.getegid(), 0o600),
    )
    candidate = hermes_cutover.PairState(
        env=hermes_cutover.FileState("3" * 64, os.geteuid(), os.getegid(), 0o600),
        config=hermes_cutover.FileState("4" * 64, os.geteuid(), os.getegid(), 0o600),
    )
    original_record = hermes_cutover.BackupRecord(
        path=root / "hermes-cutover-backups" / "original",
        manifest={},
        source=original,
        target=candidate,
        env_payload=b"original-env",
        config_payload=b"original-config",
        contains_upstream=False,
    )
    active = False
    acquisitions = 0
    phases: list[str] = []
    persisted: dict[str, object] | None = None
    revoked: list[str] = []

    @contextmanager
    def lock(_settings_value: hermes_cutover.Settings) -> Generator[None]:
        nonlocal active, acquisitions
        assert not active
        acquisitions += 1
        active = True
        try:
            yield
        finally:
            active = False

    def update_journal(
        _settings_value: hermes_cutover.Settings,
        document: dict[str, object],
        phase: str,
    ) -> None:
        nonlocal persisted
        assert active
        document["phase"] = phase
        phases.append(phase)
        persisted = dict(document)

    def perform_cutover(
        _settings_value: hermes_cutover.Settings,
        _token_value: str,
        _token_id: str,
        _previous_id: str | None,
        journal: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        assert active
        journal.update(
            {
                "backup_id": "original",
                "source": asdict(original),
                "target": asdict(candidate),
            }
        )
        return {"live": {"health": True}}

    def create_backup(
        _settings_value: hermes_cutover.Settings,
        attempt_id: str,
        *_args: object,
        **_kwargs: object,
    ) -> Path:
        assert active
        return root / "hermes-cutover-backups" / attempt_id

    def validate_backup(_settings_value: hermes_cutover.Settings, path: Path) -> object:
        assert active
        return replace(
            original_record,
            path=path,
            source=candidate,
            target=original,
        )

    monkeypatch.setattr(hermes_cutover, "_acquire_lock", lock)
    monkeypatch.setattr(
        hermes_cutover,
        "_journal_document",
        lambda _settings_value: None if persisted is None else dict(persisted),
    )
    monkeypatch.setattr(hermes_cutover, "_other_container_ids", lambda _settings_value: {})
    monkeypatch.setattr(
        hermes_cutover,
        "_discover_hermes_token_id",
        lambda _settings_value: str(previous_id),
    )
    monkeypatch.setattr(hermes_cutover, "_update_journal", update_journal)
    monkeypatch.setattr(
        hermes_cutover,
        "_issue_for_cycle",
        lambda _settings_value, _label: (candidate_id, _token()),
    )
    monkeypatch.setattr(hermes_cutover, "_perform_cutover_locked", perform_cutover)
    monkeypatch.setattr(
        hermes_cutover,
        "_journal_backup",
        lambda *_args: original_record,
    )
    monkeypatch.setattr(hermes_cutover, "_pair_state", lambda _data_dir: candidate)
    monkeypatch.setattr(hermes_cutover, "_create_backup", create_backup)
    monkeypatch.setattr(hermes_cutover, "_validate_backup", validate_backup)
    monkeypatch.setattr(
        hermes_cutover,
        "_swap_generation_locked",
        lambda *_args, **_kwargs: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_verify_one_key_exclusion",
        lambda _settings_value, _journal: {
            "alternate_succeeded": True,
            "excluded_key_restored": True,
        },
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_restart_and_verify",
        lambda *_args: {"health": True},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings_value, token_id: revoked.append(token_id),
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_token_item",
        lambda _settings_value, _token_id: {"revoked_at": "2026-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(hermes_cutover, "_set_manifest_status", lambda *_args: None)

    receipt = hermes_cutover._cycle(settings)

    assert receipt["status"] == "PASS"
    assert acquisitions == 1
    assert phases[0] == "issuing"
    assert phases[-1] == "reapplied"
    assert receipt["previous_generation"] == ("upstream" if upstream_start else "downstream")
    assert revoked == ([] if upstream_start else [previous_id])
    assert not active


def test_one_key_exclusion_journal_restores_disabled_key_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    excluded = str(UUID(int=40))
    alternate = str(UUID(int=41))
    enabled = False
    phases: list[str] = []

    def upstreams(_settings_value: hermes_cutover.Settings) -> list[dict[str, object]]:
        return [
            {"id": excluded, "enabled": enabled},
            {"id": alternate, "enabled": True},
        ]

    def admin_post(
        _settings_value: hermes_cutover.Settings,
        path: str,
    ) -> None:
        nonlocal enabled
        assert path == f"/upstream-keys/{excluded}/enable"
        enabled = True

    def counts(
        _settings_value: hermes_cutover.Settings,
    ) -> tuple[dict[str, int], list[str]]:
        eligible = [alternate]
        if enabled:
            eligible.insert(0, excluded)
        return {excluded: 0, alternate: 0}, eligible

    monkeypatch.setattr(hermes_cutover, "_upstreams_for_recovery", upstreams)
    monkeypatch.setattr(
        hermes_cutover,
        "_admin",
        lambda *_args, **_kwargs: {"probe_status": "valid"},
    )
    monkeypatch.setattr(hermes_cutover, "_admin_post", admin_post)
    monkeypatch.setattr(hermes_cutover, "_upstream_counts", counts)
    monkeypatch.setattr(
        hermes_cutover,
        "_update_journal",
        lambda _settings, document, phase: (
            document.update(phase=phase),
            phases.append(phase),
        ),
    )
    journal: dict[str, object] = {
        "schema_version": 1,
        "operation": "cycle",
        "phase": "cycle_one_key_disabled",
        "one_key_exclusion": {
            "excluded_key_id": excluded,
            "alternate_key_id": alternate,
            "original_eligible_ids": [excluded, alternate],
            "status": "disabled",
        },
    }

    hermes_cutover._restore_one_key_exclusion(_settings(tmp_path), journal)

    assert enabled is True
    assert journal["one_key_exclusion"] == {
        "excluded_key_id": excluded,
        "alternate_key_id": alternate,
        "original_eligible_ids": [excluded, alternate],
        "status": "restored",
    }
    assert phases == ["cycle_one_key_restored"]


def test_one_key_exclusion_restores_key_after_baseexception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    excluded = str(UUID(int=42))
    alternate = str(UUID(int=43))
    enabled = True
    counts = {excluded: 0, alternate: 0}

    def upstream_counts(
        _settings_value: hermes_cutover.Settings,
    ) -> tuple[dict[str, int], list[str]]:
        return counts, [excluded, alternate] if enabled else [alternate]

    def admin_post(
        _settings_value: hermes_cutover.Settings,
        path: str,
    ) -> None:
        nonlocal enabled
        if path.endswith("/disable"):
            enabled = False
        elif path.endswith("/enable"):
            enabled = True
        else:
            raise AssertionError

    monkeypatch.setattr(hermes_cutover, "_upstream_counts", upstream_counts)
    monkeypatch.setattr(hermes_cutover, "_admin_post", admin_post)
    monkeypatch.setattr(
        hermes_cutover,
        "_credential_from_env",
        lambda *_args: "api-server-key",
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_hermes_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_upstreams_for_recovery",
        lambda _settings_value: [{"id": excluded, "enabled": enabled}],
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_admin",
        lambda *_args, **_kwargs: {"probe_status": "valid"},
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_update_journal",
        lambda _settings, document, phase: document.update(phase=phase),
    )
    journal: dict[str, object] = {"operation": "cycle"}

    with pytest.raises(KeyboardInterrupt):
        hermes_cutover._verify_one_key_exclusion(_settings(tmp_path), journal)

    assert enabled is True
    exclusion = journal.get("one_key_exclusion")
    assert isinstance(exclusion, dict)
    assert exclusion["status"] == "restored"
    assert journal["phase"] == "cycle_one_key_restored"


def test_live_issue_ambiguity_reconciles_and_revokes_by_unique_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=9))
    revoked: list[str] = []

    def admin(
        _settings_value: object,
        method: str,
        _path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if method == "POST":
            message = "response_lost"
            raise hermes_cutover.CutoverError(message)
        return {"items": [{"id": token_id, "label": "task-label"}]}

    monkeypatch.setattr(hermes_cutover, "_admin", admin)
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings_value, observed_id: revoked.append(observed_id),
    )

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="downstream_issue_failed_reconciled_and_revoked",
    ):
        live_smoke._issue(
            _settings(tmp_path),
            "task-label",
            ["models:read", "chat:write"],
        )

    assert revoked == [token_id]


def test_live_issue_post_201_validation_failure_reconciles_and_revokes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=19))
    revoked: list[str] = []

    def admin(
        _settings_value: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        if method == "POST":
            return {"id": token_id, "token": _token()}
        assert path == "/downstream-tokens"
        return {"items": [{"id": token_id, "label": "post-201"}]}

    monkeypatch.setattr(hermes_cutover, "_admin", admin)
    monkeypatch.setattr(
        live_smoke,
        "_token_item",
        lambda *_args: (_ for _ in ()).throw(hermes_cutover.CutoverError("read_failed")),
    )
    monkeypatch.setattr(
        hermes_cutover,
        "_revoke_and_verify",
        lambda _settings_value, observed_id: revoked.append(observed_id),
    )

    with pytest.raises(
        hermes_cutover.CutoverError,
        match="downstream_issue_validation_failed_reconciled_and_revoked",
    ):
        live_smoke._issue(
            _settings(tmp_path),
            "post-201",
            ["models:read", "chat:write"],
        )

    assert revoked == [token_id]


def test_live_stream_requires_assistant_content_before_done() -> None:
    with pytest.raises(hermes_cutover.CutoverError, match="live_stream_content_failed"):
        live_smoke._stream_content(b"data: [DONE]\n\n")

    frames = [
        {"choices": [{"delta": {"content": "라이브 "}}]},
        {"choices": [{"delta": {"content": "스모크 성공"}}]},
    ]
    body = (
        b"".join(
            b"data: " + json.dumps(frame, ensure_ascii=True).encode() + b"\n\n" for frame in frames
        )
        + b"data: [DONE]\n\n"
    )
    assert live_smoke._stream_content(body) == "라이브 스모크 성공"


def test_live_nonstream_rejects_empty_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        hermes_cutover,
        "_request",
        lambda *_args, **_kwargs: ("application/json", b'{"choices":[{}]}'),
    )

    with pytest.raises(hermes_cutover.CutoverError, match="live_nonstream_content_failed"):
        live_smoke._chat(_settings(tmp_path), _token(), stream=False)


def test_restart_projection_detects_safe_state_drift() -> None:
    key_id = str(UUID(int=20))
    disabled_id = str(UUID(int=21))
    baseline: dict[str, object] = {
        "id": key_id,
        "fingerprint": "sha256:" + ("1" * 64),
        "enabled": True,
        "routing_state": "eligible",
        "health_state": "healthy",
        "cooldown_until": None,
        "request_count": 4,
        "success_count": 4,
        "failure_count": 0,
        "last_status_class": "success",
        "last_used_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    drifted: dict[str, object] = dict(baseline, health_state="degraded")
    disabled: dict[str, object] = dict(
        baseline,
        id=disabled_id,
        enabled=False,
        routing_state="disabled",
        request_count=0,
        success_count=0,
    )
    disabled_drifted: dict[str, object] = dict(
        disabled,
        cooldown_until="2026-01-01T01:00:00Z",
    )

    assert live_smoke._safe_upstream_projection([baseline], [key_id]) != (
        live_smoke._safe_upstream_projection([drifted], [key_id])
    )
    assert live_smoke._safe_upstream_projection(
        [baseline, disabled],
        [key_id, disabled_id],
    ) != live_smoke._safe_upstream_projection(
        [baseline, disabled_drifted],
        [key_id, disabled_id],
    )


def test_first_app_recreate_failure_runs_bounded_recovery_recreate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = str(UUID(int=50))
    second = str(UUID(int=51))
    expected_ref = "ghcr.io/example/app@sha256:" + ("a" * 64)
    compose_calls = 0
    health_calls = 0
    baseline = {
        "fingerprint": "sha256:" + ("1" * 64),
        "enabled": True,
        "routing_state": "eligible",
        "health_state": "healthy",
        "cooldown_until": None,
        "request_count": 1,
        "success_count": 1,
        "failure_count": 0,
        "last_status_class": "success",
        "last_used_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    def run(command: list[str], **_kwargs: object) -> str:
        nonlocal compose_calls
        if command[0] == "scripts/ops/production-compose.sh":
            compose_calls += 1
            if compose_calls == 1:
                message = "first_recreate_failed"
                raise hermes_cutover.CutoverError(message)
            return ""
        if command[:4] == ["docker", "inspect", "--format", "{{.Config.Image}}"]:
            return expected_ref
        return f"id-{command[-1]}"

    def health(_settings_value: hermes_cutover.Settings) -> None:
        nonlocal health_calls
        health_calls += 1

    monkeypatch.setattr(live_smoke, "_run", run)
    monkeypatch.setattr(live_smoke, "_wait_app_health", health)
    monkeypatch.setattr(
        live_smoke,
        "_upstreams",
        lambda _settings_value: [dict(baseline, id=first), dict(baseline, id=second)],
    )
    monkeypatch.setattr(live_smoke, "_scheduler_cursor", lambda: first)
    monkeypatch.setattr(live_smoke, "_container_logs", lambda *_args: b"")
    monkeypatch.setattr(live_smoke, "_secret_material_variants", tuple)

    with pytest.raises(hermes_cutover.CutoverError, match="first_recreate_failed"):
        live_smoke._restart_persistence(
            _settings(tmp_path),
            expected_ref,
            [first, second],
            [first, second],
            _token(),
            "2026-01-01T00:00:00Z",
        )

    assert compose_calls == 2
    assert health_calls == 1


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_live_matrix_signal_runs_task_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signum: signal.Signals,
) -> None:
    image_digest = "sha256:" + ("a" * 64)
    first = str(UUID(int=52))
    second = str(UUID(int=53))
    cleanup_calls = 0
    items = [
        {"id": first, "enabled": True, "routing_state": "eligible"},
        {"id": second, "enabled": True, "routing_state": "eligible"},
    ]

    def interrupt_issue(*_args: object, **_kwargs: object) -> live_smoke.IssuedToken:
        os.kill(os.getpid(), signum)
        raise AssertionError

    def cleanup(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return {
            "task_active_token_count": 0,
            "synthetic_upstream_row_count": 0,
            "upstream_row_count": 2,
            "upstream_state_restored": True,
        }

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["live_smoke.py", "--mode", "two-key", "--image-digest", image_digest],
    )
    monkeypatch.setattr(live_smoke, "_run", lambda *_args, **_kwargs: f"app@{image_digest}")
    monkeypatch.setattr(live_smoke, "_upstreams", lambda _settings_value: items)
    monkeypatch.setattr(live_smoke, "_issue", interrupt_issue)
    monkeypatch.setattr(live_smoke, "_cleanup_matrix", cleanup)

    assert live_smoke.main() == 1
    assert cleanup_calls == 1
    assert "live_smoke_interrupted" in capsys.readouterr().out


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_operator_main_converts_process_signal_to_recoverable_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    signum: signal.Signals,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["hermes_cutover.py", "cycle"])
    monkeypatch.setattr(
        hermes_cutover.Settings,
        "production",
        classmethod(lambda _cls: _settings(tmp_path)),
    )

    def interrupted_cycle(_settings_value: hermes_cutover.Settings) -> dict[str, object]:
        os.kill(os.getpid(), signum)
        raise AssertionError

    monkeypatch.setattr(hermes_cutover, "_cycle", interrupted_cycle)

    assert hermes_cutover.main() == 1
    assert "operator_command_interrupted" in capsys.readouterr().out


def test_live_round_robin_requires_exact_three_three_alternation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = str(UUID(int=10))
    second = str(UUID(int=11))
    count_snapshots = iter(({first: 4, second: 8}, {first: 7, second: 11}))
    calls = 0

    def chat(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(live_smoke, "_attempt_marker", lambda: "2026-01-01+00")
    monkeypatch.setattr(live_smoke, "_counts", lambda _settings_value: next(count_snapshots))
    monkeypatch.setattr(live_smoke, "_chat", chat)
    monkeypatch.setattr(
        live_smoke,
        "_db_sql",
        lambda *_args: f"{first}\n{second}\n{first}\n{second}\n{first}\n{second}",
    )

    receipt = live_smoke._round_robin_sequence(
        _settings(tmp_path),
        _token(),
        [first, second],
    )

    assert calls == 6
    assert receipt["request_count"] == 6
    assert receipt["per_key_counts"] == {first: 3, second: 3}
    assert receipt["alternating"] is True


def test_controlled_failure_cleanup_runs_after_post_create_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    synthetic_id = str(UUID(int=12))
    prior_id = str(UUID(int=13))
    cleanup_events: list[str] = []

    def db_sql(sql: str, *_variables: str) -> str:
        if "update upstream_keys" in sql:
            message = "injected_operation_failure"
            raise hermes_cutover.CutoverError(message)
        if "select coalesce(cursor_key_id" in sql:
            return prior_id
        if "update scheduler_state" in sql:
            cleanup_events.append("cursor_restore")
            return ""
        raise AssertionError(sql)

    def admin(
        _settings_value: object,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        assert method == "DELETE"
        assert path.endswith(synthetic_id)
        cleanup_events.append("delete")
        return {}

    monkeypatch.setattr(live_smoke, "_inject_key", lambda _settings_value: synthetic_id)
    monkeypatch.setattr(live_smoke, "_db_sql", db_sql)
    monkeypatch.setattr(
        live_smoke,
        "_disable",
        lambda _settings_value, _key_id: cleanup_events.append("disable"),
    )
    monkeypatch.setattr(hermes_cutover, "_admin", admin)
    monkeypatch.setattr(live_smoke, "_upstreams", lambda _settings_value: [])

    with pytest.raises(hermes_cutover.CutoverError, match="injected_operation_failure"):
        live_smoke._controlled_failure(_settings(tmp_path), _token(), [str(UUID(int=1))])

    assert cleanup_events == ["disable", "delete", "cursor_restore"]


def test_controlled_cooldown_excludes_synthetic_row_and_restores_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    synthetic_id = str(UUID(int=21))
    first = str(UUID(int=22))
    second = str(UUID(int=23))
    cooled = {
        "id": synthetic_id,
        "routing_state": "cooldown",
        "cooldown_until": "2026-01-01T00:10:00Z",
    }
    upstream_snapshots = iter(
        ([cooled, {"id": first}, {"id": second}], [cooled, {"id": first}, {"id": second}])
    )
    count_snapshots = iter(
        (
            {synthetic_id: 0, first: 4, second: 7},
            {synthetic_id: 0, first: 5, second: 7},
        )
    )
    cleanup: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(live_smoke, "_inject_key", lambda _settings: synthetic_id)
    monkeypatch.setattr(
        live_smoke,
        "_db_sql",
        lambda sql, *_variables: (
            str(UUID(int=24)) if "select coalesce(cursor_key_id" in sql else "1"
        ),
    )
    monkeypatch.setattr(live_smoke, "_upstreams", lambda _settings: next(upstream_snapshots))
    monkeypatch.setattr(live_smoke, "_counts", lambda _settings: next(count_snapshots))
    monkeypatch.setattr(live_smoke, "_chat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        live_smoke,
        "_cleanup_controlled_failure",
        lambda _settings, synthetic, cursor: cleanup.append((synthetic, cursor)),
    )

    receipt = live_smoke._controlled_cooldown(
        _settings(tmp_path),
        _token(),
        [first, second],
    )

    assert receipt["cooled_attempt_delta"] == 0
    assert receipt["alternate_key_id"] == first
    assert receipt["cooldown_preserved"] is True
    assert cleanup == [(synthetic_id, str(UUID(int=24)))]


def test_live_cleanup_observes_and_revokes_task_label_orphan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_id = str(UUID(int=25))
    first = str(UUID(int=26))
    second = str(UUID(int=27))
    revoked = False

    def downstream(_settings: object) -> list[dict[str, object]]:
        return [
            {
                "id": token_id,
                "label": "live-smoke-run:fixture:orphan",
                "revoked_at": "2026-01-01T00:00:00Z" if revoked else None,
            }
        ]

    def revoke(_settings: object, observed_id: str) -> None:
        nonlocal revoked
        assert observed_id == token_id
        revoked = True

    monkeypatch.setattr(live_smoke, "_downstream_items", downstream)
    monkeypatch.setattr(hermes_cutover, "_revoke_and_verify", revoke)
    monkeypatch.setattr(live_smoke, "_set_all_enabled", lambda *_args: None)
    monkeypatch.setattr(
        live_smoke,
        "_upstreams",
        lambda _settings: [
            {"id": first, "enabled": True},
            {"id": second, "enabled": True},
        ],
    )

    receipt = live_smoke._cleanup_matrix(
        _settings(tmp_path),
        [],
        [first, second],
        {first: True, second: True},
        None,
        "live-smoke-run:fixture:",
    )

    assert revoked is True
    assert receipt["task_active_token_count"] == 0


@pytest.mark.parametrize(
    "script",
    [
        "scripts/qa/smoke-live.sh",
        "scripts/qa/smoke-hermes.sh",
    ],
)
def test_live_smoke_shell_contract_is_executable_and_valid(script: str) -> None:
    path = Path(script)

    assert path.stat().st_mode & 0o111
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/bash", "-n", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_live_smoke_shell_gates_exact_cleanup_and_distribution() -> None:
    live = Path("scripts/qa/smoke-live.sh").read_text()
    hermes = Path("scripts/qa/smoke-hermes.sh").read_text()

    for fragment in (
        ".round_robin.request_count == 6",
        "all(.round_robin.per_key_counts[]; . == 3)",
        ".cleanup.task_active_token_count == 0",
        ".cleanup.synthetic_upstream_row_count == 0",
        ".restart_persistence.old_logs_scanned == true",
        ".restart_persistence.new_logs_scanned == true",
        ".restart_persistence.unrelated_containers_unchanged == true",
        ".restart_persistence.scheduler_cursor_persistent == true",
        ".controlled_cooldown.cooled_attempt_delta == 0",
        ".controlled_cooldown.cooldown_preserved == true",
        "lock_path=$state_root/cutover.lock",
        'exec 9<>"$lock_path"',
        "flock -x 9",
        ".gateway_healthy == true",
        '"$EVIDENCE_DIR/cleanup.json" >/dev/null || cleanup_status=1',
    ):
        assert fragment in live
    for fragment in (
        '[ "$candidate_scan_succeeded" = true ] || cleanup_status=1',
        '[ "$candidate_count" -eq 0 ] || cleanup_status=1',
        ".candidate_scan_succeeded == true",
        'if [ "$status" -eq 0 ] && [ "$journal_phase" = reapplied ]; then',
        ".candidate_file_count == 0",
        '.journal_phase == "reapplied"',
        '"$EVIDENCE_DIR/cleanup.json" >/dev/null || final_cleanup_status=1',
        'python3 "$helper" recover',
        ".journal_safe == true",
    ):
        assert fragment in hermes

    assert live.index("trap finish EXIT") < live.index("tmp_root=$(mktemp -d)")
    assert live.index("tmp_root=$(mktemp -d)") < live.index("hermes_running=$(docker inspect")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_hermes_cleanup_counts_candidate_names_of_every_file_type(
    tmp_path: Path,
    kind: str,
) -> None:
    source = Path("scripts/qa/smoke-hermes.sh").read_text()
    start = source.index("write_cleanup() {")
    end = source.index("\nfinish() {", start)
    function = source[start:end]
    data = tmp_path / "hermes"
    state = tmp_path / "state"
    evidence = tmp_path / "evidence"
    temporary = tmp_path / "temporary"
    data.mkdir()
    state.mkdir()
    evidence.mkdir()
    temporary.mkdir()
    journal = state / "journal.json"
    journal.write_text('{"phase":"reapplied"}\n')
    candidate = data / f".nblb-{kind}.env.candidate"
    if kind == "symlink":
        target = data / "target"
        target.write_text("fixture")
        candidate.symlink_to(target.name)
    elif kind == "fifo":
        os.mkfifo(candidate, mode=0o600)
    else:
        candidate.mkdir()
    function = function.replace(
        "/opt/agent-apps/data/hermes",
        shlex.quote(str(data)),
    ).replace(
        "Path('/opt/nvidia-build-lb/hermes-cutover-state/journal.json')",
        f"Path({str(journal)!r})",
    )
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"tmp_root={shlex.quote(str(temporary))}",
            f"EVIDENCE_DIR={shlex.quote(str(evidence))}",
            "cleanup_status=0",
            "recovery_attempted=false",
            "recovery_succeeded=false",
            function,
            "write_cleanup 0",
        )
    )

    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = hermes_cutover._json_body((evidence / "cleanup.json").read_bytes())
    assert receipt["status"] == "FAIL"
    assert receipt["candidate_scan_succeeded"] is True
    assert receipt["temporary_paths_absent"] is False
    assert receipt["candidate_file_count"] == 1


def test_hermes_cleanup_fails_when_candidate_root_cannot_be_scanned(
    tmp_path: Path,
) -> None:
    source = Path("scripts/qa/smoke-hermes.sh").read_text()
    start = source.index("write_cleanup() {")
    end = source.index("\nfinish() {", start)
    function = source[start:end]
    missing_data = tmp_path / "missing-hermes-data"
    state = tmp_path / "state"
    evidence = tmp_path / "evidence"
    temporary = tmp_path / "temporary"
    state.mkdir()
    evidence.mkdir()
    temporary.mkdir()
    journal = state / "journal.json"
    journal.write_text('{"phase":"reapplied"}\n')
    function = function.replace(
        "/opt/agent-apps/data/hermes",
        shlex.quote(str(missing_data)),
    ).replace(
        "Path('/opt/nvidia-build-lb/hermes-cutover-state/journal.json')",
        f"Path({str(journal)!r})",
    )
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"tmp_root={shlex.quote(str(temporary))}",
            f"EVIDENCE_DIR={shlex.quote(str(evidence))}",
            "cleanup_status=0",
            "recovery_attempted=false",
            "recovery_succeeded=false",
            function,
            "write_cleanup 0",
        )
    )

    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = hermes_cutover._json_body((evidence / "cleanup.json").read_bytes())
    assert receipt["status"] == "FAIL"
    assert receipt["candidate_scan_succeeded"] is False
    assert receipt["temporary_paths_absent"] is False
    assert receipt["candidate_file_count"] == 0


def test_live_database_sensor_uses_local_peer_without_password_environment() -> None:
    source = Path("scripts/qa/live_smoke.py").read_text()

    assert "PGPASSWORD" not in source
    assert '"--user",\n        "70",' in source
    assert '"--no-psqlrc"' in source


def test_live_smoke_make_contract_rejects_invalid_mode_without_live_mutation() -> None:
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/make",
            "smoke-live",
            "MODE=invalid",
            "IMAGE_DIGEST=sha256:" + ("0" * 64),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "INPUT[64]: MODE must be one-key or two-key" in completed.stdout
    assert "Error 64" in completed.stderr
