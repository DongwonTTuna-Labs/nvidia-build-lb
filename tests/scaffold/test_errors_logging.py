from io import StringIO
from pathlib import Path

import pytest

from nvidia_build_lb.config import DeploymentStage, LogLevel, SettingsSource, load_settings
from nvidia_build_lb.errors import ConfigurationError, ConfigurationErrorCode, safe_error
from nvidia_build_lb.logging import (
    LogEventName,
    LoggingConfig,
    RequestId,
    SafeLogEvent,
    StructuredLogLine,
    init_logging,
)


def test_configuration_error_has_a_safe_stable_serialization() -> None:
    # Given: a typed configuration failure.
    error = ConfigurationError(code=ConfigurationErrorCode.VAULT_KEY_INVALID)

    # When: it crosses the safe serialization boundary.
    serialized = safe_error(error)

    # Then: type, code, and public message are explicit and stable.
    assert serialized.error_type == "ConfigurationError"
    assert serialized.code == ConfigurationErrorCode.VAULT_KEY_INVALID
    assert serialized.message == "vault key must contain exactly 32 bytes"
    assert str(error) == ConfigurationErrorCode.VAULT_KEY_INVALID.value


def test_production_logging_excludes_secret_values_and_sensitive_paths(
    tmp_path: Path,
) -> None:
    # Given: a missing secret path and a production logger writing to memory.
    sensitive_path = tmp_path / "do-not-log-this-location"
    source = SettingsSource(vault_key_file=sensitive_path)
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)
    stream = StringIO()
    logger = init_logging(
        LoggingConfig(
            name="nvidia-build-lb.scaffold.production",
            stage=DeploymentStage.PRODUCTION,
            level=LogLevel.INFO,
            stream=stream,
        )
    )

    # When: the typed error is emitted through the production formatter.
    logger.emit(
        SafeLogEvent(
            name=LogEventName.CONFIGURATION_REJECTED,
            level=LogLevel.ERROR,
            request_id=RequestId("request-scaffold"),
            error=safe_error(captured.value),
        )
    )

    # Then: JSON preserves safe error metadata and excludes the source path.
    line = StructuredLogLine.model_validate_json(stream.getvalue())
    assert line.event is LogEventName.CONFIGURATION_REJECTED
    assert line.level is LogLevel.ERROR
    assert line.request_id == "request-scaffold"
    assert line.error_type == "ConfigurationError"
    assert line.error_code == ConfigurationErrorCode.VAULT_KEY_UNAVAILABLE.value
    assert str(sensitive_path) not in stream.getvalue()
    assert "authorization" not in stream.getvalue().lower()


def test_development_logging_is_human_readable() -> None:
    # Given: a development logger writing to memory.
    stream = StringIO()
    logger = init_logging(
        LoggingConfig(
            name="nvidia-build-lb.scaffold.development",
            stage=DeploymentStage.DEVELOPMENT,
            level=LogLevel.DEBUG,
            stream=stream,
        )
    )
    event = SafeLogEvent(
        name=LogEventName.SERVICE_STARTED,
        level=LogLevel.INFO,
        request_id=RequestId("request-dev"),
    )

    # When: a safe event is emitted through the development formatter.
    logger.emit(event)

    # Then: the line is compact human-readable text rather than JSON.
    assert stream.getvalue() == "INFO service.started request_id=request-dev\n"
