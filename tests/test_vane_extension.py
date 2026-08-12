from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/vane_extension.py"
SPEC = importlib.util.spec_from_file_location("vane_extension", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "extension_config.cmake").write_text("# fixture\n")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_manifest(
        self, revision: str = "a" * 40, config: str = "extension_config.cmake"
    ) -> Path:
        path = self.root / "vane-extension.toml"
        path.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'name = "iceberg"',
                    f'extension_config = "{config}"',
                    'core_extensions = ["httpfs", "parquet", "tpch"]',
                    'native_test_selection = "test/"',
                    "",
                    "[vane]",
                    'repository = "AstroVela/vane"',
                    f'revision = "{revision}"',
                    "",
                ]
            )
        )
        return path

    def test_loads_strict_manifest(self) -> None:
        manifest = MODULE.load_manifest(self.write_manifest(), self.root)
        self.assertEqual(manifest.name, "iceberg")
        self.assertEqual(manifest.core_extensions_cmake, "httpfs;parquet;tpch")
        self.assertEqual(manifest.vane_repository, "AstroVela/vane")

    def test_rejects_non_exact_vane_revision(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "full lowercase"):
            MODULE.load_manifest(self.write_manifest("main"), self.root)

    def test_rejects_template_vane_revision(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "placeholder"):
            MODULE.load_manifest(self.write_manifest("0" * 40), self.root)

    def test_rejects_extension_config_outside_root(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "escapes"):
            MODULE.load_manifest(
                self.write_manifest(config="../outside.cmake"), self.root
            )


class IdentityTests(unittest.TestCase):
    def test_resolves_identity_from_exact_vane_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            extension_root = root / "extension"
            extension_root.mkdir()
            (extension_root / "extension_config.cmake").write_text("# fixture\n")

            vane_root = root / "vane"
            (vane_root / "external/duckdb").mkdir(parents=True)
            (vane_root / "external/duckdb/CMakeLists.txt").write_text("# fixture\n")
            (vane_root / "scripts").mkdir()
            (vane_root / "scripts/sync_duckdb_source_id.py").write_text(
                'print("' + "b" * 40 + '")\n'
            )
            (vane_root / "scripts/resolve_duckdb_fork_version.py").write_text(
                'print("v1.5.0-vane.aaaaaaaaaa")\n'
            )
            (vane_root / "DUCKDB_UPSTREAM_VERSION").write_text("v1.5.0\n")
            subprocess.run(
                ["git", "init"], cwd=vane_root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=vane_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=vane_root, check=True
            )
            subprocess.run(["git", "add", "."], cwd=vane_root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=vane_root,
                check=True,
                capture_output=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=vane_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest_path = extension_root / "vane-extension.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        "schema_version = 1",
                        'name = "iceberg"',
                        'extension_config = "extension_config.cmake"',
                        'core_extensions = ["httpfs", "parquet"]',
                        'native_test_selection = "test/"',
                        "",
                        "[vane]",
                        'repository = "AstroVela/vane"',
                        f'revision = "{revision}"',
                    ]
                )
            )

            manifest = MODULE.load_manifest(manifest_path, extension_root)
            identity = MODULE.resolve_vane_identity(vane_root, manifest)
            self.assertEqual(identity.source_id, "b" * 40)
            self.assertEqual(identity.fork_version, "v1.5.0-vane.aaaaaaaaaa")
            self.assertEqual(identity.upstream_version, "v1.5.0")
            self.assertEqual(
                json.loads(json.dumps(MODULE.asdict(identity)))["source_id"], "b" * 40
            )

    def test_accepts_sha256_duckdb_source_id(self) -> None:
        self.assertIsNotNone(MODULE.SOURCE_ID_RE.fullmatch("c" * 64))


class NativeTestOutputTests(unittest.TestCase):
    def test_accepts_executed_test_output(self) -> None:
        MODULE.verify_selected_test_output("All tests passed (144 assertions)\n")

    def test_rejects_all_skipped_test_output(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "all test cases"):
            MODULE.verify_selected_test_output(
                "All tests were skipped (total skipped 1)\n"
            )


if __name__ == "__main__":
    unittest.main()
