"""Exercise host selection and the real filesystem on every wheel platform."""

from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "vane_platform_test_tools", Path(__file__).parents[1] / "scripts/vane_extension.py"
)
assert SPEC and SPEC.loader
tools = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tools
SPEC.loader.exec_module(tools)


class PlatformTests(unittest.TestCase):
    def test_triplets_match_the_host_and_reject_other_hosts(self):
        targets = (
            ("linux", "x86_64", "x64-linux-release"),
            ("darwin", "arm64", "arm64-osx-release"),
            ("win32", "AMD64", "x64-windows-static-release"),
        )
        for system, machine, triplet in targets:
            with (
                self.subTest(system=system),
                mock.patch.object(tools.sys, "platform", system),
                mock.patch.object(tools.platform, "machine", return_value=machine),
            ):
                self.assertEqual(tools.default_wheel_triplet(), triplet)
                tools.require_vane_wheel_platform(triplet)
                for _, _, other in targets:
                    if other != triplet:
                        with self.assertRaises(tools.ConfigurationError):
                            tools.require_vane_wheel_platform(other)
        with mock.patch.object(tools.ctypes, "sizeof", return_value=4):
            with self.assertRaises(tools.ConfigurationError):
                tools.default_wheel_triplet()

    def test_real_venv_python_is_executable(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "environment with spaces"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(root)], check=True
            )
            result = subprocess.check_output(
                [str(tools.venv_python(root)), "-I", "-c", "print(42)"], text=True
            )
            self.assertEqual(result.strip(), "42")

    def test_checkout_publication_never_replaces_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            for populated in (False, True):
                staged, destination = (
                    root / f"staged-{populated}",
                    root / f"dest-{populated}",
                )
                staged.mkdir()
                destination.mkdir()
                (staged / "new").write_text("verified")
                if populated:
                    (destination / "original").write_text("keep")
                with self.assertRaises(tools.ConfigurationError):
                    tools.publish_vane_checkout(staged, destination)
                self.assertTrue((staged / "new").is_file())
                self.assertFalse((destination / "new").exists())
                if populated:
                    self.assertEqual((destination / "original").read_text(), "keep")
            staged, destination = root / "verified", root / "published"
            staged.mkdir()
            (staged / "marker").write_text("exact source")
            tools.publish_vane_checkout(staged, destination)
            self.assertFalse(staged.exists())
            self.assertEqual((destination / "marker").read_text(), "exact source")

    def test_bootstrap_uses_native_paths_and_matching_host_tools(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            with (
                mock.patch.dict(os.environ, {"VCPKG_HOST_TRIPLET": "wrong"}),
                mock.patch.object(tools, "run") as run,
            ):
                tools.bootstrap_wheel_dependencies(
                    root, root / "vcpkg", root / "installed", 2
                )
            environment = run.call_args.kwargs["env"]
            self.assertEqual(
                environment["VCPKG_HOST_TRIPLET"], environment["VCPKG_TARGET_TRIPLET"]
            )
            self.assertEqual(environment["VCPKG_MAX_CONCURRENCY"], "2")
            self.assertEqual(run.call_args.args[0][0], tools.native_bash())

    def test_windows_bash_comes_from_git_instead_of_the_wsl_launcher(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "Git installation"
            git = root / "cmd/git.exe"
            bash = root / "bin/bash.exe"
            git.parent.mkdir(parents=True)
            bash.parent.mkdir(parents=True)
            git.touch()
            bash.touch()
            with (
                mock.patch.object(tools.sys, "platform", "win32"),
                mock.patch.object(
                    tools.shutil, "which", return_value=str(git)
                ) as which,
            ):
                self.assertEqual(tools.native_bash(), bash.resolve().as_posix())
                which.assert_called_once_with("git")
                bash.unlink()
                with self.assertRaises(tools.ConfigurationError):
                    tools.native_bash()

    def test_real_dependency_bootstrap_uses_a_native_shell_with_spaces(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value) / "Vane source with spaces"
            script = root / "scripts/bootstrap_vcpkg.sh"
            script.parent.mkdir(parents=True)
            script.write_text(
                "set -euo pipefail\n"
                'test "$VCPKG_HOST_TRIPLET" = "$VCPKG_TARGET_TRIPLET"\n'
                'uname -s > "$1/native-shell.txt"\n',
                encoding="utf-8",
                newline="\n",
            )
            tools.bootstrap_wheel_dependencies(
                root, root / "vcpkg", root / "installed", 2
            )
            system = (root / "native-shell.txt").read_text().strip()
            if sys.platform == "win32":
                self.assertTrue(system.startswith(("MINGW", "MSYS", "CYGWIN")))
            else:
                self.assertEqual(system, platform.system())

    def test_dependency_bootstrap_retries_are_bounded_and_reuse_installed_files(self):
        for succeed in (True, False):
            with self.subTest(succeed=succeed), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                script = root / "scripts/bootstrap_vcpkg.sh"
                script.parent.mkdir()
                script.write_text(
                    "set -euo pipefail\n"
                    "count=0\n"
                    'if [ -f "$1/attempts" ]; then read -r count < "$1/attempts"; fi\n'
                    "count=$((count + 1))\n"
                    'printf "%s\\n" "$count" > "$1/attempts"\n'
                    f'if [ "$count" -lt 3 ] || [ "{succeed}" = False ]; then exit 28; fi\n',
                    encoding="utf-8",
                    newline="\n",
                )
                installed = root / "installed"
                installed.mkdir()
                sentinel = installed / "already-built"
                sentinel.write_text("keep this package")
                with mock.patch.object(tools.time, "sleep") as sleep:
                    if succeed:
                        tools.bootstrap_wheel_dependencies(
                            root, root / "vcpkg", installed, 2
                        )
                    else:
                        with self.assertRaises(subprocess.CalledProcessError):
                            tools.bootstrap_wheel_dependencies(
                                root, root / "vcpkg", installed, 2
                            )
                self.assertEqual((root / "attempts").read_text().strip(), "3")
                self.assertEqual(sentinel.read_text(), "keep this package")
                self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
