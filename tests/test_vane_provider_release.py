# SPDX-FileCopyrightText: 2026 Vane contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
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
        return MODULE.validate_release(self.root, VANE_VERSION, self.config)

    def publishable(self) -> None:
        MODULE.require_indexes_publishable(
            self.root, {"paimon": PROVIDER_VERSION}, self.config
        )

    def verify(self, *, attempts: int = 1, delay: int = 0) -> None:
        MODULE.require_index_match(
            self.root,
            "paimon",
            PROVIDER_VERSION,
            self.config,
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

    def test_single_provider_and_cli_outputs(self) -> None:
        self.release()
        self.assertEqual(self.validate(), {"paimon": PROVIDER_VERSION})
        outputs = self.root / "outputs"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "validate",
                "--config",
                str(self.config_path),
                "--directory",
                str(self.root),
                "--vane-version",
                VANE_VERSION,
                "--github-output",
                str(outputs),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        expected = {"vane_version": VANE_VERSION, "paimon_version": PROVIDER_VERSION}
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(
            dict(line.split("=", 1) for line in outputs.read_text().splitlines()),
            expected,
        )

    def test_cli_failure_does_not_write_outputs(self) -> None:
        outputs = self.root / "outputs"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "validate",
                "--config",
                str(self.config_path),
                "--directory",
                str(self.root),
                "--vane-version",
                VANE_VERSION,
                "--github-output",
                str(outputs),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("provider set", result.stderr)
        self.assertFalse(outputs.exists())

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
                MODULE.validate_release(self.root, version, self.config)

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
            MODULE.require_indexes_publishable(self.root, {}, self.config)

    def test_http_request_has_timeout_and_response_bound(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"urls": []}'
        response.__enter__.return_value = response
        with mock.patch.object(
            MODULE.urllib.request, "urlopen", return_value=response
        ) as request:
            self.assertEqual(
                MODULE._request_json("https://test.pypi.org/pypi/example/1/json"),
                (200, {"urls": []}),
            )
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        response.read.assert_called_once_with(MODULE.MAX_INDEX_BYTES + 1)
        response.read.return_value = b"x" * (MODULE.MAX_INDEX_BYTES + 1)
        with mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(
                MODULE.ReleaseValidationError, "response size limit"
            ):
                MODULE._request_json("https://test.pypi.org/pypi/example/1/json")


if __name__ == "__main__":
    unittest.main()
