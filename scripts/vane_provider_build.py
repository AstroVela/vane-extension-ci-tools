#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Vane contributors
# SPDX-License-Identifier: Apache-2.0

"""Delegate provider packaging and verification to an exact Vane tooling checkout."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def load_release_tools():
    path = Path(__file__).with_name("vane_provider_release.py")
    spec = importlib.util.spec_from_file_location("_provider_build_release_tools", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load release tools: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-root", required=True, type=Path)
    parser.add_argument("--vane-source", required=True, type=Path)
    parser.add_argument("--extension-name", required=True)
    parser.add_argument("--operation", choices=("build", "verify"), default="build")
    args, remaining = parser.parse_known_args(argv)
    tools = load_release_tools()
    config = tools.load_config(args.extension_root / "vane-provider-release.toml")
    provider = config.provider(args.extension_name)
    if provider.release_number is None:
        raise tools.ReleaseValidationError(
            "provider packaging requires a configured release_number"
        )
    source = Path(
        os.environ.get("VANE_PROVIDER_PACKAGING_SOURCE", str(args.vane_source))
    ).resolve()
    if config.packaging_revision is not None:
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != config.packaging_revision:
            raise tools.ReleaseValidationError(
                "provider packaging checkout differs from its exact configured revision"
            )
        if subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            text=True,
        ).strip():
            raise tools.ReleaseValidationError(
                "provider packaging requires a clean tooling checkout"
            )
    if any(
        value == "--release-number" or value.startswith("--release-number=")
        for value in remaining
    ):
        raise tools.ReleaseValidationError(
            "release_number must come only from the provider release config"
        )
    script = (
        "build_extension_wheel.py"
        if args.operation == "build"
        else "verify_extension_wheel.py"
    )
    command = [
        sys.executable,
        "-I",
        str(source / "scripts" / script),
        "--extension-name",
        args.extension_name,
    ]
    if args.operation == "build":
        command.extend(("--release-number", str(provider.release_number)))
    subprocess.run([*command, *remaining], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
