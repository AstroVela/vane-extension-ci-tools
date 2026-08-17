from __future__ import annotations

import base64
import importlib.util
import json
import os
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

    def test_rejects_target_in_supporting_build_extensions(self) -> None:
        path = self.write_manifest()
        path.write_text(
            path.read_text().replace(
                '["httpfs", "parquet", "tpch"]', '["httpfs", "iceberg"]'
            )
        )
        with self.assertRaisesRegex(
            MODULE.ConfigurationError, "must not contain the target extension"
        ):
            MODULE.load_manifest(path, self.root)


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
        first_revision = self.git(checkout, "rev-parse", "HEAD", capture=True)
        (checkout / "README.md").write_text("fixture\n")
        self.git(checkout, "add", "README.md")
        self.git(checkout, "commit", "-m", "add README")
        self.git(checkout, "tag", "v1.5.0", first_revision)
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
            self.assertEqual(
                self.git(
                    vane_source,
                    "rev-parse",
                    "v1.5.0^{commit}",
                    capture=True,
                ),
                self.git(vane_source, "rev-parse", "HEAD^", capture=True),
            )

    def test_prepare_rejects_non_official_local_vane_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            with mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.git(vane_source, "tag", "v9.9.9")
            with (
                mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()),
                self.assertRaisesRegex(
                    MODULE.ConfigurationError, "tag refs do not exactly match"
                ),
            ):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

    def test_prepare_failure_leaves_no_checkout_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            original_run = MODULE.run

            def fail_fetch(
                command: list[str],
                *,
                cwd: Path | None = None,
                capture: bool = False,
            ) -> str:
                if command[:3] == ["git", "fetch", "origin"]:
                    raise subprocess.CalledProcessError(1, command)
                return original_run(command, cwd=cwd, capture=capture)

            with (
                mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()),
                mock.patch.object(MODULE, "run", side_effect=fail_fetch),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertFalse(os.path.lexists(vane_source))
            self.assertEqual(list(root.glob(".vane-source.prepare-*")), [])

            with mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertEqual(
                self.git(vane_source, "rev-parse", "HEAD", capture=True), revision
            )

    def test_prepare_does_not_replace_dangling_destination_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            vane_source = root / "vane-source"
            vane_source.symlink_to(root / "missing-source", target_is_directory=True)

            with self.assertRaisesRegex(
                MODULE.ConfigurationError, "existing Vane source is not a Git checkout"
            ):
                MODULE.prepare_vane(vane_source, self.manifest("a" * 40))

            self.assertTrue(vane_source.is_symlink())

    def test_prepare_does_not_replace_destination_created_during_preparation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            origin, revision = self.create_vane_origin(root)
            vane_source = root / "vane-source"
            original_verify = MODULE.verify_vane_checkout

            def create_destination_after_verify(
                checkout: Path,
                manifest: object,
                *,
                require_complete_history: bool = True,
            ) -> None:
                original_verify(
                    checkout,
                    manifest,
                    require_complete_history=require_complete_history,
                )
                vane_source.mkdir()

            with (
                mock.patch.object(MODULE, "VANE_REPOSITORY_URL", origin.as_uri()),
                mock.patch.object(
                    MODULE,
                    "verify_vane_checkout",
                    side_effect=create_destination_after_verify,
                ),
                self.assertRaisesRegex(
                    MODULE.ConfigurationError, "appeared during preparation"
                ),
            ):
                MODULE.prepare_vane(vane_source, self.manifest(revision))

            self.assertTrue(vane_source.is_dir())
            self.assertEqual(list(vane_source.iterdir()), [])
            self.assertEqual(list(root.glob(".vane-source.prepare-*")), [])

    def test_publish_uses_syscall_without_a_libc_renameat2_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            staged_source = root / "staged-source"
            staged_source.mkdir()
            (staged_source / "marker").write_text("verified\n")
            vane_source = root / "vane-source"
            syscall_only_libc = mock.Mock(
                spec_set=["syscall"],
                syscall=MODULE.ctypes.CDLL(None, use_errno=True).syscall,
            )

            with mock.patch.object(
                MODULE.ctypes, "CDLL", return_value=syscall_only_libc
            ):
                MODULE.publish_vane_checkout(staged_source, vane_source)

            self.assertFalse(os.path.lexists(staged_source))
            self.assertEqual((vane_source / "marker").read_text(), "verified\n")

    def test_publish_has_no_non_atomic_fallback_when_syscall_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            staged_source = root / "staged-source"
            staged_source.mkdir()
            (staged_source / "marker").write_text("verified\n")
            vane_source = root / "vane-source"
            syscall = mock.Mock(return_value=-1)
            syscall_only_libc = mock.Mock(spec_set=["syscall"], syscall=syscall)

            with (
                mock.patch.object(
                    MODULE.ctypes, "CDLL", return_value=syscall_only_libc
                ),
                mock.patch.object(
                    MODULE.ctypes,
                    "get_errno",
                    return_value=MODULE.errno.ENOSYS,
                ),
                self.assertRaisesRegex(
                    MODULE.ConfigurationError, "requires Linux renameat2 support"
                ),
            ):
                MODULE.publish_vane_checkout(staged_source, vane_source)

            self.assertTrue((staged_source / "marker").is_file())
            self.assertFalse(os.path.lexists(vane_source))

    def test_publish_rejects_non_x86_64_linux(self) -> None:
        staged_source = Path("staged-source")
        vane_source = Path("vane-source")

        with (
            mock.patch.object(MODULE.sys, "platform", "linux"),
            mock.patch.object(
                MODULE.os,
                "uname",
                return_value=mock.Mock(machine="aarch64"),
            ),
            mock.patch.object(MODULE.ctypes, "CDLL") as load_libc,
            self.assertRaisesRegex(
                MODULE.ConfigurationError, "requires 64-bit x86 Linux"
            ),
        ):
            MODULE.publish_vane_checkout(staged_source, vane_source)

        load_libc.assert_not_called()

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


class VaneWheelTests(unittest.TestCase):
    @staticmethod
    def manifest() -> object:
        return MODULE.ExtensionManifest(
            schema_version=1,
            name="iceberg",
            extension_config="extension_config.cmake",
            build_extensions=("httpfs", "parquet"),
            native_test_selection="test/",
            vane_repository="AstroVela/vane",
            vane_revision="a" * 40,
        )

    @staticmethod
    def identity() -> object:
        return MODULE.VaneIdentity(
            source_id="b" * 40,
            fork_version="v1.5.0-vane.aaaaaaaaaa",
            upstream_version="v1.5.0",
        )

    def test_reads_exact_vane_wheel_extension_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vane_source = Path(temporary_directory)
            (vane_source / "pyproject.toml").write_text(
                "[tool.scikit-build.cmake.define]\n"
                'BUILD_EXTENSIONS = "core_functions;json;parquet;icu;httpfs"\n'
            )

            self.assertEqual(
                MODULE.load_vane_default_build_extensions(vane_source),
                ("core_functions", "json", "parquet", "icu", "httpfs"),
            )

    def test_rejects_duplicate_vane_wheel_extension_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vane_source = Path(temporary_directory)
            (vane_source / "pyproject.toml").write_text(
                "[tool.scikit-build.cmake.define]\n"
                'BUILD_EXTENSIONS = "core_functions;json;json"\n'
            )

            with self.assertRaisesRegex(
                MODULE.ConfigurationError, "duplicate extension"
            ):
                MODULE.load_vane_default_build_extensions(vane_source)

    def test_generated_link_config_preserves_caller_link_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = MODULE.write_vane_wheel_link_config(
                Path(temporary_directory), "iceberg"
            )
            contents = config.read_text()

            self.assertIn("IN LISTS DUCKDB_EXTENSION_NAMES", contents)
            self.assertIn("_SHOULD_LINK", contents)
            self.assertIn(
                'set(BUILD_EXTENSIONS "${_VANE_LINKED_EXTENSIONS}" PARENT_SCOPE)',
                contents,
            )
            self.assertIn(
                "Caller config did not register iceberg for static linking", contents
            )

    def test_generated_dependency_prefix_keeps_backend_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prefix = root / "vane-installed/x64-linux"
            config = MODULE.write_vane_wheel_dependency_prefix_config(root, prefix)

            self.assertEqual(config.name, "vane-dependency-prefix.cmake")
            self.assertIn(
                f'list(PREPEND CMAKE_PREFIX_PATH "{prefix}")', config.read_text()
            )

    def test_wheel_platform_has_no_non_x64_fallback(self) -> None:
        with (
            mock.patch.object(MODULE.sys, "platform", "linux"),
            mock.patch.object(
                MODULE.os,
                "uname",
                return_value=mock.Mock(machine="aarch64"),
            ),
            self.assertRaisesRegex(
                MODULE.ConfigurationError, "require 64-bit x86 Linux"
            ),
        ):
            MODULE.require_vane_wheel_platform("x64-linux")

        with self.assertRaisesRegex(
            MODULE.ConfigurationError, "VCPKG_TARGET_TRIPLET=x64-linux"
        ):
            MODULE.require_vane_wheel_platform("arm64-linux")

    def test_requires_vane_arrow_flight_dependency_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            arrow_config = (
                root
                / "installed/x64-linux/share/arrow/ArrowConfig.cmake"
            )
            arrow_config.parent.mkdir(parents=True)
            arrow_config.write_text("# fixture\n")

            with (
                mock.patch.dict(
                    os.environ,
                    {"VANE_VCPKG_INSTALLED_DIR": str(root / "installed")},
                    clear=True,
                ),
                self.assertRaisesRegex(
                    MODULE.ConfigurationError, "ArrowFlightConfig.cmake"
                ),
            ):
                MODULE.require_vane_wheel_dependency_prefix(
                    root / "vane", "x64-linux"
                )

    def test_rejects_ignored_duckdb_local_extension_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vane_source = Path(temporary_directory)
            local_config = (
                vane_source
                / "external/duckdb/extension/extension_config_local.cmake"
            )
            local_config.parent.mkdir(parents=True)
            local_config.write_text("# ambient fixture\n")

            with self.assertRaisesRegex(
                MODULE.ConfigurationError, "ignored local extension config"
            ):
                MODULE.reject_duckdb_local_extension_config(vane_source)

    def test_builds_and_verifies_wheel_with_caller_extension_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            extension_root = root / "extension"
            extension_root.mkdir()
            extension_config = extension_root / "extension_config.cmake"
            extension_config.write_text("# fixture\n")
            vane_source = root / "vane"
            vane_source.mkdir()
            (vane_source / "pyproject.toml").write_text(
                "[tool.scikit-build.cmake.define]\n"
                'BUILD_EXTENSIONS = "core_functions;json;iceberg;httpfs"\n'
            )
            installed_root = root / "vane-installed"
            for relative in (
                "share/arrow/ArrowConfig.cmake",
                "share/arrowflight/ArrowFlightConfig.cmake",
            ):
                dependency = installed_root / "x64-linux" / relative
                dependency.parent.mkdir(parents=True, exist_ok=True)
                dependency.write_text("# fixture\n")
            toolchain = root / "vcpkg/scripts/buildsystems/vcpkg.cmake"
            toolchain.parent.mkdir(parents=True)
            toolchain.write_text("# fixture\n")
            build_dir = root / "build/vane-wheel"
            dist_dir = build_dir / "dist"
            calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

            def fake_run(
                command: list[str],
                *,
                cwd: Path | None = None,
                capture: bool = False,
                env: dict[str, str] | None = None,
            ) -> str:
                del capture
                calls.append((command, cwd, env))
                if command[1:3] == ["-m", "build"]:
                    output_dir = Path(command[command.index("--outdir") + 1])
                    fixture_wheel = (
                        output_dir
                        / "vane_ai-1.5.0-cp312-cp312-linux_x86_64.whl"
                    )
                    fixture_wheel.write_bytes(b"fixture")
                return ""

            identity = self.identity()
            manifest = self.manifest()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "VCPKG_TOOLCHAIN_PATH": str(toolchain),
                        "VCPKG_TARGET_TRIPLET": "x64-linux",
                        "VANE_VCPKG_INSTALLED_DIR": str(installed_root),
                        "CMAKE_PREFIX_PATH": "/ambient/prefix",
                        "SKBUILD_CMAKE_DEFINE": "BUILD_EXTENSIONS=ambient",
                        "VANE_CMAKE_PREFIX_PATH": "/ambient/vane-prefix",
                        "DONT_LINK": "1",
                        "DUCKDB_HTTPFS_DIRECTORY": "/ambient/httpfs",
                        "DUCKDB_ICEBERG_DIRECTORY": "/ambient/iceberg",
                        "VCPKG_OVERLAY_PORTS": "/ambient/ports",
                        "COVERAGE": "true",
                        "GITHUB_BASE_REF": "release/9.9",
                        "GITHUB_REF_NAME": "extension-branch",
                        "VANE_VERSION_BRANCH": "release/9.9",
                        "SETUPTOOLS_SCM_PRETEND_VERSION": "99.0.0",
                    },
                    clear=True,
                ),
                mock.patch.object(
                    MODULE, "resolve_vane_identity", return_value=identity
                ),
                mock.patch.object(MODULE, "run", side_effect=fake_run),
                mock.patch.object(MODULE, "verify_vane_wheel") as verify_wheel,
            ):
                wheel = MODULE.build_vane_wheel(
                    extension_root,
                    manifest,
                    vane_source,
                    build_dir,
                    dist_dir,
                    jobs=12,
                )

            self.assertEqual(
                wheel,
                dist_dir / "vane_ai-1.5.0-cp312-cp312-linux_x86_64.whl",
            )
            self.assertEqual(len(calls), 1)
            command, cwd, environment = calls[0]
            self.assertEqual(command[1:4], ["-m", "build", "--wheel"])
            self.assertEqual(command[-1], str(vane_source))
            self.assertNotEqual(
                Path(command[command.index("--outdir") + 1]), dist_dir
            )
            self.assertEqual(cwd, extension_root)
            assert environment is not None
            cmake_args = MODULE.shlex.split(environment["CMAKE_ARGS"])
            self.assertIn("--fresh", cmake_args)
            self.assertIn("-DBUILD_DISTRIBUTED_EXCHANGE=ON", cmake_args)
            self.assertIn("-DENABLE_EXTENSION_AUTOINSTALL=OFF", cmake_args)
            self.assertIn(
                f"-DDUCKDB_EXTENSION_CONFIGS={extension_config};"
                f"{build_dir / 'vane-extension-link.cmake'}",
                cmake_args,
            )
            self.assertIn(
                "-DBUILD_EXTENSIONS=core_functions;json;httpfs;parquet",
                cmake_args,
            )
            self.assertIn(
                "-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES="
                f"{build_dir / 'vane-dependency-prefix.cmake'}",
                cmake_args,
            )
            self.assertNotIn(
                f"-DCMAKE_PREFIX_PATH={installed_root / 'x64-linux'}", cmake_args
            )
            self.assertFalse(
                any("ambient" in argument for argument in cmake_args), cmake_args
            )
            self.assertEqual(environment["CMAKE_BUILD_PARALLEL_LEVEL"], "12")
            self.assertEqual(environment["VCPKG_MAX_CONCURRENCY"], "12")
            self.assertEqual(environment["VCPKG_TARGET_TRIPLET"], "x64-linux")
            self.assertEqual(environment["VCPKG_TOOLCHAIN_PATH"], str(toolchain))
            self.assertNotIn("CMAKE_PREFIX_PATH", environment)
            self.assertNotIn("COVERAGE", environment)
            self.assertNotIn("GITHUB_BASE_REF", environment)
            self.assertNotIn("GITHUB_REF_NAME", environment)
            self.assertNotIn("VANE_VERSION_BRANCH", environment)
            self.assertNotIn("SETUPTOOLS_SCM_PRETEND_VERSION", environment)
            self.assertNotIn("SKBUILD_CMAKE_DEFINE", environment)
            self.assertNotIn("VANE_CMAKE_PREFIX_PATH", environment)
            self.assertNotIn("DONT_LINK", environment)
            self.assertNotIn("DUCKDB_HTTPFS_DIRECTORY", environment)
            self.assertNotIn("VCPKG_OVERLAY_PORTS", environment)
            self.assertEqual(
                environment["DUCKDB_ICEBERG_DIRECTORY"], str(extension_root)
            )
            verify_wheel.assert_called_once()
            verified_wheel, verified_manifest, verified_identity, temporary_parent = (
                verify_wheel.call_args.args
            )
            self.assertEqual(verified_wheel.name, wheel.name)
            self.assertNotEqual(verified_wheel, wheel)
            self.assertEqual(verified_manifest, manifest)
            self.assertEqual(verified_identity, identity)
            self.assertEqual(temporary_parent, build_dir.parent)

    def test_verification_loads_static_extension_without_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            wheel = root / "vane_ai.whl"
            wheel.write_bytes(b"fixture")
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PYTHONHOME": "/unsafe/home",
                        "PYTHONPATH": "/unsafe/path",
                        "VIRTUAL_ENV": "/unsafe/venv",
                    },
                    clear=True,
                ),
                mock.patch.object(MODULE, "run") as run_command,
            ):
                MODULE.verify_vane_wheel(
                    wheel,
                    self.manifest(),
                    self.identity(),
                    root,
                )

            self.assertEqual(run_command.call_count, 3)
            for call in run_command.call_args_list:
                environment = call.kwargs["env"]
                self.assertNotIn("PYTHONHOME", environment)
                self.assertNotIn("PYTHONPATH", environment)
                self.assertNotIn("VIRTUAL_ENV", environment)
            install_command = run_command.call_args_list[1].args[0]
            self.assertEqual(install_command[1:4], ["-m", "pip", "install"])
            self.assertEqual(install_command[-1], str(wheel))
            verification_call = run_command.call_args_list[2]
            verification_command = verification_call.args[0]
            self.assertEqual(verification_command[1:3], ["-I", "-c"])
            self.assertIn("LOAD {extension_name}", verification_command[3])
            self.assertNotIn("INSTALL ", verification_command[3])
            self.assertIn("STATICALLY_LINKED", verification_command[3])
            self.assertIn("expected_source_id[:10]", verification_command[3])


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

    def test_wheel_lane_is_read_only_and_uploads_verified_artifact(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/_vane_extension_ci.yml"
        ).read_text()
        _, wheel_job = workflow.split("  wheel:\n", 1)
        self.assertIn("needs: verify_workflow", wheel_job)
        self.assertIn("permissions:\n      contents: read", wheel_job)
        self.assertNotIn("id-token: write", wheel_job)
        self.assertIn("vane_wheel", wheel_job)
        self.assertIn("VCPKG_MAX_CONCURRENCY", wheel_job)
        self.assertIn(
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            wheel_job,
        )

    def test_local_wheel_target_bootstraps_exact_vane_dependencies(self) -> None:
        makefile = (
            Path(__file__).parents[1] / "makefiles/vane_extension.Makefile"
        ).read_text()
        self.assertIn("vane_wheel: vane_wheel_dependencies", makefile)
        self.assertIn(
            'bash "$(VANE_SOURCE_DIR)/scripts/bootstrap_vcpkg.sh"', makefile
        )
        self.assertEqual(
            makefile.count('VCPKG_MAX_CONCURRENCY="$(VANE_BUILD_JOBS)"'), 2
        )
        self.assertIn('test "$$(uname -m)" = "x86_64"', makefile)
        self.assertIn('test "$$(getconf LONG_BIT)" = "64"', makefile)

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
