# SPDX-FileCopyrightText: 2026 Vane contributors
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts/vane_provider_build.py"
SPEC = importlib.util.spec_from_file_location("vane_provider_build", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProviderBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "vane-provider-release.toml").write_text(
            'interpreters = ["cp312"]\nplatforms = ["manylinux_2_28_x86_64"]\n'
            "max_wheel_bytes = 100000000\n[providers.sample]\n"
            'distribution = "vane-extension-sample"\ndependencies = []\nrelease_number = 3\n'
            '[packaging]\nrepository = "AstroVela/vane"\nrevision = "'
            + "a" * 40
            + '"\n'
        )
        self.arguments = [
            "--extension-root",
            str(self.root),
            "--vane-source",
            str(self.root / "engine"),
            "--extension-name",
            "sample",
        ]

    def test_build_delegates_to_pinned_tools_with_configured_sequence(self):
        with mock.patch.dict(
            "os.environ", {"VANE_PROVIDER_PACKAGING_SOURCE": str(self.root / "tools")}
        ), mock.patch.object(
            MODULE.subprocess, "check_output", side_effect=["a" * 40, ""]
        ), mock.patch.object(
            MODULE.subprocess, "run"
        ) as run:
            self.assertEqual(
                MODULE.main([*self.arguments, "--artifact", "sample.duckdb_extension"]),
                0,
            )
        command = run.call_args.args[0]
        self.assertEqual(
            command[:3],
            [
                sys.executable,
                "-I",
                str(self.root / "tools/scripts/build_extension_wheel.py"),
            ],
        )
        self.assertIn("--release-number", command)
        self.assertEqual(command[command.index("--release-number") + 1], "3")
        self.assertEqual(command[-2:], ["--artifact", "sample.duckdb_extension"])

    def test_verification_uses_the_same_pinned_tooling_without_build_flags(self):
        with mock.patch.object(
            MODULE.subprocess, "check_output", side_effect=["a" * 40, ""]
        ), mock.patch.object(MODULE.subprocess, "run") as run:
            MODULE.main(
                [*self.arguments, "--operation", "verify", "--base-wheel", "base.whl"]
            )
        command = run.call_args.args[0]
        self.assertTrue(command[2].endswith("scripts/verify_extension_wheel.py"))
        self.assertNotIn("--release-number", command)

    def test_wrong_revision_dirty_tools_and_number_overrides_are_rejected(self):
        cases = [
            (["b" * 40], []),
            (["a" * 40, " M scripts/build_extension_wheel.py"], []),
            (["a" * 40, ""], ["--release-number=99"]),
        ]
        for outputs, extra in cases:
            with self.subTest(extra=extra, outputs=outputs), mock.patch.object(
                MODULE.subprocess, "check_output", side_effect=outputs
            ), mock.patch.object(MODULE.subprocess, "run") as run:
                with self.assertRaises(RuntimeError):
                    MODULE.main([*self.arguments, *extra])
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
