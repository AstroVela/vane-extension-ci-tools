#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Vane contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate immutable provider release sets and package-index file identities.

This is a release-matrix gate, not an artifact security verifier. The exact
Vane checkout's build_extension_wheel.py and verify_extension_wheel.py must
approve the native artifacts before this tool is called.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

INDEX_JSON_BASES = {
    "testpypi": "https://test.pypi.org/pypi",
    "pypi": "https://pypi.org/pypi",
}
RELEASE_CHANNELS = ("testpypi-dev", "release")
MAX_METADATA_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024


class ReleaseValidationError(RuntimeError):
    """A release configuration, wheel matrix, or indexed file set is invalid."""


def _load_source_tools():
    path = Path(__file__).resolve().with_name("vane_extension.py")
    spec = importlib.util.spec_from_file_location("_vane_release_source_tools", path)
    if spec is None or spec.loader is None:
        raise ReleaseValidationError(f"cannot load source verification tools: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_sources(
    manifest_path: Path,
    extension_root: Path,
    vane_source: Path,
    ci_tools_version: str,
) -> None:
    """Reuse the native lane's exact official source and clean-checkout gates."""
    source_tools = _load_source_tools()
    try:
        manifest = source_tools.load_manifest(
            manifest_path.resolve(), extension_root.resolve()
        )
        source_tools.verify_ci_tools_checkout(
            Path(__file__).resolve().parents[1], ci_tools_version
        )
        source_tools.verify_official_vane_revision(manifest)
        # No build/version derivation happens here, so a clean shallow checkout
        # of the exact official Vane commit is sufficient for release assembly.
        source_tools.verify_vane_checkout(
            vane_source.resolve(), manifest, require_complete_history=False
        )
    except (source_tools.ConfigurationError, subprocess.CalledProcessError) as error:
        raise ReleaseValidationError(
            f"release source verification failed: {error}"
        ) from error


@dataclass(frozen=True)
class Provider:
    name: str
    distribution: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseConfig:
    interpreters: tuple[str, ...]
    platforms: tuple[str, ...]
    max_wheel_bytes: int
    providers: tuple[Provider, ...]

    @property
    def tags(self) -> frozenset[Tag]:
        return frozenset(
            Tag(interpreter, "none", platform)
            for interpreter in self.interpreters
            for platform in self.platforms
        )

    def provider(self, name: str) -> Provider:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ReleaseValidationError(f"unknown provider: {name}")

    def dependency_closure(self, name: str) -> frozenset[str]:
        def visit(current: str, ancestors: frozenset[str]) -> set[str]:
            if current in ancestors:
                raise ReleaseValidationError(f"provider dependency cycle at {current}")
            result: set[str] = set()
            for dependency in self.provider(current).dependencies:
                result.add(dependency)
                result.update(visit(dependency, ancestors | {current}))
            return result

        return frozenset(visit(name, frozenset()))


def _string_list(
    value: object, description: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ReleaseValidationError(f"{description} must be a unique string array")
    return tuple(value)


def load_config(path: Path) -> ReleaseConfig:
    with path.open("rb") as source:
        raw = tomllib.load(source)
    if set(raw) != {"interpreters", "platforms", "max_wheel_bytes", "providers"}:
        raise ReleaseValidationError("release config has missing or unknown fields")
    interpreters = _string_list(raw["interpreters"], "interpreters")
    platforms = _string_list(raw["platforms"], "platforms")
    if any(not re.fullmatch(r"cp[1-9][0-9]+", value) for value in interpreters):
        raise ReleaseValidationError("interpreters must be explicit CPython tags")
    if any(
        value == "any" or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", value)
        for value in platforms
    ):
        raise ReleaseValidationError("platforms must be explicit native platform tags")
    limit = raw["max_wheel_bytes"]
    if type(limit) is not int or limit <= 0:
        raise ReleaseValidationError("max_wheel_bytes must be a positive byte count")
    if not isinstance(raw["providers"], dict) or not raw["providers"]:
        raise ReleaseValidationError("providers must be a nonempty table")
    providers = []
    distributions = set()
    for name, entry in raw["providers"].items():
        if name == "vane" or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ReleaseValidationError(f"invalid provider output name: {name!r}")
        if not isinstance(entry, dict) or set(entry) != {
            "distribution",
            "dependencies",
        }:
            raise ReleaseValidationError(
                f"provider {name} has missing or unknown fields"
            )
        distribution = entry["distribution"]
        if (
            not isinstance(distribution, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", distribution)
            or distribution == "vane-ai"
            or distribution in distributions
        ):
            raise ReleaseValidationError(
                "provider distributions must be unique canonical names other than vane-ai"
            )
        distributions.add(distribution)
        providers.append(
            Provider(
                name,
                distribution,
                _string_list(
                    entry["dependencies"], f"{name}.dependencies", allow_empty=True
                ),
            )
        )
    config = ReleaseConfig(interpreters, platforms, limit, tuple(providers))
    for provider in config.providers:
        config.dependency_closure(provider.name)
    return config


@dataclass(frozen=True)
class WheelRecord:
    path: Path
    distribution: str
    version: str
    tag: Tag
    requirements: tuple[Requirement, ...]


def _canonical_version(value: str, description: str) -> str:
    try:
        parsed = Version(value)
    except InvalidVersion as error:
        raise ReleaseValidationError(
            f"{description} is not a valid PEP 440 version"
        ) from error
    if str(parsed) != value or parsed.local is not None:
        raise ReleaseValidationError(
            f"{description} must use canonical public PEP 440 spelling"
        )
    return value


def validate_vane_version(value: str, channel: str) -> None:
    """Keep development candidates separate from tagged public releases."""
    if channel not in RELEASE_CHANNELS:
        raise ReleaseValidationError(f"unknown release channel: {channel}")
    _canonical_version(value, "Vane version")
    version = Version(value)
    if version.epoch != 0 or len(version.release) != 3:
        raise ReleaseValidationError("Vane version must use X.Y.Z without an epoch")
    if channel == "testpypi-dev" and not version.is_devrelease:
        raise ReleaseValidationError(
            "Vane TestPyPI development candidate must have a development version"
        )
    if channel == "release" and version.is_devrelease:
        raise ReleaseValidationError(
            "Vane release channel forbids development versions"
        )


def _read_wheel(path: Path, config: ReleaseConfig) -> WheelRecord:
    if not path.is_file() or path.is_symlink():
        raise ReleaseValidationError(f"{path.name} must be a regular wheel file")
    if path.stat().st_size > config.max_wheel_bytes:
        raise ReleaseValidationError(
            f"{path.name} exceeds max_wheel_bytes={config.max_wheel_bytes}"
        )
    filename_name, filename_version, build, tags = parse_wheel_filename(path.name)
    if build or len(tags) != 1 or not tags <= config.tags:
        raise ReleaseValidationError(
            f"{path.name} must have one configured tag and no build tag"
        )
    try:
        with zipfile.ZipFile(path) as wheel:
            members = [
                member
                for member in wheel.infolist()
                if member.filename.count("/") == 1
                and member.filename.endswith(".dist-info/METADATA")
            ]
            if len(members) != 1:
                raise ReleaseValidationError(
                    f"{path.name} must have exactly one top-level METADATA"
                )
            if members[0].file_size > MAX_METADATA_BYTES:
                raise ReleaseValidationError(
                    f"{path.name} METADATA exceeds {MAX_METADATA_BYTES} bytes"
                )
            metadata = BytesParser(policy=default).parsebytes(wheel.read(members[0]))
    except zipfile.BadZipFile as error:
        raise ReleaseValidationError(
            f"{path.name} is not a valid wheel archive"
        ) from error
    names = [str(value) for value in metadata.get_all("Name", [])]
    versions = [str(value) for value in metadata.get_all("Version", [])]
    if len(names) != 1 or len(versions) != 1:
        raise ReleaseValidationError(
            f"{path.name} must declare exactly one Name and Version"
        )
    version = _canonical_version(versions[0], f"{path.name} version")
    if canonicalize_name(names[0]) != filename_name or version != str(filename_version):
        raise ReleaseValidationError(
            f"{path.name} filename and METADATA identities differ"
        )
    expected_metadata = (
        f"{str(filename_name).replace('-', '_')}-{version}.dist-info/METADATA"
    )
    if members[0].filename != expected_metadata:
        raise ReleaseValidationError(f"{path.name} has a mismatched dist-info identity")
    requirements = tuple(
        Requirement(str(value)) for value in metadata.get_all("Requires-Dist", [])
    )
    return WheelRecord(
        path, str(filename_name), version, next(iter(tags)), requirements
    )


def _exact_requirements(record: WheelRecord) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for requirement in record.requirements:
        name = canonicalize_name(requirement.name)
        specifiers = tuple(requirement.specifier)
        if (
            name in resolved
            or requirement.extras
            or requirement.url is not None
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "==="
        ):
            raise ReleaseValidationError(
                f"{record.path.name} must contain only unique exact === requirements"
            )
        resolved[name] = specifiers[0].version
    return resolved


def _require_matrix(
    records: tuple[WheelRecord, ...], provider: Provider, config: ReleaseConfig
) -> str:
    if (
        len(records) != len(config.tags)
        or {record.tag for record in records} != config.tags
    ):
        raise ReleaseValidationError(
            f"{provider.distribution} must contain exactly one wheel per configured tag"
        )
    versions = {record.version for record in records}
    if len(versions) != 1:
        raise ReleaseValidationError(
            f"{provider.distribution} wheels must share one immutable version"
        )
    return next(iter(versions))


def validate_release(
    directory: Path, vane_version: str, config: ReleaseConfig, *, channel: str
) -> dict[str, str]:
    """Require a complete matrix with exact Vane and transitive provider dependencies."""
    validate_vane_version(vane_version, channel)
    records = tuple(
        _read_wheel(path, config) for path in sorted(directory.glob("*.whl"))
    )
    if {record.distribution for record in records} != {
        p.distribution for p in config.providers
    }:
        raise ReleaseValidationError(
            "release directory does not match the configured provider set"
        )
    by_provider = {
        provider.name: tuple(
            record for record in records if record.distribution == provider.distribution
        )
        for provider in config.providers
    }
    versions = {
        provider.name: _require_matrix(by_provider[provider.name], provider, config)
        for provider in config.providers
    }
    for provider in config.providers:
        expected = {"vane-ai": vane_version}
        expected.update(
            {
                config.provider(name).distribution: versions[name]
                for name in config.dependency_closure(provider.name)
            }
        )
        for record in by_provider[provider.name]:
            if _exact_requirements(record) != expected:
                raise ReleaseValidationError(
                    f"{record.path.name} does not exactly require {expected}"
                )
    return versions


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ReleaseValidationError("package-index redirects are not allowed")


def _request_json(url: str) -> tuple[int, object | None]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "vane-provider-release-validator/1"}
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=30) as response:
            data = response.read(MAX_INDEX_BYTES + 1)
            if len(data) > MAX_INDEX_BYTES:
                raise ReleaseValidationError(
                    "package-index JSON exceeds the response size limit"
                )
            return response.status, json.loads(data)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, None
    except (OSError, ValueError) as error:
        raise ReleaseValidationError(
            f"package-index query failed for {url}: {error}"
        ) from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_wheel_hashes(
    directory: Path, provider: Provider, version: str, config: ReleaseConfig
) -> dict[str, str]:
    _canonical_version(version, "provider version")
    records = tuple(
        _read_wheel(path, config)
        for path in sorted(directory.glob("*.whl"))
        if parse_wheel_filename(path.name)[0] == provider.distribution
    )
    if _require_matrix(records, provider, config) != version:
        raise ReleaseValidationError(
            f"{provider.distribution} local version differs from {version}"
        )
    return {record.path.name: _sha256(record.path) for record in records}


def _indexed_wheel_hashes(document: object) -> dict[str, str]:
    if not isinstance(document, dict) or not isinstance(document.get("urls"), list):
        raise ReleaseValidationError("package index returned malformed metadata")
    actual: dict[str, str] = {}
    for item in document["urls"]:
        if not isinstance(item, dict):
            raise ReleaseValidationError("package index returned malformed files")
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (
            item.get("packagetype") != "bdist_wheel"
            or not isinstance(filename, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or item.get("yanked", False) is not False
        ):
            raise ReleaseValidationError(
                "package index returned a non-wheel, yanked or malformed file"
            )
        if filename in actual:
            raise ReleaseValidationError(
                f"package index returned duplicate file {filename}"
            )
        actual[filename] = digest
    if not actual:
        raise ReleaseValidationError(
            "package index returned an existing version without wheels"
        )
    return actual


def _index_base(index: str) -> str:
    try:
        return INDEX_JSON_BASES[index]
    except KeyError:
        raise ReleaseValidationError(f"unknown package index: {index}") from None


def _index_url(provider: Provider, version: str, index: str) -> str:
    return f"{_index_base(index)}/{urllib.parse.quote(provider.distribution, safe='')}/{urllib.parse.quote(version, safe='')}/json"


def _require_publishable(
    expected: dict[str, str], provider: Provider, version: str, index: str
) -> None:
    status, document = _request_json(_index_url(provider, version, index))
    if status == 404:
        return
    if status != 200:
        raise ReleaseValidationError(
            f"expected absent or reusable {index} version {provider.distribution}=={version}, received HTTP {status}"
        )
    actual = _indexed_wheel_hashes(document)
    if any(expected.get(filename) != digest for filename, digest in actual.items()):
        raise ReleaseValidationError(
            f"indexed wheel identities conflict for {provider.distribution}=={version}"
        )


def require_indexes_publishable(
    directory: Path, versions: dict[str, str], config: ReleaseConfig, *, index: str
) -> None:
    """Allow first publication or a byte-identical, possibly partial, rerun."""
    _index_base(index)
    if set(versions) != {provider.name for provider in config.providers}:
        raise ReleaseValidationError(
            "publishable versions must cover every configured provider"
        )
    for provider in config.providers:
        version = versions[provider.name]
        expected = _expected_wheel_hashes(directory, provider, version, config)
        _require_publishable(expected, provider, version, index)


def require_index_match(
    directory: Path,
    provider_name: str,
    version: str,
    config: ReleaseConfig,
    *,
    index: str,
    attempts: int,
    delay_seconds: int,
) -> None:
    """Wait for exactly the local matrix, not just a matching version number."""
    _index_base(index)
    provider = config.provider(provider_name)
    expected = _expected_wheel_hashes(directory, provider, version, config)
    _require_index_match(
        expected,
        provider,
        version,
        index,
        attempts=attempts,
        delay_seconds=delay_seconds,
    )


def _require_index_match(
    expected: dict[str, str],
    provider: Provider,
    version: str,
    index: str,
    *,
    attempts: int,
    delay_seconds: int,
) -> None:
    if attempts <= 0 or delay_seconds < 0:
        raise ReleaseValidationError(
            "index retry settings must be non-negative and include an attempt"
        )
    url = _index_url(provider, version, index)
    last_problem = "release was not indexed"
    for attempt in range(attempts):
        try:
            status, document = _request_json(url)
            if status == 200:
                if _indexed_wheel_hashes(document) == expected:
                    return
                last_problem = f"indexed wheel identities differ for {provider.distribution}=={version}"
            else:
                last_problem = f"{index} returned HTTP {status} for {url}"
        except ReleaseValidationError as error:
            last_problem = str(error)
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise ReleaseValidationError(last_problem)


def verify_promotion(
    directory: Path,
    vane_version: str,
    config: ReleaseConfig,
    *,
    attempts: int,
    delay_seconds: int,
) -> dict[str, str]:
    """Require the complete staged release before accepting an immutable PyPI upload."""
    versions = validate_release(directory, vane_version, config, channel="release")
    expected = {
        provider.name: _expected_wheel_hashes(
            directory, provider, versions[provider.name], config
        )
        for provider in config.providers
    }
    for provider in config.providers:
        _require_index_match(
            expected[provider.name],
            provider,
            versions[provider.name],
            "testpypi",
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
    for provider in config.providers:
        _require_publishable(
            expected[provider.name], provider, versions[provider.name], "pypi"
        )
    # Both indexes refer to one hash snapshot. Detect local replacement/addition
    # during network checks; callers must also keep the files unchanged until upload.
    validate_release(directory, vane_version, config, channel="release")
    for provider in config.providers:
        if (
            _expected_wheel_hashes(directory, provider, versions[provider.name], config)
            != expected[provider.name]
        ):
            raise ReleaseValidationError("local wheels changed during promotion checks")
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate", help="validate a complete development or tagged release set"
    )
    validate.add_argument("--channel", required=True, choices=RELEASE_CHANNELS)
    validate.add_argument("--vane-version", required=True)
    validate.add_argument("--github-output", type=Path)
    validate.add_argument(
        "--require-publishable-on",
        action="append",
        default=[],
        choices=INDEX_JSON_BASES,
    )
    verify = subparsers.add_parser(
        "verify-index", help="compare indexed files with the local provider matrix"
    )
    verify.add_argument("--provider", required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--index", required=True, choices=INDEX_JSON_BASES)
    promotion = subparsers.add_parser(
        "verify-promotion",
        help="verify TestPyPI staging and immutable PyPI publication",
    )
    promotion.add_argument("--vane-version", required=True)
    promotion.add_argument("--github-output", type=Path)
    for command in (verify, promotion):
        command.add_argument("--attempts", default=5, type=int)
        command.add_argument("--delay-seconds", default=15, type=int)
    for command in (validate, verify, promotion):
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--directory", required=True, type=Path)
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--extension-root", required=True, type=Path)
        command.add_argument("--vane-source", required=True, type=Path)
        command.add_argument("--ci-tools-version", required=True)
    arguments = parser.parse_args(argv)
    try:
        if (
            arguments.command == "validate"
            and arguments.channel == "testpypi-dev"
            and "pypi" in arguments.require_publishable_on
        ):
            raise ReleaseValidationError("development candidates cannot target PyPI")
        verify_sources(
            arguments.manifest,
            arguments.extension_root,
            arguments.vane_source,
            arguments.ci_tools_version,
        )
        config = load_config(arguments.config)
        directory = arguments.directory.expanduser().resolve()
        if arguments.command == "validate":
            versions = validate_release(
                directory, arguments.vane_version, config, channel=arguments.channel
            )
            for index in dict.fromkeys(arguments.require_publishable_on):
                require_indexes_publishable(directory, versions, config, index=index)
        elif arguments.command == "verify-promotion":
            versions = verify_promotion(
                directory,
                arguments.vane_version,
                config,
                attempts=arguments.attempts,
                delay_seconds=arguments.delay_seconds,
            )
        else:
            require_index_match(
                directory,
                arguments.provider,
                arguments.version,
                config,
                index=arguments.index,
                attempts=arguments.attempts,
                delay_seconds=arguments.delay_seconds,
            )
            return 0
        if arguments.command in ("validate", "verify-promotion"):
            outputs = {"vane_version": arguments.vane_version}
            outputs.update(
                {f"{name}_version": version for name, version in versions.items()}
            )
            if arguments.github_output is not None:
                with arguments.github_output.open("a", encoding="utf-8") as output:
                    for name, value in outputs.items():
                        output.write(f"{name}={value}\n")
            print(json.dumps(outputs, sort_keys=True))
    except (ReleaseValidationError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
