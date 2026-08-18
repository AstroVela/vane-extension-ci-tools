#!/usr/bin/env python3
"""Strict Vane integration helpers for out-of-tree DuckDB extensions."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
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
LINUX_X86_64_RENAMEAT2_SYSCALL = 316
VANE_WHEEL_VERIFY_SCRIPT = r"""
import json
import sys

import vane

extension_name, expected_version, expected_source_id = sys.argv[1:]
connection = vane.connect(
    ":memory:",
    config={
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
    },
)
extension = connection.execute(
    "SELECT loaded, install_mode FROM duckdb_extensions() "
    "WHERE extension_name = ?",
    [extension_name],
).fetchone()
if extension is None:
    raise RuntimeError(f"wheel does not contain extension {extension_name!r}")
if extension[1] != "STATICALLY_LINKED":
    raise RuntimeError(
        f"extension {extension_name!r} is not statically linked: {extension[1]!r}"
    )
connection.execute(f"LOAD {extension_name}")
loaded = connection.execute(
    "SELECT loaded FROM duckdb_extensions() WHERE extension_name = ?",
    [extension_name],
).fetchone()
if loaded != (True,):
    raise RuntimeError(f"extension {extension_name!r} did not load from the wheel")
actual_version, actual_source_id = connection.execute(
    "SELECT library_version, source_id FROM pragma_version()"
).fetchone()
if actual_version != expected_version:
    raise RuntimeError(
        f"Vane fork version mismatch: expected {expected_version!r}, "
        f"got {actual_version!r}"
    )
expected_runtime_source_id = expected_source_id[:10]
if actual_source_id != expected_runtime_source_id:
    raise RuntimeError(
        "DuckDB SourceID mismatch: expected the prefix "
        f"{expected_runtime_source_id!r} of the verified full SourceID "
        f"{expected_source_id!r}, "
        f"got {actual_source_id!r}"
    )
print(
    json.dumps(
        {
            "extension": extension_name,
            "fork_version": actual_version,
            "source_id": actual_source_id,
            "install_mode": extension[1],
        },
        sort_keys=True,
    )
)
"""


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
    env: Mapping[str, str] | None = None,
) -> str:
    rendered = " ".join(command)
    print(f"+ {rendered}", file=sys.stderr)
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env=dict(env) if env is not None else None,
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
    if name in build_extensions:
        fail(
            "build_extensions must not contain the target extension; "
            "extension_config owns its source selection"
        )

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


def parse_vane_tag_refs(output: str, label: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            fail(f"{label} returned an invalid Vane tag ref: {line!r}")
        object_id, ref = fields
        if ref.endswith("^{}"):
            continue
        if not FULL_SHA_RE.fullmatch(object_id) or not ref.startswith("refs/tags/"):
            fail(f"{label} returned an invalid Vane tag ref: {line!r}")
        if ref in tags:
            fail(f"{label} returned a duplicate Vane tag ref: {ref}")
        tags[ref] = object_id
    return tags


def fetch_official_vane_tags(vane_source: Path) -> None:
    run(["git", "fetch", "--tags", VANE_REPOSITORY_URL], cwd=vane_source)
    official_tags = parse_vane_tag_refs(
        run(
            ["git", "ls-remote", "--tags", VANE_REPOSITORY_URL],
            capture=True,
        ),
        VANE_REPOSITORY,
    )
    local_tags = parse_vane_tag_refs(
        run(
            [
                "git",
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "refs/tags",
            ],
            cwd=vane_source,
            capture=True,
        ),
        "Vane checkout",
    )
    differing_refs = sorted(
        ref
        for ref in official_tags.keys() | local_tags.keys()
        if official_tags.get(ref) != local_tags.get(ref)
    )
    if differing_refs:
        fail(
            "Vane checkout tag refs do not exactly match AstroVela/vane: "
            f"{differing_refs[0]}"
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


def publish_vane_checkout(staged_source: Path, vane_source: Path) -> None:
    if (
        sys.platform != "linux"
        or os.uname().machine != "x86_64"
        or ctypes.sizeof(ctypes.c_void_p) != 8
    ):
        fail("atomic Vane checkout publication requires 64-bit x86 Linux renameat2")

    try:
        syscall = ctypes.CDLL(None, use_errno=True).syscall
    except (AttributeError, OSError):
        fail("atomic Vane checkout publication requires the Linux syscall interface")

    syscall.argtypes = [ctypes.c_long]
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = syscall(
        LINUX_X86_64_RENAMEAT2_SYSCALL,
        ctypes.c_int(-100),  # AT_FDCWD
        ctypes.c_char_p(os.fsencode(staged_source)),
        ctypes.c_int(-100),  # AT_FDCWD
        ctypes.c_char_p(os.fsencode(vane_source)),
        ctypes.c_uint(1),  # RENAME_NOREPLACE
    )
    if result == 0:
        return

    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        fail(f"Vane source appeared during preparation: {vane_source}")
    if error_number == errno.ENOSYS:
        fail("atomic Vane checkout publication requires Linux renameat2 support")
    raise OSError(error_number, os.strerror(error_number), vane_source)


def prepare_vane(vane_source: Path, manifest: ExtensionManifest) -> None:
    if os.path.lexists(vane_source):
        if not (vane_source / ".git").exists():
            fail(f"existing Vane source is not a Git checkout: {vane_source}")
        verify_vane_checkout(vane_source, manifest, require_complete_history=False)
        verify_official_vane_revision(manifest)
        unshallow_vane_checkout(vane_source, manifest)
        fetch_official_vane_tags(vane_source)
        verify_vane_checkout(vane_source, manifest)
        return

    vane_source.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{vane_source.name}.prepare-", dir=vane_source.parent
    ) as temporary:
        staged_source = Path(temporary) / "source"
        run(["git", "init", str(staged_source)])
        run(
            ["git", "remote", "add", "origin", VANE_REPOSITORY_URL],
            cwd=staged_source,
        )
        run(["git", "fetch", "origin", manifest.vane_revision], cwd=staged_source)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=staged_source)
        fetch_official_vane_tags(staged_source)
        verify_vane_checkout(staged_source, manifest)
        publish_vane_checkout(staged_source, vane_source)


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


def cmake_compiler_launcher_args() -> list[str]:
    launcher = os.environ.get("VANE_CMAKE_COMPILER_LAUNCHER")
    if launcher is None:
        return []
    if launcher != "ccache":
        fail("VANE_CMAKE_COMPILER_LAUNCHER must be ccache")
    return [
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
    ]


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
    duckdb_source = vane_source / "external/duckdb"
    build_dir = build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)

    cmake_command = [
        "cmake",
        "--fresh",
        "-S",
        str(duckdb_source),
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
        f"-DDUCKDB_SOURCE_PATH={duckdb_source}",
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
    cmake_command.extend(cmake_compiler_launcher_args())
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


def load_vane_default_build_extensions(vane_source: Path) -> tuple[str, ...]:
    pyproject_path = vane_source / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
        value = pyproject["tool"]["scikit-build"]["cmake"]["define"][
            "BUILD_EXTENSIONS"
        ]
    except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        fail(f"Vane checkout has no valid wheel extension configuration: {exc}")

    if not isinstance(value, str) or not value:
        fail("Vane wheel BUILD_EXTENSIONS must be a non-empty string")
    extensions: list[str] = []
    for extension in value.split(";"):
        if not NAME_RE.fullmatch(extension):
            fail(f"Vane wheel contains a non-canonical extension name: {extension!r}")
        if extension in extensions:
            fail(f"Vane wheel contains a duplicate extension: {extension}")
        extensions.append(extension)
    return tuple(extensions)


def write_vane_wheel_link_config(build_dir: Path, extension_name: str) -> Path:
    config_path = build_dir / "vane-extension-link.cmake"
    config_path.write_text(
        "# Generated by vane-extension-ci-tools.\n"
        "# Propagate the caller config's linked extensions back to Vane.\n"
        "set(_VANE_LINKED_EXTENSIONS)\n"
        "foreach(_VANE_EXTENSION_NAME IN LISTS DUCKDB_EXTENSION_NAMES)\n"
        "  string(TOUPPER \"${_VANE_EXTENSION_NAME}\" _VANE_EXTENSION_UPPER)\n"
        "  if(DUCKDB_EXTENSION_${_VANE_EXTENSION_UPPER}_SHOULD_LINK)\n"
        "    list(APPEND _VANE_LINKED_EXTENSIONS \"${_VANE_EXTENSION_NAME}\")\n"
        "  endif()\n"
        "endforeach()\n"
        f'list(FIND _VANE_LINKED_EXTENSIONS "{extension_name}" '
        "_VANE_TARGET_INDEX)\n"
        "if(_VANE_TARGET_INDEX EQUAL -1)\n"
        f'  message(FATAL_ERROR "Caller config did not register {extension_name} '
        'for static linking")\n'
        "endif()\n"
        'set(BUILD_EXTENSIONS "${_VANE_LINKED_EXTENSIONS}" PARENT_SCOPE)\n',
        encoding="utf-8",
    )
    return config_path


def write_vane_wheel_dependency_prefix_config(
    build_dir: Path, dependency_prefix: Path
) -> Path:
    config_path = build_dir / "vane-dependency-prefix.cmake"
    config_path.write_text(
        "# Generated by vane-extension-ci-tools.\n"
        "# Keep the PEP 517 backend's package prefixes (including pybind11) "
        "while adding the exact Vane dependency prefix.\n"
        f'list(PREPEND CMAKE_PREFIX_PATH "{dependency_prefix}")\n',
        encoding="utf-8",
    )
    return config_path


def require_vane_wheel_platform(target_triplet: str) -> None:
    if (
        sys.platform != "linux"
        or os.uname().machine != "x86_64"
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or target_triplet != "x64-linux"
    ):
        fail(
            "Vane wheel builds require 64-bit x86 Linux and "
            "VCPKG_TARGET_TRIPLET=x64-linux"
        )


def reject_duckdb_local_extension_config(vane_source: Path) -> None:
    local_config = (
        vane_source
        / "external/duckdb/extension/extension_config_local.cmake"
    )
    if local_config.exists():
        fail(
            "Vane wheel builds reject DuckDB's ignored local extension config: "
            f"{local_config}"
        )


def require_vane_wheel_dependency_prefix(
    vane_source: Path, target_triplet: str
) -> Path:
    installed_value = os.environ.get("VANE_VCPKG_INSTALLED_DIR", "")
    installed_root = (
        Path(installed_value).resolve()
        if installed_value
        else vane_source / "vcpkg_installed"
    )
    prefix = installed_root / target_triplet
    for relative in (
        "share/arrow/ArrowConfig.cmake",
        "share/arrowflight/ArrowFlightConfig.cmake",
    ):
        if not (prefix / relative).is_file():
            fail(
                "Vane wheel dependencies are incomplete; run the exact checkout's "
                f"scripts/bootstrap_vcpkg.sh first: {prefix / relative}"
            )
    return prefix


def verify_vane_wheel(
    wheel_path: Path,
    manifest: ExtensionManifest,
    identity: VaneIdentity,
    temporary_parent: Path,
) -> None:
    if not wheel_path.is_file():
        fail(f"Vane wheel does not exist: {wheel_path}")
    with tempfile.TemporaryDirectory(
        prefix=".vane-wheel-verify-", dir=temporary_parent
    ) as temporary:
        environment_root = Path(temporary)
        verification_environment = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            verification_environment.pop(name, None)
        run(
            [sys.executable, "-m", "venv", str(environment_root)],
            env=verification_environment,
        )
        verification_python = environment_root / "bin/python"
        run(
            [
                str(verification_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel_path),
            ],
            env=verification_environment,
        )
        run(
            [
                str(verification_python),
                "-I",
                "-c",
                VANE_WHEEL_VERIFY_SCRIPT,
                manifest.name,
                identity.fork_version,
                identity.source_id,
            ],
            env=verification_environment,
        )


def build_vane_wheel(
    extension_root: Path,
    manifest: ExtensionManifest,
    vane_source: Path,
    build_dir: Path,
    dist_dir: Path,
    jobs: int,
) -> Path:
    target_triplet = os.environ.get("VCPKG_TARGET_TRIPLET", "x64-linux")
    require_vane_wheel_platform(target_triplet)
    reject_duckdb_local_extension_config(vane_source)
    toolchain_value = os.environ.get("VCPKG_TOOLCHAIN_PATH", "")
    toolchain = Path(toolchain_value).resolve() if toolchain_value else None
    if toolchain is None or not toolchain.is_file():
        fail("VCPKG_TOOLCHAIN_PATH must identify the vcpkg CMake toolchain")

    identity = resolve_vane_identity(vane_source, manifest)
    vane_dependency_prefix = require_vane_wheel_dependency_prefix(
        vane_source, target_triplet
    )
    extension_config = resolve_within(
        extension_root, manifest.extension_config, "extension_config"
    )
    build_dir = build_dir.resolve()
    dist_dir = dist_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    link_config = write_vane_wheel_link_config(build_dir, manifest.name)
    dependency_prefix_config = write_vane_wheel_dependency_prefix_config(
        build_dir, vane_dependency_prefix
    )

    build_extensions = [
        extension
        for extension in load_vane_default_build_extensions(vane_source)
        if extension != manifest.name
    ]
    for extension in manifest.build_extensions:
        if extension not in build_extensions:
            build_extensions.append(extension)

    cmake_args = [
        "--fresh",
        "-DBUILD_DISTRIBUTED_EXCHANGE=ON",
        "-DENABLE_EXTENSION_AUTOLOADING=OFF",
        "-DENABLE_EXTENSION_AUTOINSTALL=OFF",
        "-DVCPKG_BUILD=ON",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        f"-DVCPKG_MANIFEST_DIR={extension_root}",
        f"-DVCPKG_INSTALLED_DIR={build_dir / 'vcpkg_installed'}",
        f"-DVCPKG_TARGET_TRIPLET={target_triplet}",
        f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={dependency_prefix_config}",
        f"-DDUCKDB_EXTENSION_CONFIGS={extension_config};{link_config}",
        f"-DBUILD_EXTENSIONS={';'.join(build_extensions)}",
    ]
    cmake_args.extend(cmake_compiler_launcher_args())
    build_environment = os.environ.copy()
    vcpkg_selection_variables = {
        "VCPKG_CHAINLOAD_TOOLCHAIN_FILE",
        "VCPKG_DEFAULT_HOST_TRIPLET",
        "VCPKG_DEFAULT_TRIPLET",
        "VCPKG_OVERLAY_PORTS",
        "VCPKG_OVERLAY_TRIPLETS",
    }
    for name in tuple(build_environment):
        if (
            name in {
                "CMAKE_ARGS",
                "CMAKE_PREFIX_PATH",
                "COVERAGE",
                "DONT_LINK",
                "GITHUB_BASE_REF",
                "GITHUB_REF_NAME",
                "VANE_CMAKE_PREFIX_PATH",
                "VANE_CMAKE_COMPILER_LAUNCHER",
                "VANE_VERSION_BRANCH",
            }
            or name in vcpkg_selection_variables
            or name.startswith("SETUPTOOLS_SCM_PRETEND_VERSION")
            or name.startswith("SKBUILD_")
            or (name.startswith("DUCKDB_") and name.endswith("_DIRECTORY"))
        ):
            build_environment.pop(name)
    build_environment.update(
        {
            "CMAKE_ARGS": shlex.join(cmake_args),
            "CMAKE_BUILD_PARALLEL_LEVEL": str(jobs),
            "CMAKE_GENERATOR": os.environ.get("VANE_CMAKE_GENERATOR", "Ninja"),
            "SKBUILD_BUILD_DIR": str(build_dir),
            "SKBUILD_CMAKE_BUILD_TYPE": "Release",
            "VCPKG_MAX_CONCURRENCY": str(jobs),
            "VCPKG_TARGET_TRIPLET": target_triplet,
            "VCPKG_TOOLCHAIN_PATH": str(toolchain),
        }
    )
    build_environment[f"DUCKDB_{manifest.name.upper()}_DIRECTORY"] = str(
        extension_root
    )
    with tempfile.TemporaryDirectory(
        prefix=".vane-wheel-output-", dir=dist_dir.parent
    ) as temporary_output:
        output_dir = Path(temporary_output)
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(output_dir),
                str(vane_source),
            ],
            cwd=extension_root,
            env=build_environment,
        )

        wheels = sorted(output_dir.glob("*.whl"))
        if len(wheels) != 1:
            fail(
                "Vane wheel build must produce exactly one wheel in its "
                "isolated output directory"
            )
        staged_wheel = wheels[0]
        verify_vane_wheel(staged_wheel, manifest, identity, build_dir.parent)
        stale_wheels = sorted(
            path for path in dist_dir.glob("*.whl") if path.name != staged_wheel.name
        )
        if stale_wheels:
            fail(
                "Vane wheel dist directory contains a stale wheel with a different "
                f"filename: {stale_wheels[0]}"
            )
        wheel_path = dist_dir / staged_wheel.name
        os.replace(staged_wheel, wheel_path)
        return wheel_path


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

    ci_tools_parser = subparsers.add_parser("verify-ci-tools")
    ci_tools_parser.add_argument("--ci-tools-source", type=Path, required=True)
    ci_tools_parser.add_argument("--expected-sha", required=True)

    native_parser = subparsers.add_parser("native")
    native_parser.add_argument("--vane-source", type=Path, required=True)
    native_parser.add_argument("--build-dir", type=Path, required=True)
    native_parser.add_argument("--jobs", default="8")
    native_parser.add_argument("--skip-tests", action="store_true")

    wheel_parser = subparsers.add_parser("wheel")
    wheel_parser.add_argument("--vane-source", type=Path, required=True)
    wheel_parser.add_argument("--build-dir", type=Path, required=True)
    wheel_parser.add_argument("--dist-dir", type=Path, required=True)
    wheel_parser.add_argument("--jobs", default="8")
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
    elif args.command == "wheel":
        jobs = require_positive_int(args.jobs, "jobs")
        wheel_path = build_vane_wheel(
            extension_root,
            manifest,
            args.vane_source.resolve(),
            args.build_dir,
            args.dist_dir,
            jobs,
        )
        print(wheel_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigurationError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
