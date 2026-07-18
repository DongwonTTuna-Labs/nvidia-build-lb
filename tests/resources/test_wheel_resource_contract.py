import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from typing import ClassVar, Final, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from tests.ui.evidence_paths import claim_evidence_directory, evidence_directory

pytestmark = pytest.mark.ui_fake

_INSTALLED_RESOURCE_PROBE: Final = dedent(
    """
    import hashlib
    import json
    import sys
    from pathlib import Path

    install_root = Path(sys.argv[1]).resolve()
    repository_root = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(install_root))
    assert all(
        not entry or not Path(entry).resolve().is_relative_to(repository_root)
        for entry in sys.path
    ), "repository source leaked into the isolated probe"

    import nvidia_build_lb.web.resources as resource_module
    from nvidia_build_lb.web.resources import WebResource, load_web_resource

    module_path = Path(resource_module.__file__).resolve()
    assert module_path.is_relative_to(install_root), "resource module was not wheel-installed"
    web_root = module_path.parent
    expected_paths = {
        WebResource.ADMIN_DOCUMENT: "templates/admin.html",
        WebResource.ADMIN_STYLESHEET: "static/admin.css",
        WebResource.ADMIN_SCRIPT: "static/admin.js",
        WebResource.FAVICON: "static/favicon.svg",
        WebResource.SHOWCASE_DOCUMENT: "templates/showcase.html",
        WebResource.SHOWCASE_STYLESHEET: "static/showcase.css",
        WebResource.SHOWCASE_SCRIPT: "static/showcase.js",
    }
    assert set(expected_paths) == set(WebResource), "WebResource allowlist is not closed"
    browser_roots = (web_root / "static", web_root / "templates")
    packaged_paths = {
        path.relative_to(web_root).as_posix()
        for browser_root in browser_roots
        for path in browser_root.iterdir()
        if path.is_file() and path.name != "__init__.py"
    }
    forbidden_browser_files = sorted(packaged_paths - set(expected_paths.values()))
    assert not forbidden_browser_files, "wheel package data has adjacent browser files"
    assert packaged_paths == set(expected_paths.values()), "wheel package data is not exact"

    loaded = {}
    for resource, relative_path in expected_paths.items():
        content = load_web_resource(resource)
        assert content.strip(), f"empty resource: {resource.value}"
        expected_content = (web_root / relative_path).read_text(encoding="utf-8")
        assert content == expected_content, f"mapping mismatch: {resource.value}"
        loaded[resource.value] = content

    try:
        WebResource("not-allowlisted")
    except ValueError:
        pass
    else:
        raise AssertionError("WebResource accepted a fallback name")

    resources = []
    for relative_path in sorted(packaged_paths):
        content = (web_root / relative_path).read_bytes()
        resources.append({
            "path": relative_path,
            "byte_count": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    print(json.dumps({
        "probe_environment": "wheel-install-outside-repository-cwd",
        "source_tree_fallback": False,
        "resources": resources,
        "forbidden_browser_files": forbidden_browser_files,
    }, sort_keys=True))
    """
)


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class _PackagedResource(_StrictModel):
    path: str
    byte_count: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ResourceProvenance(_StrictModel):
    probe_environment: Literal["wheel-install-outside-repository-cwd"]
    source_tree_fallback: Literal[False]
    resources: tuple[
        _PackagedResource,
        _PackagedResource,
        _PackagedResource,
        _PackagedResource,
        _PackagedResource,
        _PackagedResource,
        _PackagedResource,
    ]
    forbidden_browser_files: tuple[str, ...]


def _source_manifest(repository_root: Path) -> tuple[_PackagedResource, ...]:
    web_root = repository_root / "src" / "nvidia_build_lb" / "web"
    relative_paths = (
        "static/admin.css",
        "static/admin.js",
        "static/favicon.svg",
        "static/showcase.css",
        "static/showcase.js",
        "templates/admin.html",
        "templates/showcase.html",
    )
    return tuple(
        _PackagedResource(
            path=relative_path,
            byte_count=(web_root / relative_path).stat().st_size,
            sha256=sha256((web_root / relative_path).read_bytes()).hexdigest(),
        )
        for relative_path in relative_paths
    )


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> str:
    completed = subprocess.run(  # noqa: S603 - executable and arguments are task-owned.
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_built_wheel_loads_only_closed_web_resources_outside_source_tree() -> None:
    # Given: a wheel built offline and installed without dependencies into a private target.
    assert "browser_suffixes" not in _INSTALLED_RESOURCE_PROBE
    assert '"forbidden_browser_files": []' not in _INSTALLED_RESOURCE_PROBE
    repository_root = Path(__file__).resolve().parents[2]
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    environment = os.environ.copy()
    environment["UV_COLOR"] = "never"
    environment["UV_NO_PROGRESS"] = "1"
    with TemporaryDirectory(
        prefix="nblb-wheel-resource-",
        dir=repository_root.parent,
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        distribution_dir = temporary_root / "dist"
        install_root = temporary_root / "private-install"
        external_cwd = temporary_root / "external-cwd"
        external_cwd.mkdir()
        _ = _run_checked(
            [
                uv_executable,
                "build",
                "--wheel",
                "--offline",
                "--no-python-downloads",
                "--out-dir",
                str(distribution_dir),
                str(repository_root),
            ],
            cwd=repository_root,
            environment=environment,
        )
        wheels = tuple(distribution_dir.glob("*.whl"))
        assert len(wheels) == 1
        _ = _run_checked(
            [
                uv_executable,
                "pip",
                "install",
                "--target",
                str(install_root),
                "--no-deps",
                "--offline",
                "--no-python-downloads",
                str(wheels[0]),
            ],
            cwd=external_cwd,
            environment=environment,
        )

        # When: isolated Python probes the private install from a repository-external CWD.
        receipt_text = _run_checked(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _INSTALLED_RESOURCE_PROBE,
                str(install_root),
                str(repository_root),
            ],
            cwd=external_cwd,
            environment=environment,
        )

        # Then: all seven exact mappings have wheel-backed provenance and no adjacent asset.
        receipt = _ResourceProvenance.model_validate_json(receipt_text)
        assert receipt.resources == _source_manifest(repository_root)
        assert tuple(item.path for item in receipt.resources) == (
            "static/admin.css",
            "static/admin.js",
            "static/favicon.svg",
            "static/showcase.css",
            "static/showcase.js",
            "templates/admin.html",
            "templates/showcase.html",
        )
        assert receipt.forbidden_browser_files == ()
        evidence_root = claim_evidence_directory(evidence_directory())
        output = evidence_root / "resource-provenance.json"
        _ = output.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
        assert _ResourceProvenance.model_validate_json(output.read_text()) == receipt
