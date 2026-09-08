# SPDX-FileCopyrightText: 2026 Vane contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts/vane_provider_release.py"
SPEC = importlib.util.spec_from_file_location("vane_provider_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

VANE_VERSION = "0.2.0.dev612"
PROVIDER_VERSION = "0.2.0.0.612.123"
INTERPRETERS = ("cp310", "cp311", "cp312", "cp313", "cp314")
PLATFORM = "manylinux_2_28_x86_64"


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "release.toml"
        self.config_path.write_text(
            'interpreters = ["cp310", "cp311", "cp312", "cp313", "cp314"]\n'
            f'platforms = ["{PLATFORM}"]\n'
            "max_wheel_bytes = 100000000\n"
            "[providers.paimon]\n"
            'distribution = "vane-extension-paimon"\n'
            "dependencies = []\n",
            encoding="utf-8",
        )
        self.config = MODULE.load_config(self.config_path)

    def wheel(
        self,
        interpreter: str,
        *,
        name: str = "paimon",
        version: str = PROVIDER_VERSION,
        platform: str = PLATFORM,
        abi: str = "none",
        build: str = "",
        requirements: tuple[str, ...] = (f"vane-ai==={VANE_VERSION}",),
        extra_metadata: str = "",
        metadata_name: str | None = None,
        metadata_directory: str | None = None,
    ) -> Path:
        distribution = f"vane_extension_{name}"
        path = (
            self.root
            / f"{distribution}-{version}-{build}{interpreter}-{abi}-{platform}.whl"
        )
        metadata = (
            "Metadata-Version: 2.4\n"
            f"Name: {metadata_name or distribution}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {value}\n" for value in requirements)
            + extra_metadata
            + "\n"
        )
        with zipfile.ZipFile(path, "w") as wheel:
            directory = metadata_directory or f"{distribution}-{version}.dist-info"
            wheel.writestr(f"{directory}/METADATA", metadata)
        return path

    def release(self, **kwargs) -> tuple[Path, ...]:
        return tuple(self.wheel(interpreter, **kwargs) for interpreter in INTERPRETERS)

    def validate(self) -> dict[str, str]:
        return MODULE.validate_release(
            self.root, VANE_VERSION, self.config, channel="testpypi-dev"
        )

    def publishable(self) -> None:
        MODULE.require_indexes_publishable(
            self.root, {"paimon": PROVIDER_VERSION}, self.config, index="testpypi"
        )

    def verify(self, *, attempts: int = 1, delay: int = 0) -> None:
        MODULE.require_index_match(
            self.root,
            "paimon",
            PROVIDER_VERSION,
            self.config,
            index="testpypi",
            attempts=attempts,
            delay_seconds=delay,
        )

    @staticmethod
    def indexed(paths: tuple[Path, ...]) -> dict:
        return {
            "urls": [
                {
                    "filename": path.name,
                    "digests": {"sha256": MODULE._sha256(path)},
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                }
                for path in paths
            ]
        }

    def cli_arguments(self) -> list[str]:
        return [
            "validate",
            "--channel",
            "testpypi-dev",
            "--config",
            str(self.config_path),
            "--directory",
            str(self.root),
            "--vane-version",
            VANE_VERSION,
            "--github-output",
            str(self.root / "outputs"),
            "--manifest",
            str(self.root / "vane-extension.toml"),
            "--extension-root",
            str(self.root),
            "--vane-source",
            str(self.root / "vane"),
            "--ci-tools-version",
            "a" * 40,
        ]

    def test_single_provider_and_cli_outputs(self) -> None:
        self.release()
        self.assertEqual(self.validate(), {"paimon": PROVIDER_VERSION})
        output = io.StringIO()
        with mock.patch.object(MODULE, "verify_sources") as verify, redirect_stdout(
            output
        ):
            self.assertEqual(MODULE.main(self.cli_arguments()), 0)
        verify.assert_called_once_with(
            self.root / "vane-extension.toml", self.root, self.root / "vane", "a" * 40
        )
        expected = {"vane_version": VANE_VERSION, "paimon_version": PROVIDER_VERSION}
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(
            dict(
                line.split("=", 1)
                for line in (self.root / "outputs").read_text().splitlines()
            ),
            expected,
        )

    def test_cli_failure_does_not_write_outputs(self) -> None:
        error = io.StringIO()
        with mock.patch.object(MODULE, "verify_sources"), redirect_stderr(error):
            self.assertEqual(MODULE.main(self.cli_arguments()), 2)
        self.assertIn("provider set", error.getvalue())
        self.assertFalse((self.root / "outputs").exists())

    def test_cli_requires_source_inputs(self) -> None:
        for command in ("validate", "verify-index"):
            result = subprocess.run(
                (
                    [
                        sys.executable,
                        "-I",
                        str(SCRIPT),
                        command,
                        "--channel",
                        "testpypi-dev",
                        "--config",
                        str(self.config_path),
                        "--directory",
                        str(self.root),
                        "--vane-version",
                        VANE_VERSION,
                    ]
                    if command == "validate"
                    else [
                        sys.executable,
                        "-I",
                        str(SCRIPT),
                        command,
                        "--index",
                        "testpypi",
                        "--config",
                        str(self.config_path),
                        "--directory",
                        str(self.root),
                        "--provider",
                        "paimon",
                        "--version",
                        PROVIDER_VERSION,
                    ]
                ),
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            for flag in (
                "--manifest",
                "--extension-root",
                "--vane-source",
                "--ci-tools-version",
            ):
                self.assertIn(flag, result.stderr)

    def test_cli_source_failure_precedes_artifacts_index_and_outputs(self) -> None:
        with mock.patch.object(
            MODULE,
            "verify_sources",
            side_effect=MODULE.ReleaseValidationError("dirty source"),
        ), mock.patch.object(MODULE, "validate_release") as validate, mock.patch.object(
            MODULE, "_request_json"
        ) as request, redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(MODULE.main(self.cli_arguments()), 2)
        validate.assert_not_called()
        request.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_source_gate_reuses_exact_native_verifiers(self) -> None:
        native = mock.Mock()
        native.ConfigurationError = RuntimeError
        manifest = native.load_manifest.return_value
        with mock.patch.object(MODULE, "_load_source_tools", return_value=native):
            MODULE.verify_sources(
                self.root / "vane-extension.toml",
                self.root,
                self.root / "vane",
                "a" * 40,
            )
        self.assertEqual(
            native.mock_calls,
            [
                mock.call.load_manifest(self.root / "vane-extension.toml", self.root),
                mock.call.verify_ci_tools_checkout(
                    SCRIPT.resolve().parents[1], "a" * 40
                ),
                mock.call.verify_official_vane_revision(manifest),
                mock.call.verify_vane_checkout(
                    self.root / "vane", manifest, require_complete_history=False
                ),
            ],
        )

    def test_source_gate_propagates_pin_and_checkout_failures(self) -> None:
        native = MODULE._load_source_tools()
        for sha in ("main", "A" * 40, "123"):
            with self.subTest(sha=sha), mock.patch.object(
                MODULE, "_load_source_tools", return_value=native
            ), mock.patch.object(native, "load_manifest"), self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "full lowercase"
            ):
                MODULE.verify_sources(
                    self.root / "manifest", self.root, self.root / "vane", sha
                )
        for step in (
            "verify_ci_tools_checkout",
            "verify_official_vane_revision",
            "verify_vane_checkout",
        ):
            fake = mock.Mock()
            fake.ConfigurationError = native.ConfigurationError
            getattr(fake, step).side_effect = native.ConfigurationError(
                "revision or checkout mismatch"
            )
            with self.subTest(step=step), mock.patch.object(
                MODULE, "_load_source_tools", return_value=fake
            ), self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "revision or checkout mismatch"
            ):
                MODULE.verify_sources(
                    self.root / "manifest", self.root, self.root / "vane", "a" * 40
                )

    def test_multi_provider_graph_uses_complete_transitive_closure(self) -> None:
        with self.config_path.open("a") as output:
            output.write(
                '[providers.avro]\ndistribution = "vane-extension-avro"\ndependencies = []\n'
                '[providers.iceberg]\ndistribution = "vane-extension-iceberg"\ndependencies = ["avro"]\n'
                '[providers.root]\ndistribution = "vane-extension-root"\ndependencies = ["iceberg"]\n'
            )
        self.config = MODULE.load_config(self.config_path)
        self.release()
        avro_version = "0.2.0.0.612.456"
        self.release(name="avro", version=avro_version)
        iceberg_requirements = (
            f"vane-ai==={VANE_VERSION}",
            f"vane-extension-avro==={avro_version}",
        )
        self.release(name="iceberg", requirements=iceberg_requirements)
        self.release(
            name="root",
            requirements=iceberg_requirements
            + (f"vane-extension-iceberg==={PROVIDER_VERSION}",),
        )
        self.assertEqual(self.validate()["avro"], avro_version)
        self.wheel(
            "cp314",
            name="root",
            requirements=(
                f"vane-ai==={VANE_VERSION}",
                f"vane-extension-iceberg==={PROVIDER_VERSION}",
            ),
        )
        with self.assertRaisesRegex(
            MODULE.ReleaseValidationError, "does not exactly require"
        ):
            self.validate()

    def test_multi_platform_matrix(self) -> None:
        second = "manylinux_2_28_aarch64"
        self.config = replace(self.config, platforms=(PLATFORM, second))
        self.release()
        self.release(platform=second)
        self.validate()
        self.wheel("cp314", platform=second).unlink()
        with self.assertRaisesRegex(
            MODULE.ReleaseValidationError, "one wheel per configured tag"
        ):
            self.validate()

    def test_config_rejects_unknown_missing_or_invalid_fields(self) -> None:
        original = self.config_path.read_text()
        for before, after in (
            ("interpreters =", "unknown ="),
            ('"cp310", "cp311"', '"cp310", "cp310"'),
            ('"cp310"', '"py3"'),
            (PLATFORM, "any"),
            ("100000000", "true"),
            ("100000000", "0"),
            ("[providers.paimon]", "[providers.vane]"),
            ("[providers.paimon]", '[providers."bad-name"]'),
            ('"vane-extension-paimon"', '"Vane_Extension_Paimon"'),
            ('"vane-extension-paimon"', '"vane-ai"'),
            ("dependencies = []", 'dependencies = ["missing"]'),
            ("dependencies = []", 'dependencies = ["paimon"]'),
            ("dependencies = []", 'dependencies = ["missing", "missing"]'),
            ("dependencies = []", "dependencies = [{}]"),
            ("dependencies = []", "unrecognized = []"),
        ):
            with self.subTest(after=after):
                self.config_path.write_text(original.replace(before, after))
                with self.assertRaises(MODULE.ReleaseValidationError):
                    MODULE.load_config(self.config_path)

    def test_config_rejects_duplicate_distributions_and_longer_cycles(self) -> None:
        original = self.config_path.read_text()
        extra = '[providers.second]\ndistribution = "vane-extension-paimon"\ndependencies = []\n'
        self.config_path.write_text(original + extra)
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "unique canonical"):
            MODULE.load_config(self.config_path)
        self.config_path.write_text(
            original.replace("dependencies = []", 'dependencies = ["second"]')
            + extra.replace(
                '"vane-extension-paimon"', '"vane-extension-second"'
            ).replace("dependencies = []", 'dependencies = ["paimon"]')
        )
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "cycle"):
            MODULE.load_config(self.config_path)

    def test_vane_version_is_canonical_public_development(self) -> None:
        self.release()
        for version in (
            "0.2.0",
            "v0.2.0.dev612",
            "0.2.0.dev612+local",
            "0.2.0.dev612\nx=y",
        ):
            with self.subTest(version=version), self.assertRaises(
                MODULE.ReleaseValidationError
            ):
                MODULE.validate_release(
                    self.root, version, self.config, channel="testpypi-dev"
                )

    def test_rejects_missing_extra_duplicate_or_wrong_tag_wheels(self) -> None:
        paths = self.release()
        paths[-1].unlink()
        with self.assertRaises(MODULE.ReleaseValidationError):
            self.validate()
        for options in (
            {"name": "unexpected"},
            {"build": "1-"},
            {"abi": "cp314"},
            {"platform": "linux_x86_64"},
            {"interpreter": "cp39"},
            {"interpreter": "cp310.cp314"},
            {"version": "0.2.0.0.612.999"},
        ):
            with self.subTest(options=options):
                options = {"interpreter": "cp314", **options}
                path = self.wheel(**options)
                with self.assertRaises(MODULE.ReleaseValidationError):
                    self.validate()
                path.unlink()

    def test_rejects_inexact_unexpected_or_conditional_requirements(self) -> None:
        self.release()
        for requirements in (
            (),
            ("vane-ai==0.2.0.dev612",),
            ("vane-ai>=0.2",),
            ("vane-ai===0.2.0.dev611",),
            (f"vane-ai==={VANE_VERSION}", "other===1"),
            (f"vane-ai==={VANE_VERSION}", f"VANE_AI==={VANE_VERSION}"),
            (f"vane-ai[extra]==={VANE_VERSION}",),
            (f'vane-ai==={VANE_VERSION}; python_version>="3.10"',),
            ("vane-ai @ https://example.com/wheel.whl",),
        ):
            with self.subTest(requirements=requirements):
                self.wheel("cp314", requirements=requirements)
                with self.assertRaises(MODULE.ReleaseValidationError):
                    self.validate()

    def test_rejects_mismatched_duplicate_or_local_metadata(self) -> None:
        for options in (
            {"metadata_name": "vane-extension-wrong"},
            {"metadata_directory": "wrong-1.dist-info"},
            {"extra_metadata": "Name: duplicate\n"},
            {"extra_metadata": "Version: 1\n"},
            {"version": PROVIDER_VERSION + "+local"},
        ):
            with self.subTest(options=options):
                path = self.wheel("cp310", **options)
                with self.assertRaises(MODULE.ReleaseValidationError):
                    MODULE._read_wheel(path, self.config)

    def test_rejects_equivalent_but_differently_spelled_versions(self) -> None:
        path = self.wheel("cp310", version="1.0")
        renamed = path.with_name(path.name.replace("-1.0-", "-1.0.0-"))
        path.rename(renamed)
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "identities differ"):
            MODULE._read_wheel(renamed, self.config)

    def test_rejects_oversize_wheel_and_metadata(self) -> None:
        path = self.wheel("cp310")
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "max_wheel_bytes"):
            MODULE._read_wheel(
                path, replace(self.config, max_wheel_bytes=path.stat().st_size - 1)
            )
        path = self.wheel(
            "cp310", extra_metadata="X: " + "x" * MODULE.MAX_METADATA_BYTES
        )
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "METADATA exceeds"):
            MODULE._read_wheel(path, self.config)

    def test_rejects_symlink_and_corrupt_archive(self) -> None:
        path = self.wheel("cp310")
        target = path.with_suffix(".backup")
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "regular wheel"):
            MODULE._read_wheel(path, self.config)
        path.unlink()
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(
            MODULE.ReleaseValidationError, "valid wheel archive"
        ):
            MODULE._read_wheel(path, self.config)

    def test_first_partial_and_complete_immutable_reruns(self) -> None:
        paths = self.release()
        for response in (
            (404, None),
            (200, self.indexed(paths[:1])),
            (200, self.indexed(paths)),
        ):
            with mock.patch.object(MODULE, "_request_json", return_value=response):
                self.publishable()
        with mock.patch.object(
            MODULE, "_request_json", return_value=(200, self.indexed(paths))
        ):
            self.verify()

    def test_rejects_conflicting_extra_or_yanked_index_files(self) -> None:
        paths = self.release()
        for change in (
            {"digests": {"sha256": "0" * 64}},
            {"filename": "unexpected.whl"},
            {"yanked": True},
            {"packagetype": "sdist"},
            {"digests": "invalid"},
        ):
            with self.subTest(change=change):
                document = self.indexed(paths)
                document["urls"][0].update(change)
                with mock.patch.object(
                    MODULE, "_request_json", return_value=(200, document)
                ):
                    with self.assertRaises(MODULE.ReleaseValidationError):
                        self.publishable()
                    with self.assertRaises(MODULE.ReleaseValidationError):
                        self.verify()

    def test_rejects_malformed_empty_or_duplicate_index_metadata(self) -> None:
        paths = self.release()
        for document in (
            None,
            {},
            {"urls": []},
            {"urls": [None]},
            self.indexed(paths + paths[:1]),
        ):
            with self.subTest(document=document), self.assertRaises(
                MODULE.ReleaseValidationError
            ):
                MODULE._indexed_wheel_hashes(document)

    def test_index_errors_are_not_absence_and_partial_is_not_complete(self) -> None:
        paths = self.release()
        for response in ((403, None), (429, None), (500, None)):
            with mock.patch.object(MODULE, "_request_json", return_value=response):
                with self.assertRaisesRegex(
                    MODULE.ReleaseValidationError, "received HTTP"
                ):
                    self.publishable()
        with mock.patch.object(
            MODULE, "_request_json", return_value=(200, self.indexed(paths[:1]))
        ):
            with self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "identities differ"
            ):
                self.verify()

    def test_index_retry_is_bounded(self) -> None:
        paths = self.release()
        with mock.patch.object(
            MODULE,
            "_request_json",
            side_effect=[
                MODULE.ReleaseValidationError("network error"),
                (404, None),
                (200, self.indexed(paths)),
            ],
        ) as request, mock.patch.object(MODULE.time, "sleep") as sleep:
            self.verify(attempts=3, delay=2)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(2)])
        with mock.patch.object(
            MODULE, "_request_json", return_value=(404, None)
        ) as request:
            with self.assertRaises(MODULE.ReleaseValidationError):
                self.verify(attempts=2)
        self.assertEqual(request.call_count, 2)
        for attempts, delay in ((0, 0), (1, -1)):
            with self.assertRaises(MODULE.ReleaseValidationError):
                self.verify(attempts=attempts, delay=delay)

    def test_index_checks_validate_local_wheel_identity_before_request(self) -> None:
        self.release()
        self.wheel("cp314", metadata_name="not-paimon")
        with mock.patch.object(MODULE, "_request_json") as request:
            with self.assertRaises(MODULE.ReleaseValidationError):
                self.publishable()
            with self.assertRaises(MODULE.ReleaseValidationError):
                self.verify()
            request.assert_not_called()

    def test_index_requires_expected_provider_version_and_matrix(self) -> None:
        self.release(version="0.2.0.0.612.456")
        with self.assertRaisesRegex(
            MODULE.ReleaseValidationError, "local version differs"
        ):
            self.verify()
        with self.assertRaisesRegex(
            MODULE.ReleaseValidationError, "every configured provider"
        ):
            MODULE.require_indexes_publishable(
                self.root, {}, self.config, index="testpypi"
            )

    def test_http_request_has_timeout_and_response_bound(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"urls": []}'
        response.__enter__.return_value = response
        with mock.patch.object(MODULE.urllib.request, "build_opener") as build:
            request = build.return_value.open
            request.return_value = response
            self.assertEqual(
                MODULE._request_json("https://test.pypi.org/pypi/example/1/json"),
                (200, {"urls": []}),
            )
        self.assertIsInstance(build.call_args.args[0], MODULE._NoRedirect)
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        response.read.assert_called_once_with(MODULE.MAX_INDEX_BYTES + 1)
        response.read.return_value = b"x" * (MODULE.MAX_INDEX_BYTES + 1)
        with mock.patch.object(MODULE.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "response size limit"
            ):
                MODULE._request_json("https://test.pypi.org/pypi/example/1/json")

    def test_release_channels_accept_only_their_vane_versions(self) -> None:
        for version in ("0.2.0.dev612", "0.2.0rc2.dev3"):
            MODULE.validate_vane_version(version, "testpypi-dev")
            with self.assertRaisesRegex(MODULE.ReleaseValidationError, "development"):
                MODULE.validate_vane_version(version, "release")
        for version in ("0.2.0", "0.2.0a1", "0.2.0b1", "0.2.0rc1", "0.2.1.post1"):
            MODULE.validate_vane_version(version, "release")
            with self.assertRaisesRegex(MODULE.ReleaseValidationError, "development"):
                MODULE.validate_vane_version(version, "testpypi-dev")
        for channel in MODULE.RELEASE_CHANNELS:
            for version in ("0.2", "1!0.2.0", "0.2.0+local", "v0.2.0", "0.2.0\nx=y"):
                with self.subTest(channel=channel, version=version), self.assertRaises(
                    MODULE.ReleaseValidationError
                ):
                    MODULE.validate_vane_version(version, channel)
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "unknown release"):
            MODULE.validate_vane_version("0.2.0", "automatic")

    def test_release_matrix_still_requires_the_exact_base_version(self) -> None:
        self.release(requirements=("vane-ai===0.2.0",))
        self.assertEqual(
            MODULE.validate_release(self.root, "0.2.0", self.config, channel="release"),
            {"paimon": PROVIDER_VERSION},
        )
        self.wheel("cp314", requirements=("vane-ai===0.2.1",))
        with self.assertRaisesRegex(MODULE.ReleaseValidationError, "exactly require"):
            MODULE.validate_release(self.root, "0.2.0", self.config, channel="release")

    def test_both_indexes_use_exact_endpoints_and_the_same_immutable_checks(
        self,
    ) -> None:
        paths = self.release()
        provider = self.config.provider("paimon")
        for index, base in MODULE.INDEX_JSON_BASES.items():
            with self.subTest(index=index), mock.patch.object(
                MODULE, "_request_json", return_value=(200, self.indexed(paths))
            ) as request:
                MODULE.require_indexes_publishable(
                    self.root, {"paimon": PROVIDER_VERSION}, self.config, index=index
                )
                MODULE.require_index_match(
                    self.root,
                    "paimon",
                    PROVIDER_VERSION,
                    self.config,
                    index=index,
                    attempts=1,
                    delay_seconds=0,
                )
                expected = f"{base}/vane-extension-paimon/{PROVIDER_VERSION}/json"
                self.assertEqual(request.call_args_list, [mock.call(expected)] * 2)
                self.assertEqual(
                    MODULE._index_url(provider, PROVIDER_VERSION, index), expected
                )
            for status in (301, 302, 403, 429, 500):
                with mock.patch.object(
                    MODULE, "_request_json", return_value=(status, None)
                ):
                    with self.assertRaises(MODULE.ReleaseValidationError):
                        MODULE.require_indexes_publishable(
                            self.root,
                            {"paimon": PROVIDER_VERSION},
                            self.config,
                            index=index,
                        )
                    with self.assertRaises(MODULE.ReleaseValidationError):
                        MODULE.require_index_match(
                            self.root,
                            "paimon",
                            PROVIDER_VERSION,
                            self.config,
                            index=index,
                            attempts=1,
                            delay_seconds=0,
                        )

    def test_unknown_indexes_are_rejected_without_network_access(self) -> None:
        self.release()
        for index in ("auto", "PyPI", "https://pypi.org", "https://example.com"):
            with self.subTest(index=index), mock.patch.object(
                MODULE, "_request_json"
            ) as request:
                with self.assertRaisesRegex(
                    MODULE.ReleaseValidationError, "unknown package index"
                ):
                    MODULE.require_indexes_publishable(
                        self.root,
                        {"paimon": PROVIDER_VERSION},
                        self.config,
                        index=index,
                    )
                with self.assertRaisesRegex(
                    MODULE.ReleaseValidationError, "unknown package index"
                ):
                    MODULE.require_index_match(
                        self.root,
                        "paimon",
                        PROVIDER_VERSION,
                        self.config,
                        index=index,
                        attempts=1,
                        delay_seconds=0,
                    )
                request.assert_not_called()

    def test_http_redirects_never_switch_package_indexes(self) -> None:
        handler = MODULE._NoRedirect()
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "redirects are not allowed"
            ):
                handler.redirect_request(
                    mock.Mock(),
                    mock.Mock(),
                    status,
                    "redirect",
                    {},
                    "https://pypi.org/pypi/example/1/json",
                )

    def test_promotion_checks_complete_staging_before_pypi(self) -> None:
        paths = self.release(requirements=("vane-ai===0.2.0",))
        for published in (
            (404, None),
            (200, self.indexed(paths[:1])),
            (200, self.indexed(paths)),
        ):
            with self.subTest(published=published), mock.patch.object(
                MODULE,
                "_request_json",
                side_effect=[(200, self.indexed(paths)), published],
            ) as request:
                self.assertEqual(
                    MODULE.verify_promotion(
                        self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
                    ),
                    {"paimon": PROVIDER_VERSION},
                )
                self.assertEqual(
                    request.call_args_list,
                    [
                        mock.call(
                            f"https://test.pypi.org/pypi/vane-extension-paimon/{PROVIDER_VERSION}/json"
                        ),
                        mock.call(
                            f"https://pypi.org/pypi/vane-extension-paimon/{PROVIDER_VERSION}/json"
                        ),
                    ],
                )

    def test_promotion_rejects_missing_partial_changed_or_yanked_staging(self) -> None:
        paths = self.release(requirements=("vane-ai===0.2.0",))
        changed = self.indexed(paths)
        changed["urls"][0]["digests"]["sha256"] = "0" * 64
        yanked = self.indexed(paths)
        yanked["urls"][0]["yanked"] = True
        for staged in (
            (404, None),
            (503, None),
            (200, self.indexed(paths[:1])),
            (200, changed),
            (200, yanked),
        ):
            with self.subTest(staged=staged), mock.patch.object(
                MODULE, "_request_json", return_value=staged
            ) as request, self.assertRaises(MODULE.ReleaseValidationError):
                MODULE.verify_promotion(
                    self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
                )
            self.assertEqual(request.call_count, 1)
            self.assertTrue(
                request.call_args.args[0].startswith("https://test.pypi.org/")
            )

    def test_promotion_rejects_conflicting_pypi_files(self) -> None:
        paths = self.release(requirements=("vane-ai===0.2.0",))
        conflict = self.indexed(paths)
        conflict["urls"][0]["digests"]["sha256"] = "0" * 64
        with mock.patch.object(
            MODULE,
            "_request_json",
            side_effect=[(200, self.indexed(paths)), (200, conflict)],
        ), self.assertRaisesRegex(MODULE.ReleaseValidationError, "conflict"):
            MODULE.verify_promotion(
                self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
            )

    def test_promotion_uses_one_snapshot_and_rejects_local_changes(self) -> None:
        for change in ("replace", "add", "remove", "symlink"):
            with self.subTest(change=change):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                self.root = Path(temporary.name)
                paths = self.release(requirements=("vane-ai===0.2.0",))
                staged = self.indexed(paths)

                def respond(url):
                    if url.startswith("https://test.pypi.org/"):
                        return 200, staged
                    if change == "replace":
                        self.wheel(
                            "cp314",
                            requirements=("vane-ai===0.2.0",),
                            extra_metadata="X-Changed: true\n",
                        )
                    elif change == "add":
                        self.wheel(
                            "cp314",
                            name="unexpected",
                            requirements=("vane-ai===0.2.0",),
                        )
                    elif change == "remove":
                        paths[-1].unlink()
                    else:
                        target = paths[-1].with_suffix(".saved")
                        paths[-1].rename(target)
                        paths[-1].symlink_to(target)
                    return 404, None

                with mock.patch.object(
                    MODULE, "_request_json", side_effect=respond
                ), self.assertRaises(MODULE.ReleaseValidationError):
                    MODULE.verify_promotion(
                        self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
                    )

    def test_promotion_requires_all_providers_staged_before_checking_any_pypi(
        self,
    ) -> None:
        self.config = replace(
            self.config,
            providers=(
                MODULE.Provider("avro", "vane-extension-avro", ()),
                MODULE.Provider("iceberg", "vane-extension-iceberg", ("avro",)),
            ),
        )
        avro_version = "0.2.0.456"
        avro = self.release(
            name="avro", version=avro_version, requirements=("vane-ai===0.2.0",)
        )
        iceberg = self.release(
            name="iceberg",
            requirements=("vane-ai===0.2.0", f"vane-extension-avro==={avro_version}"),
        )
        for staged_iceberg in (iceberg[:1], iceberg):
            with mock.patch.object(
                MODULE,
                "_request_json",
                side_effect=[
                    (200, self.indexed(avro)),
                    (200, self.indexed(staged_iceberg)),
                    (404, None),
                    (404, None),
                ],
            ) as request:
                if staged_iceberg == iceberg:
                    self.assertEqual(
                        MODULE.verify_promotion(
                            self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
                        ),
                        {"avro": avro_version, "iceberg": PROVIDER_VERSION},
                    )
                else:
                    with self.assertRaises(MODULE.ReleaseValidationError):
                        MODULE.verify_promotion(
                            self.root, "0.2.0", self.config, attempts=1, delay_seconds=0
                        )
                expected_calls = [
                    mock.call(
                        f"https://test.pypi.org/pypi/vane-extension-avro/{avro_version}/json"
                    ),
                    mock.call(
                        f"https://test.pypi.org/pypi/vane-extension-iceberg/{PROVIDER_VERSION}/json"
                    ),
                ]
                if staged_iceberg == iceberg:
                    expected_calls += [
                        mock.call(
                            f"https://pypi.org/pypi/vane-extension-avro/{avro_version}/json"
                        ),
                        mock.call(
                            f"https://pypi.org/pypi/vane-extension-iceberg/{PROVIDER_VERSION}/json"
                        ),
                    ]
                self.assertEqual(request.call_args_list, expected_calls)

    def test_promotion_rejects_dev_and_wrong_base_dependencies_before_network(
        self,
    ) -> None:
        self.release()
        with mock.patch.object(MODULE, "_request_json") as request:
            for version in (VANE_VERSION, "0.2.0"):
                with self.subTest(version=version), self.assertRaises(
                    MODULE.ReleaseValidationError
                ):
                    MODULE.verify_promotion(
                        self.root, version, self.config, attempts=1, delay_seconds=0
                    )
            request.assert_not_called()

    def test_cli_dev_candidates_cannot_target_pypi(self) -> None:
        self.release()
        arguments = self.cli_arguments() + ["--require-publishable-on", "pypi"]
        with mock.patch.object(MODULE, "verify_sources") as sources, mock.patch.object(
            MODULE, "_request_json"
        ) as request, redirect_stderr(io.StringIO()):
            self.assertEqual(MODULE.main(arguments), 2)
        sources.assert_not_called()
        request.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_cli_validates_both_indexes_for_a_release(self) -> None:
        self.release(requirements=("vane-ai===0.2.0",))
        arguments = self.cli_arguments()
        arguments[arguments.index("--channel") + 1] = "release"
        arguments[arguments.index("--vane-version") + 1] = "0.2.0"
        arguments += [
            "--require-publishable-on",
            "testpypi",
            "--require-publishable-on",
            "pypi",
        ]
        with mock.patch.object(MODULE, "verify_sources"), mock.patch.object(
            MODULE, "_request_json", return_value=(404, None)
        ) as request, redirect_stdout(io.StringIO()):
            self.assertEqual(MODULE.main(arguments), 0)
        self.assertEqual(request.call_count, 2)
        self.assertTrue((self.root / "outputs").exists())

    def test_cli_verify_index_selects_the_explicit_index(self) -> None:
        paths = self.release()
        arguments = self.cli_arguments()
        arguments[0] = "verify-index"
        for option in ("--channel", "--vane-version", "--github-output"):
            position = arguments.index(option)
            del arguments[position : position + 2]
        arguments += [
            "--provider",
            "paimon",
            "--version",
            PROVIDER_VERSION,
            "--attempts",
            "1",
        ]
        for index, base in MODULE.INDEX_JSON_BASES.items():
            with mock.patch.object(
                MODULE, "verify_sources"
            ) as sources, mock.patch.object(
                MODULE, "_request_json", return_value=(200, self.indexed(paths))
            ) as request:
                self.assertEqual(MODULE.main(arguments + ["--index", index]), 0)
            sources.assert_called_once()
            request.assert_called_once_with(
                f"{base}/vane-extension-paimon/{PROVIDER_VERSION}/json"
            )
        self.assertFalse((self.root / "outputs").exists())

    def test_cli_requires_explicit_channel_index_and_promotion_sources(self) -> None:
        validate = self.cli_arguments()
        channel_position = validate.index("--channel")
        del validate[channel_position : channel_position + 2]
        for arguments, required in (
            (validate, ("--channel",)),
            (["verify-index"], ("--index",)),
            (
                ["verify-promotion"],
                (
                    "--vane-version",
                    "--manifest",
                    "--extension-root",
                    "--vane-source",
                    "--ci-tools-version",
                ),
            ),
        ):
            error = io.StringIO()
            with redirect_stderr(error), self.assertRaises(SystemExit) as result:
                MODULE.main(arguments)
            self.assertEqual(result.exception.code, 2)
            for flag in required:
                self.assertIn(flag, error.getvalue())

    def test_cli_promotion_writes_outputs_only_after_all_gates(self) -> None:
        paths = self.release(requirements=("vane-ai===0.2.0",))
        arguments = self.cli_arguments()
        arguments[0] = "verify-promotion"
        channel_position = arguments.index("--channel")
        del arguments[channel_position : channel_position + 2]
        arguments[arguments.index("--vane-version") + 1] = "0.2.0"
        arguments += ["--attempts", "1", "--delay-seconds", "0"]
        with mock.patch.object(MODULE, "verify_sources"), mock.patch.object(
            MODULE, "_request_json", return_value=(404, None)
        ), redirect_stderr(io.StringIO()):
            self.assertEqual(MODULE.main(arguments), 2)
        self.assertFalse((self.root / "outputs").exists())
        with mock.patch.object(MODULE, "verify_sources"), mock.patch.object(
            MODULE,
            "_request_json",
            side_effect=[(200, self.indexed(paths)), (404, None)],
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(MODULE.main(arguments), 0)
        self.assertIn("vane_version=0.2.0\n", (self.root / "outputs").read_text())


if __name__ == "__main__":
    unittest.main()
