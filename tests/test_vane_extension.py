from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self,
        revision: str = "a" * 40,
        config: str = "extension_config.cmake",
        repository: str = "AstroVela/vane",
        native_test_selection: str = "test/",
    ) -> Path:
        path = self.root / "vane-extension.toml"
        path.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'name = "iceberg"',
                    f'extension_config = "{config}"',
                    'build_extensions = ["httpfs", "parquet", "tpch"]',
                    f'native_test_selection = "{native_test_selection}"',
                    "",
                    "[vane]",
                    f'repository = "{repository}"',
                    f'revision = "{revision}"',
                    "",
                ]
            )
        )
        return path

    def test_loads_strict_manifest(self) -> None:
        manifest = MODULE.load_manifest(self.write_manifest(), self.root)
        self.assertEqual(manifest.name, "iceberg")
        self.assertEqual(manifest.build_extensions_cmake, "httpfs;parquet;tpch")
        self.assertEqual(manifest.vane_repository, "AstroVela/vane")

    def test_rejects_non_exact_vane_revision(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "full lowercase"):
            MODULE.load_manifest(self.write_manifest("main"), self.root)

    def test_rejects_template_vane_revision(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "placeholder"):
            MODULE.load_manifest(self.write_manifest("0" * 40), self.root)

    def test_rejects_boolean_schema_version(self) -> None:
        path = self.write_manifest()
        path.write_text(
            path.read_text().replace("schema_version = 1", "schema_version = true")
        )
        with self.assertRaisesRegex(MODULE.ConfigurationError, "schema_version"):
            MODULE.load_manifest(path, self.root)

    def test_rejects_noncanonical_vane_repository(self) -> None:
        with self.assertRaisesRegex(
            MODULE.ConfigurationError, "must be AstroVela/vane"
        ):
            MODULE.load_manifest(
                self.write_manifest(repository="someone-else/vane"), self.root
            )

    def test_rejects_extension_config_outside_root(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "escapes"):
            MODULE.load_manifest(
                self.write_manifest(config="../outside.cmake"), self.root
            )

    def test_rejects_manifest_outside_root(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.toml"
        outside.write_text(self.write_manifest().read_text())
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(MODULE.ConfigurationError, "manifest escapes"):
            MODULE.load_manifest(outside, self.root)

    def test_rejects_native_test_command_line_option(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "command-line option"):
            MODULE.load_manifest(
                self.write_manifest(native_test_selection="--list-tests"), self.root
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
                        'build_extensions = ["httpfs", "parquet"]',
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


class VaneCheckoutTests(unittest.TestCase):
    @staticmethod
    def git(directory: Path, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=capture,
            text=True,
        )
        return result.stdout.strip() if capture else ""

    def create_vane_origin(self, root: Path) -> tuple[Path, str]:
        checkout = root / "vane-checkout"
        checkout.mkdir()
        self.git(checkout, "init")
        self.git(checkout, "config", "user.email", "fixture@example.com")
        self.git(checkout, "config", "user.name", "Fixture")
        (checkout / "external/duckdb").mkdir(parents=True)
        (checkout / "external/duckdb/CMakeLists.txt").write_text("# fixture\n")
        (checkout / "scripts").mkdir()
        (checkout / "scripts/sync_duckdb_source_id.py").write_text(
            'print("' + "b" * 40 + '")\n'
        )
        (checkout / "scripts/resolve_duckdb_fork_version.py").write_text(
            'print("v1.5.0-vane.aaaaaaaaaa")\n'
        )
        (checkout / "DUCKDB_UPSTREAM_VERSION").write_text("v1.5.0\n")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-m", "add duckdb fork")
        (checkout / "README.md").write_text("fixture\n")
        self.git(checkout, "add", "README.md")
        self.git(checkout, "commit", "-m", "add README")
        revision = self.git(checkout, "rev-parse", "HEAD", capture=True)

        origin = root / "vane-origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(checkout), str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        return origin, revision

    @staticmethod
    def manifest(revision: str) -> object:
        return MODULE.ExtensionManifest(
            schema_version=1,
            name="iceberg",
            extension_config="extension_config.cmake",
            build_extensions=("httpfs",),
            native_test_selection="test/",
            vane_repository="AstroVela/vane",
            vane_revision=revision,
        )

    def test_prepare_fetches_complete_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            with mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertEqual(
                self.git(
                    vane_source,
                    "rev-parse",
                    "--is-shallow-repository",
                    capture=True,
                ),
                "false",
            )
            self.assertEqual(
                self.git(vane_source, "rev-list", "--count", "HEAD", capture=True),
                "2",
            )
            self.assertEqual(
                self.git(vane_source, "rev-parse", "HEAD", capture=True), revision
            )

    def test_prepare_unshallows_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            subprocess.run(
                ["git", "clone", "--depth", "1", origin.as_uri(), str(vane_source)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                self.git(
                    vane_source,
                    "rev-parse",
                    "--is-shallow-repository",
                    capture=True,
                ),
                "true",
            )

            with mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertEqual(
                self.git(
                    vane_source,
                    "rev-parse",
                    "--is-shallow-repository",
                    capture=True,
                ),
                "false",
            )

    def test_prepare_verifies_existing_checkout_against_official_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            subprocess.run(
                ["git", "clone", origin.as_uri(), str(vane_source)],
                check=True,
                capture_output=True,
                text=True,
            )

            with mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertEqual(
                self.git(vane_source, "rev-parse", "HEAD", capture=True),
                revision,
            )

    def test_prepare_rejects_fork_only_existing_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            official_origin, _ = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            subprocess.run(
                ["git", "clone", official_origin.as_uri(), str(vane_source)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.git(vane_source, "config", "user.email", "fixture@example.com")
            self.git(vane_source, "config", "user.name", "Fixture")
            (vane_source / "FORK_ONLY.md").write_text("fork-only\n")
            self.git(vane_source, "add", "FORK_ONLY.md")
            self.git(vane_source, "commit", "-m", "fork-only commit")
            fork_revision = self.git(vane_source, "rev-parse", "HEAD", capture=True)

            with mock.patch.object(
                MODULE, "VANE_REPOSITORY_URL", official_origin.as_uri()
            ):
                with self.assertRaisesRegex(
                    MODULE.ConfigurationError, "not available from AstroVela/vane"
                ):
                    MODULE.prepare_vane(vane_source, self.manifest(fork_revision))


class CIToolsCheckoutTests(unittest.TestCase):
    @staticmethod
    def git(directory: Path, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=capture,
            text=True,
        )
        return result.stdout.strip() if capture else ""

    def create_ci_tools_origin(self, root: Path) -> tuple[Path, Path, str]:
        checkout = root / "ci-tools-checkout"
        checkout.mkdir()
        self.git(checkout, "init")
        self.git(checkout, "config", "user.email", "fixture@example.com")
        self.git(checkout, "config", "user.name", "Fixture")
        (checkout / "scripts").mkdir()
        (checkout / "scripts/vane_extension.py").write_text("# fixture\n")
        self.git(checkout, "add", ".")
        self.git(checkout, "commit", "-m", "add CI tools")
        revision = self.git(checkout, "rev-parse", "HEAD", capture=True)

        origin = root / "ci-tools-origin.git"
        subprocess.run(
            ["git", "clone", "--bare", str(checkout), str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        return checkout, origin, revision

    def test_accepts_clean_expected_official_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout, origin, revision = self.create_ci_tools_origin(
                Path(temporary_directory)
            )
            with mock.patch.object(MODULE, "CI_TOOLS_REPOSITORY_URL", origin.as_uri()):
                MODULE.verify_ci_tools_checkout(checkout, revision)

    def test_rejects_mismatched_ci_tools_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout, _, _ = self.create_ci_tools_origin(Path(temporary_directory))
            with self.assertRaisesRegex(MODULE.ConfigurationError, "revision mismatch"):
                MODULE.verify_ci_tools_checkout(checkout, "b" * 40)

    def test_rejects_dirty_ci_tools_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout, _, revision = self.create_ci_tools_origin(
                Path(temporary_directory)
            )
            (checkout / "scripts/vane_extension.py").write_text("# dirty\n")
            with self.assertRaisesRegex(MODULE.ConfigurationError, "working-tree"):
                MODULE.verify_ci_tools_checkout(checkout, revision)


class ReusableWorkflowIdentityTests(unittest.TestCase):
    @staticmethod
    def token(claims: dict[str, object]) -> str:
        def encode(value: object) -> str:
            return (
                base64.urlsafe_b64encode(json.dumps(value).encode())
                .rstrip(b"=")
                .decode()
            )

        return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"

    def test_accepts_matching_reusable_workflow_identity(self) -> None:
        revision = "a" * 40
        MODULE.verify_reusable_workflow_identity(
            self.token(
                {
                    "aud": MODULE.OIDC_AUDIENCE,
                    "job_workflow_ref": (
                        "AstroVela/vane-extension-ci-tools/"
                        ".github/workflows/_vane_extension_ci.yml@" + revision
                    ),
                    "job_workflow_sha": revision,
                }
            ),
            revision,
        )

    def test_rejects_non_pinned_reusable_workflow_ref(self) -> None:
        revision = "a" * 40
        with self.assertRaisesRegex(MODULE.ConfigurationError, "workflow ref mismatch"):
            MODULE.verify_reusable_workflow_identity(
                self.token(
                    {
                        "aud": MODULE.OIDC_AUDIENCE,
                        "job_workflow_ref": (
                            "AstroVela/vane-extension-ci-tools/"
                            ".github/workflows/_vane_extension_ci.yml@refs/heads/main"
                        ),
                        "job_workflow_sha": revision,
                    }
                ),
                revision,
            )

    def test_rejects_mismatched_reusable_workflow_sha(self) -> None:
        revision = "a" * 40
        with self.assertRaisesRegex(MODULE.ConfigurationError, "workflow SHA mismatch"):
            MODULE.verify_reusable_workflow_identity(
                self.token(
                    {
                        "aud": MODULE.OIDC_AUDIENCE,
                        "job_workflow_ref": (
                            "AstroVela/vane-extension-ci-tools/"
                            ".github/workflows/_vane_extension_ci.yml@" + revision
                        ),
                        "job_workflow_sha": "b" * 40,
                    }
                ),
                revision,
            )

    def test_rejects_mismatched_oidc_audience(self) -> None:
        revision = "a" * 40
        with self.assertRaisesRegex(MODULE.ConfigurationError, "audience mismatch"):
            MODULE.verify_reusable_workflow_identity(
                self.token(
                    {
                        "aud": "different-audience",
                        "job_workflow_ref": (
                            "AstroVela/vane-extension-ci-tools/"
                            ".github/workflows/_vane_extension_ci.yml@" + revision
                        ),
                        "job_workflow_sha": revision,
                    }
                ),
                revision,
            )


class WorkflowContractTests(unittest.TestCase):
    def test_reusable_workflow_example_grants_caller_oidc_permission(self) -> None:
        readme = (Path(__file__).parents[1] / "README.md").read_text()
        self.assertIn("id-token: write", readme)
        self.assertIn(
            "Reusable workflows cannot elevate this permission from the caller.",
            readme,
        )

    def test_oidc_permission_is_isolated_from_native_build(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/_vane_extension_ci.yml"
        ).read_text()
        verify_job, native_job = workflow.split("  native:\n", 1)
        self.assertIn("  verify_workflow:\n", verify_job)
        self.assertIn("id-token: write", verify_job)
        self.assertIn("needs: verify_workflow", native_job)
        self.assertNotIn("id-token: write", native_job)

    def test_ci_tools_input_is_not_interpolated_into_shell(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/_vane_extension_ci.yml"
        ).read_text()
        self.assertIn("VANE_CI_TOOLS_VERSION: ${{ inputs.ci_tools_version }}", workflow)
        self.assertNotIn('"${{ inputs.ci_tools_version }}"', workflow)

    def test_local_targets_verify_committed_ci_tools_gitlink(self) -> None:
        makefile = (
            Path(__file__).parents[1] / "makefiles/vane_extension.Makefile"
        ).read_text()
        self.assertIn('rev-parse "HEAD:vane-extension-ci-tools"', makefile)
        self.assertIn("vane_validate: vane_verify_ci_tools", makefile)

    def test_make_passes_committed_ci_tools_gitlink_as_expected_sha(self) -> None:
        expected_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            extension_root = Path(temporary_directory)
            subprocess.run(["git", "init"], cwd=extension_root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=extension_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"],
                cwd=extension_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{expected_sha},vane-extension-ci-tools",
                ],
                cwd=extension_root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "pin CI tools"],
                cwd=extension_root,
                check=True,
                capture_output=True,
            )

            makefile = Path(__file__).parents[1] / "makefiles/vane_extension.Makefile"
            result = subprocess.run(
                [
                    "make",
                    "--just-print",
                    "--file",
                    str(makefile),
                    "vane_verify_ci_tools",
                    f"VANE_EXTENSION_ROOT={extension_root}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(f'--expected-sha "{expected_sha}"', result.stdout)


class NativeTestOutputTests(unittest.TestCase):
    def test_accepts_executed_test_output(self) -> None:
        MODULE.verify_selected_test_output("All tests passed (144 assertions)\n")

    def test_rejects_all_skipped_test_output(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "all test cases"):
            MODULE.verify_selected_test_output(
                "All tests were skipped (total skipped 1)\n"
            )

    def test_rejects_no_matching_test_output(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigurationError, "no test cases"):
            MODULE.verify_selected_test_output(
                "No test cases matched 'missing/'\nNo tests ran\n"
            )


if __name__ == "__main__":
    unittest.main()
