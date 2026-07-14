"""Raw curl and OpenAI SDK over the real API/router/adapter fake-upstream path."""

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest
from openai import OpenAI

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.logging import StructuredLogLine
from nvidia_build_lb.scheduler_state import TerminalOutcome
from tests.contracts._support import CHAT_TOKEN, MODELS_TOKEN

from .conftest import LiveApiServer, track_temp_credential_file
from .fakes import successful_terminals

pytestmark = pytest.mark.api
_ERROR_CASES = (
    ("provider-json-error-body-must-not-escape", "upstream_request_rejected"),
    ("provider-problem-json-body-must-not-escape", "upstream_request_rejected"),
    ("provider-text-error-body-must-not-escape", "upstream_rate_limited"),
)


class _CurlLaunchError(Exception):
    """Synthetic curl launch failure after the credential file exists."""


@dataclass(frozen=True, slots=True)
class _CurlRequest:
    method: str
    path: str
    token: str
    body: bytes | None = None
    expected_returncode: int = 0


def _curl(server: LiveApiServer, tmp_path: Path, request: _CurlRequest) -> bytes:
    executable = shutil.which("curl")
    assert executable is not None, "curl is required by the locked Todo 5 gate"
    header_path = tmp_path / (
        f"curl-header-{request.method.lower()}-{len(tuple(tmp_path.iterdir()))}"
    )
    track_temp_credential_file(header_path)
    descriptor: int | None = None
    created = False
    completed: subprocess.CompletedProcess[bytes]
    try:
        descriptor = os.open(
            header_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            _ = handle.write(
                f"Authorization: Bearer {request.token}\nContent-Type: application/json\n".encode()
            )
        command = [
            executable,
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--http1.1",
            "--request",
            request.method,
            "--header",
            f"@{header_path}",
        ]
        if request.body is not None:
            command.extend(("--data-binary", "@-"))
        command.append(f"{server.base_url}{request.path}")
        completed = subprocess.run(  # noqa: S603 - fixed curl path and fixed option grammar.
            command,
            input=request.body,
            capture_output=True,
            check=False,
            timeout=15,
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            header_path.unlink(missing_ok=True)
    assert completed.returncode == request.expected_returncode, completed.stderr.decode(
        errors="replace"
    )
    assert not header_path.exists()
    return completed.stdout


def _assert_safe_error_media_types(server: LiveApiServer, tmp_path: Path) -> None:
    for provider_marker, safe_code in _ERROR_CASES:
        error_body = (
            b'{"model":"z-ai/glm-5.2","messages":[{"role":"user",'
            + f'"content":"{provider_marker}"'.encode()
            + b"}]}"
        )
        safe_error = _curl(
            server,
            tmp_path,
            _CurlRequest(
                "POST",
                "/v1/chat/completions",
                CHAT_TOKEN,
                error_body,
                expected_returncode=22,
            ),
        )
        assert f'"code":"{safe_code}"'.encode() in safe_error
        assert provider_marker.encode() not in safe_error


def test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream(
    live_api_server: LiveApiServer,
    tmp_path: Path,
) -> None:
    models = _curl(
        live_api_server,
        tmp_path,
        _CurlRequest("GET", "/v1/models", MODELS_TOKEN),
    )
    assert models == (
        b'{"object":"list","data":[{"id":"z-ai/glm-5.2","object":"model","owned_by":"nvidia"}]}'
    )

    nonstream_body = (
        b'{"model":"z-ai/glm-5.2","messages":[{"role":"user",'
        b'"content":"curl nonstream"}],"curl_extension":{"preserve":true}}'
    )
    nonstream = _curl(
        live_api_server,
        tmp_path,
        _CurlRequest("POST", "/v1/chat/completions", CHAT_TOKEN, nonstream_body),
    )
    assert b'"id":"curl-json"' in nonstream
    assert b'"content":"fake upstream ok"' in nonstream

    stream_body = (
        b'{"model":"z-ai/glm-5.2","messages":[{"role":"user",'
        b'"content":"curl stream"}],"stream":true}'
    )
    curl_stream = _curl(
        live_api_server,
        tmp_path,
        _CurlRequest("POST", "/v1/chat/completions", CHAT_TOKEN, stream_body),
    )
    assert b'"content":"fake "' in curl_stream
    assert curl_stream.endswith(b"data: [DONE]\n\n")

    with OpenAI(
        api_key=MODELS_TOKEN,
        base_url=f"{live_api_server.base_url}/v1",
        timeout=10,
        max_retries=0,
    ) as models_client:
        sdk_models = models_client.models.list()
        assert [model.id for model in sdk_models.data] == ["z-ai/glm-5.2"]

    with OpenAI(
        api_key=CHAT_TOKEN,
        base_url=f"{live_api_server.base_url}/v1",
        timeout=10,
        max_retries=0,
    ) as chat_client:
        completion = chat_client.chat.completions.create(
            model="z-ai/glm-5.2",
            messages=[{"role": "user", "content": "sdk nonstream"}],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        assert completion.id == "sdk-json"
        assert completion.choices[0].message.content == "fake upstream ok"

        stream = chat_client.chat.completions.create(
            model="z-ai/glm-5.2",
            messages=[{"role": "user", "content": "sdk stream"}],
            stream=True,
        )
        pieces = [chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices]
        stream.close()
        assert "".join(pieces) == "fake upstream ok"

    midstream_body = (
        b'{"model":"z-ai/glm-5.2","messages":[{"role":"user",'
        b'"content":"midstream probe"}],"stream":true}'
    )
    midstream = _curl(
        live_api_server,
        tmp_path,
        _CurlRequest("POST", "/v1/chat/completions", CHAT_TOKEN, midstream_body),
    )
    assert b'"content":"partial"' in midstream
    assert b"event: error\n" in midstream
    assert b'"code":"upstream_stream_error"' in midstream
    assert b"data: [DONE]" not in midstream

    _assert_safe_error_media_types(live_api_server, tmp_path)

    harness = live_api_server.harness
    assert len(harness.upstream.observed) == 8
    assert all(request.path == "/v1/chat/completions" for request in harness.upstream.observed)
    assert harness.upstream.pending_count == 0
    assert len(harness.attempts.starts) == 8
    assert len(harness.attempts.terminals) == 8
    assert successful_terminals(harness, count=4)
    assert tuple(terminal.outcome for terminal in harness.attempts.terminals[4:]) == (
        TerminalOutcome.FAILED,
        TerminalOutcome.FAILED,
        TerminalOutcome.FAILED,
        TerminalOutcome.FAILED,
    )
    assert tuple(terminal.status_class for terminal in harness.attempts.terminals[4:]) == (
        LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
        LastStatusClass.REQUEST_REJECTED,
        LastStatusClass.REQUEST_REJECTED,
        LastStatusClass.RATE_LIMITED,
    )
    assert harness.fail_stop.calls == 0

    log_lines = harness.log_stream.getvalue().splitlines()
    assert len(log_lines) == 8
    events = [StructuredLogLine.model_validate_json(line) for line in log_lines]
    assert all(event.internal_key_id is not None for event in events)
    assert all(event.terminal_outcome is TerminalOutcome.SUCCEEDED for event in events[:4])
    assert tuple(event.safe_status_class for event in events[4:]) == (
        LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
        LastStatusClass.REQUEST_REJECTED,
        LastStatusClass.REQUEST_REJECTED,
        LastStatusClass.RATE_LIMITED,
    )
    log_corpus = harness.log_stream.getvalue().lower()
    for forbidden in (
        CHAT_TOKEN,
        MODELS_TOKEN,
        "curl nonstream",
        "sdk nonstream",
        "authorization",
        *(provider_marker for provider_marker, _safe_code in _ERROR_CASES),
    ):
        assert forbidden.lower() not in log_corpus
    assert not tuple(tmp_path.iterdir())


def test_curl_credential_file_is_atomic_mode_0600_and_cleans_on_failure(
    live_api_server: LiveApiServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_run(command: list[str], **kwargs: object) -> Never:
        del kwargs
        header_value = command[command.index("--header") + 1]
        header_path = Path(header_value.removeprefix("@"))
        assert stat.S_IMODE(header_path.stat().st_mode) == 0o600
        raise _CurlLaunchError

    monkeypatch.setattr(subprocess, "run", reject_run)

    with pytest.raises(_CurlLaunchError):
        _ = _curl(
            live_api_server,
            tmp_path,
            _CurlRequest("GET", "/v1/models", MODELS_TOKEN),
        )

    assert not tuple(tmp_path.iterdir())
