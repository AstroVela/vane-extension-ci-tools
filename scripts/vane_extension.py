#!/usr/bin/env python3
"""Strict Vane integration helpers for out-of-tree DuckDB extensions."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

import tomllib

SCHEMA_VERSION = 1
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
SOURCE_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
FORK_VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-vane\.[0-9a-f]{10}")
VANE_REPOSITORY = "AstroVela/vane"
VANE_REPOSITORY_URL = f"https://github.com/{VANE_REPOSITORY}.git"
CI_TOOLS_REPOSITORY = "AstroVela/vane-extension-ci-tools"
CI_TOOLS_REPOSITORY_URL = f"https://github.com/{CI_TOOLS_REPOSITORY}.git"
CI_TOOLS_WORKFLOW_PATH = ".github/workflows/_vane_extension_ci.yml"
OIDC_AUDIENCE = "vane-extension-ci-tools"


class ConfigurationError(RuntimeError):
    """Raised when an integration input violates the strict contract."""


@dataclass(frozen=True)
class ExtensionManifest:
    schema_version: int
    name: str
    extension_config: str
    build_extensions: tuple[str, ...]
    native_test_selection: str
    vane_repository: str
    vane_revision: str

    @property
    def build_extensions_cmake(self) -> str:
        return ";".join(self.build_extensions)


@dataclass(frozen=True)
class VaneIdentity:
    source_id: str
    fork_version: str
    upstream_version: str


def fail(message: str) -> NoReturn:
    raise ConfigurationError(message)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}", file=sys.stderr)
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def resolve_within(root: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        fail(f"{label} must be relative to the extension root: {value}")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        fail(f"{label} escapes the extension root: {value}")
    return resolved


def require_string(table: dict[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        fail(f"{label} must be a non-empty single-line string")
    return value


def load_manifest(manifest_path: Path, extension_root: Path) -> ExtensionManifest:
    resolved_root = extension_root.resolve()
    resolved_manifest = manifest_path.resolve()
    try:
        resolved_manifest.relative_to(resolved_root)
    except ValueError:
        fail(f"manifest escapes the extension root: {manifest_path}")

    try:
        with resolved_manifest.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        fail(f"manifest does not exist: {resolved_manifest}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {resolved_manifest}: {exc}")

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        fail(f"schema_version must be {SCHEMA_VERSION}, got {schema_version!r}")

    name = require_string(raw, "name", "name")
    if not NAME_RE.fullmatch(name):
        fail(f"name is not canonical: {name}")

    extension_config = require_string(raw, "extension_config", "extension_config")
    extension_config_path = resolve_within(
        extension_root, extension_config, "extension_config"
    )
    if not extension_config_path.is_file():
        fail(f"extension_config does not exist: {extension_config_path}")

    build_extensions_raw = raw.get("build_extensions")
    if not isinstance(build_extensions_raw, list) or not build_extensions_raw:
        fail("build_extensions must be a non-empty array")
    build_extensions: list[str] = []
    for value in build_extensions_raw:
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            fail(f"build_extensions contains a non-canonical name: {value!r}")
        if value in build_extensions:
            fail(f"build_extensions contains a duplicate: {value}")
        build_extensions.append(value)

    native_test_selection = require_string(
        raw, "native_test_selection", "native_test_selection"
    )
    if native_test_selection.startswith("-"):
        fail("native_test_selection must be a test selector, not a command-line option")
    resolve_within(extension_root, native_test_selection, "native_test_selection")

    vane_raw = raw.get("vane")
    if not isinstance(vane_raw, dict):
        fail("vane must be a table")
    vane_repository = require_string(vane_raw, "repository", "vane.repository")
    if vane_repository != VANE_REPOSITORY:
        fail(f"vane.repository must be {VANE_REPOSITORY}: {vane_repository}")
    vane_revision = require_string(vane_raw, "revision", "vane.revision")
    if not FULL_SHA_RE.fullmatch(vane_revision):
        fail("vane.revision must be a full lowercase 40-character commit SHA")
    if vane_revision == "0" * 40:
        fail("vane.revision must not be the template placeholder")

    return ExtensionManifest(
        schema_version=schema_version,
        name=name,
        extension_config=extension_config,
        build_extensions=tuple(build_extensions),
        native_test_selection=native_test_selection,
        vane_repository=vane_repository,
        vane_revision=vane_revision,
    )


def verify_vane_checkout(
    vane_source: Path,
    manifest: ExtensionManifest,
    *,
    require_complete_history: bool = True,
) -> None:
    if not (vane_source / "external/duckdb/CMakeLists.txt").is_file():
        fail(f"Vane checkout is missing external/duckdb: {vane_source}")
    for relative in (
        "scripts/sync_duckdb_source_id.py",
        "scripts/resolve_duckdb_fork_version.py",
        "DUCKDB_UPSTREAM_VERSION",
    ):
        if not (vane_source / relative).is_file():
            fail(f"Vane checkout is missing {relative}: {vane_source}")

    actual_revision = run(["git", "rev-parse", "HEAD"], cwd=vane_source, capture=True)
    if actual_revision != manifest.vane_revision:
        fail(
            "Vane checkout revision mismatch: "
            f"expected {manifest.vane_revision}, got {actual_revision}"
        )
    status = run(["git", "status", "--porcelain"], cwd=vane_source, capture=True)
    if status:
        fail(f"Vane checkout contains tracked changes: {vane_source}")
    if not require_complete_history:
        return
    shallow = run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=vane_source,
        capture=True,
    )
    if shallow == "true":
        fail(
            "Vane checkout has incomplete Git history; rerun vane_prepare to fetch "
            "the complete history required for Vane identity resolution"
        )
    if shallow != "false":
        fail(f"Vane checkout returned an invalid shallow-repository state: {shallow!r}")


def verify_official_revision(
    repository: str,
    repository_url: str,
    revision: str,
    label: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="official-revision-") as temporary:
        verification_repository = Path(temporary) / "repository.git"
        run(["git", "init", "--bare", "--quiet", str(verification_repository)])
        try:
            run(
                [
                    "git",
                    "-c",
                    "protocol.version=2",
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "--filter=tree:0",
                    repository_url,
                    revision,
                ],
                cwd=verification_repository,
            )
        except subprocess.CalledProcessError:
            fail(f"{label} revision is not available from {repository}: {revision}")

        fetched_revision = run(
            ["git", "rev-parse", "FETCH_HEAD^{commit}"],
            cwd=verification_repository,
            capture=True,
        )
        if fetched_revision != revision:
            fail(
                f"official {label} fetch returned an unexpected revision: "
                f"expected {revision}, got {fetched_revision}"
            )


def verify_official_vane_revision(manifest: ExtensionManifest) -> None:
    verify_official_revision(
        VANE_REPOSITORY,
        VANE_REPOSITORY_URL,
        manifest.vane_revision,
        "Vane",
    )


def verify_ci_tools_checkout(ci_tools_source: Path, expected_sha: str) -> None:
    if not FULL_SHA_RE.fullmatch(expected_sha):
        fail("expected CI-tools SHA must be a full lowercase 40-character commit SHA")
    if not (ci_tools_source / "scripts/vane_extension.py").is_file():
        fail(
            f"CI-tools checkout is missing scripts/vane_extension.py: {ci_tools_source}"
        )

    actual_sha = run(["git", "rev-parse", "HEAD"], cwd=ci_tools_source, capture=True)
    if actual_sha != expected_sha:
        fail(
            "CI-tools checkout revision mismatch: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    status = run(["git", "status", "--porcelain"], cwd=ci_tools_source, capture=True)
    if status:
        fail(f"CI-tools checkout contains working-tree changes: {ci_tools_source}")

    verify_official_revision(
        CI_TOOLS_REPOSITORY,
        CI_TOOLS_REPOSITORY_URL,
        expected_sha,
        "CI-tools",
    )


def unshallow_vane_checkout(vane_source: Path, manifest: ExtensionManifest) -> None:
    shallow = run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=vane_source,
        capture=True,
    )
    if shallow not in {"true", "false"}:
        fail(f"Vane checkout returned an invalid shallow-repository state: {shallow!r}")
    if shallow == "true":
        run(
            [
                "git",
                "fetch",
                "--unshallow",
                VANE_REPOSITORY_URL,
                manifest.vane_revision,
            ],
            cwd=vane_source,
        )


def prepare_vane(vane_source: Path, manifest: ExtensionManifest) -> None:
    if vane_source.exists():
        if not (vane_source / ".git").exists():
            fail(f"existing Vane source is not a Git checkout: {vane_source}")
        verify_vane_checkout(vane_source, manifest, require_complete_history=False)
        verify_official_vane_revision(manifest)
        unshallow_vane_checkout(vane_source, manifest)
        verify_vane_checkout(vane_source, manifest)
        return

    vane_source.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", str(vane_source)])
    run(["git", "remote", "add", "origin", VANE_REPOSITORY_URL], cwd=vane_source)
    run(["git", "fetch", "origin", manifest.vane_revision], cwd=vane_source)
    run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=vane_source)
    verify_vane_checkout(vane_source, manifest)


def decode_jwt_claims(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3:
        fail("GitHub OIDC token is not a JWT")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as exc:
        fail(f"GitHub OIDC token payload is invalid: {exc}")
    if not isinstance(claims, dict):
        fail("GitHub OIDC token payload is not an object")
    return claims


def verify_reusable_workflow_identity(token: str, expected_sha: str) -> None:
    if not FULL_SHA_RE.fullmatch(expected_sha):
        fail(
            "expected reusable workflow SHA must be a full lowercase 40-character commit SHA"
        )
    claims = decode_jwt_claims(token)
    if claims.get("aud") != OIDC_AUDIENCE:
        fail(
            "reusable workflow token audience mismatch: "
            f"expected {OIDC_AUDIENCE}, got {claims.get('aud')!r}"
        )
    expected_ref = f"{CI_TOOLS_REPOSITORY}/{CI_TOOLS_WORKFLOW_PATH}@{expected_sha}"
    if claims.get("job_workflow_ref") != expected_ref:
        fail(
            "reusable workflow ref mismatch: "
            f"expected {expected_ref}, got {claims.get('job_workflow_ref')!r}"
        )
    if claims.get("job_workflow_sha") != expected_sha:
        fail(
            "reusable workflow SHA mismatch: "
            f"expected {expected_sha}, got {claims.get('job_workflow_sha')!r}"
        )


def fetch_reusable_workflow_oidc_token() -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        fail(
            "GitHub OIDC token environment is unavailable for reusable workflow verification"
        )
    separator = "&" if "?" in request_url else "?"
    url = (
        f"{request_url}{separator}{urllib.parse.urlencode({'audience': OIDC_AUDIENCE})}"
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {request_token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except OSError as exc:
        fail(f"could not request GitHub OIDC token: {exc}")
    token = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        fail("GitHub OIDC response did not contain a token")
    return token


def resolve_vane_identity(
    vane_source: Path, manifest: ExtensionManifest
) -> VaneIdentity:
    verify_vane_checkout(vane_source, manifest)
    source_id = run(
        [
            sys.executable,
            str(vane_source / "scripts/sync_duckdb_source_id.py"),
            "--print",
        ],
        cwd=vane_source,
        capture=True,
    )
    if not SOURCE_ID_RE.fullmatch(source_id):
        fail(f"Vane returned an invalid DuckDB SourceID: {source_id!r}")
    fork_version = run(
        [
            sys.executable,
            str(vane_source / "scripts/resolve_duckdb_fork_version.py"),
            "--print-version",
        ],
        cwd=vane_source,
        capture=True,
    )
    if not FORK_VERSION_RE.fullmatch(fork_version):
        fail(f"Vane returned an invalid DuckDB fork version: {fork_version!r}")
    upstream_version = (vane_source / "DUCKDB_UPSTREAM_VERSION").read_text().strip()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", upstream_version):
        fail(f"Vane returned an invalid DuckDB upstream version: {upstream_version!r}")
    return VaneIdentity(source_id, fork_version, upstream_version)


def require_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        fail(f"{label} must be a positive integer: {value}")
    if parsed <= 0:
        fail(f"{label} must be a positive integer: {value}")
    return parsed


def verify_selected_test_output(output: str) -> None:
    if "All tests were skipped" in output:
        fail("selected native test did not execute: all test cases were skipped")
    if "No tests ran" in output:
        fail("selected native test did not execute: no test cases ran")


def run_selected_test(test_binary: Path, selection: str, extension_root: Path) -> None:
    command = [str(test_binary), selection]
    print(f"+ {' '.join(command)}", file=sys.stderr)
    result = subprocess.run(
        command,
        cwd=extension_root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(result.stdout, end="")
    result.check_returncode()
    verify_selected_test_output(result.stdout)


def run_native(
    extension_root: Path,
    manifest: ExtensionManifest,
    vane_source: Path,
    build_dir: Path,
    jobs: int,
    skip_tests: bool,
) -> None:
    identity = resolve_vane_identity(vane_source, manifest)
    toolchain_value = os.environ.get("VCPKG_TOOLCHAIN_PATH", "")
    toolchain = Path(toolchain_value).resolve() if toolchain_value else None
    if toolchain is None or not toolchain.is_file():
        fail("VCPKG_TOOLCHAIN_PATH must identify the vcpkg CMake toolchain")

    target_triplet = os.environ.get("VCPKG_TARGET_TRIPLET", "x64-linux")
    extension_config = resolve_within(
        extension_root, manifest.extension_config, "extension_config"
    )
    build_dir = build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)

    cmake_command = [
        "cmake",
        "--fresh",
        "-S",
        str(vane_source / "external/duckdb"),
        "-B",
        str(build_dir),
        "-G",
        os.environ.get("VANE_CMAKE_GENERATOR", "Ninja"),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_CXX_STANDARD=20",
        "-DCMAKE_CXX_STANDARD_REQUIRED=ON",
        "-DCMAKE_CXX_EXTENSIONS=OFF",
        "-DBUILD_UNITTESTS=ON",
        "-DBUILD_BENCHMARKS=OFF",
        "-DBUILD_DISTRIBUTED_EXCHANGE=OFF",
        "-DEXTENSION_STATIC_BUILD=ON",
        "-DENABLE_EXTENSION_AUTOLOADING=OFF",
        "-DENABLE_EXTENSION_AUTOINSTALL=OFF",
        f"-DDUCKDB_EXTENSION_CONFIGS={extension_config}",
        f"-DBUILD_EXTENSIONS={manifest.build_extensions_cmake}",
        f"-DUNITTEST_ROOT_DIRECTORY={extension_root}",
        "-DENABLE_UNITTEST_CPP_TESTS=FALSE",
        "-DVCPKG_BUILD=ON",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DVCPKG_MANIFEST_DIR={extension_root}",
        f"-DVCPKG_TARGET_TRIPLET={target_triplet}",
        f"-DOVERRIDE_GIT_DESCRIBE={identity.upstream_version}-0-g{identity.source_id[:10]}",
        f"-DDUCKDB_EXPLICIT_VERSION={identity.fork_version}",
        f"-DGIT_COMMIT_HASH={identity.source_id[:10]}",
    ]
    prefix_path = os.environ.get("VANE_CMAKE_PREFIX_PATH")
    if prefix_path:
        cmake_command.append(f"-DCMAKE_PREFIX_PATH={prefix_path}")
    run(cmake_command, cwd=extension_root)

    run(
        [
            "cmake",
            "--build",
            str(build_dir),
            "--target",
            "unittest",
            f"{manifest.name}_loadable_extension",
            "--parallel",
            str(jobs),
        ],
        cwd=extension_root,
    )
    if skip_tests:
        return

    test_binary = build_dir / "test/unittest"
    if not test_binary.is_file():
        fail(f"DuckDB unittest binary was not generated: {test_binary}")
    run_selected_test(test_binary, manifest.native_test_selection, extension_root)


def manifest_outputs(
    manifest: ExtensionManifest, extension_root: Path
) -> dict[str, str]:
    return {
        "extension_name": manifest.name,
        "extension_config": str(
            resolve_within(
                extension_root, manifest.extension_config, "extension_config"
            )
        ),
        "build_extensions": manifest.build_extensions_cmake,
        "native_test_selection": manifest.native_test_selection,
        "vane_repository": manifest.vane_repository,
        "vane_revision": manifest.vane_revision,
    }


def write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                fail(f"GitHub output {key} contains a newline")
            handle.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--extension-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--github-output", type=Path)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--vane-source", type=Path, required=True)

    identity_parser = subparsers.add_parser("identity")
    identity_parser.add_argument("--vane-source", type=Path, required=True)
    identity_parser.add_argument("--github-output", type=Path)

    workflow_parser = subparsers.add_parser("verify-reusable-workflow")
    workflow_parser.add_argument("--expected-sha", required=True)

    ci_tools_parser = subparsers.add_parser("verify-ci-tools")
    ci_tools_parser.add_argument("--ci-tools-source", type=Path, required=True)
    ci_tools_parser.add_argument("--expected-sha", required=True)

    native_parser = subparsers.add_parser("native")
    native_parser.add_argument("--vane-source", type=Path, required=True)
    native_parser.add_argument("--build-dir", type=Path, required=True)
    native_parser.add_argument("--jobs", default="2")
    native_parser.add_argument("--skip-tests", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    extension_root = args.extension_root.resolve()
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path, extension_root)

    if args.command == "manifest":
        outputs = manifest_outputs(manifest, extension_root)
        if args.github_output:
            write_github_outputs(args.github_output, outputs)
        print(json.dumps({**asdict(manifest), **outputs}, indent=2, sort_keys=True))
    elif args.command == "prepare":
        prepare_vane(args.vane_source.resolve(), manifest)
    elif args.command == "identity":
        identity = resolve_vane_identity(args.vane_source.resolve(), manifest)
        values = asdict(identity)
        if args.github_output:
            write_github_outputs(args.github_output, values)
        print(json.dumps(values, indent=2, sort_keys=True))
    elif args.command == "verify-reusable-workflow":
        verify_reusable_workflow_identity(
            fetch_reusable_workflow_oidc_token(), args.expected_sha
        )
    elif args.command == "verify-ci-tools":
        verify_ci_tools_checkout(args.ci_tools_source.resolve(), args.expected_sha)
    elif args.command == "native":
        jobs = require_positive_int(args.jobs, "jobs")
        run_native(
            extension_root,
            manifest,
            args.vane_source.resolve(),
            args.build_dir,
            jobs,
            args.skip_tests,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
