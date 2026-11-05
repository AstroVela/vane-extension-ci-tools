"""Exercise host selection and the real filesystem on every wheel platform."""

from __future__ import annotations

import importlib.util
import os
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
            self.assertEqual(run.call_args.args[0][0], "bash")


if __name__ == "__main__":
    unittest.main()
