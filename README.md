# Vane Extension CI Tools

Reusable tooling for building and testing out-of-tree DuckDB extensions against
an exact [AstroVela/Vane](https://github.com/AstroVela/vane) revision.

This project is deliberately separate from
[`duckdb/extension-ci-tools`](https://github.com/duckdb/extension-ci-tools).
An extension can keep DuckDB's original `duckdb/` and `extension-ci-tools/`
submodules for its upstream build while adding this repository as a second,
Vane-only integration layer.

## Contract

- `AstroVela/vane` is the only accepted source of the distributed DuckDB fork.
  The tools check out its exact revision and build against `external/duckdb`;
  no DuckDB mirror or alternate fork is accepted.
- Every build pins a full Vane commit SHA. Branch names, tags, `main`, version
  guesses, and runtime fallbacks are rejected.
- Local Make targets read the exact CI-tools SHA from the extension repository's
  committed `vane-extension-ci-tools` gitlink, then require this checkout to be
  clean, at that revision, and available from the official repository.
- DuckDB's content-derived SourceID and Vane fork version are obtained through
  the scripts in the selected Vane checkout.
- The standard DuckDB extension targets are not replaced. All Make targets
  provided here use the `vane_` prefix.
- The native lane disables Arrow Flight exchange while compiling and running
  the extension's original DuckDB tests. The wheel lane enables distributed
  exchange and uses the exact Vane checkout's complete native dependency set.
- Wheel verification disables extension autoload and autoinstall, loads the
  target only from the wheel, and requires it to report `STATICALLY_LINKED`.
  Ray and service-backed end-to-end lanes are separate later stages.

## Extension layout

```text
duckdb/                       # upstream DuckDB submodule
extension-ci-tools/           # upstream DuckDB tooling
vane-extension-ci-tools/      # this repository
vane-extension.toml           # exact Vane integration manifest
```

Example `.gitmodules` entry:

```ini
[submodule "vane-extension-ci-tools"]
    path = vane-extension-ci-tools
    url = https://github.com/AstroVela/vane-extension-ci-tools.git
```

The upstream Makefile can retain its existing include and add the Vane targets:

```make
include extension-ci-tools/makefiles/duckdb_extension.Makefile
include vane-extension-ci-tools/makefiles/vane_extension.Makefile
```

The second include only defines `vane_verify_ci_tools`, `vane_validate`,
`vane_prepare`, `vane_identity`, `vane_native`, `vane_ci`,
`vane_wheel_dependencies`, and `vane_wheel`.

## Manifest

Copy [`templates/vane-extension.toml`](templates/vane-extension.toml) into the
extension repository and replace every placeholder with a reviewed value. The
Vane revision must be a complete lowercase 40-character commit SHA.

The manifest describes source and test selection only. `build_extensions`
lists supporting extensions; it must not repeat the target `name`, whose source
and link policy are owned by `extension_config`. Distributed scan and write
protocol versions remain owned by the extension's C++ registrations and the
runtime capability manifest.

## Local native lane

Set the vcpkg toolchain used by the extension, then run:

```bash
export VCPKG_TOOLCHAIN_PATH=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
make vane_ci
```

Before validation or source preparation, `vane_verify_ci_tools` compares this
checkout with the exact submodule gitlink committed in the extension's `HEAD`.
It rejects a different revision, working-tree changes, a missing gitlink, and
commits unavailable from the official `AstroVela/vane-extension-ci-tools`
repository.

`vane_prepare` clones the exact manifest revision into `build/vane-source` with
the complete Git history required by Vane's identity resolver. A new checkout
is prepared and verified in a temporary sibling directory, then atomically
published without replacing a destination that appeared concurrently. A failed
fetch leaves the destination free for a clean retry. Atomic publication requires
64-bit x86 Linux `renameat2`, invoked through the kernel syscall rather than a
libc `renameat2` wrapper; the tools fail instead of falling back to a
replacement-prone rename. If that checkout already exists, it must be clean and
at the exact revision; a shallow checkout is safely unshallowed. Existing
checkouts are always verified by fetching the exact revision directly from the
hard-coded official `AstroVela/vane` URL; a different `origin` cannot substitute
a fork-only commit. Preparation also fetches release tags from that same
official URL and requires every local tag ref to match it exactly, so wheel
metadata cannot use missing, moved, or private tags. The tools never reset or
clean working-tree changes.

Override generated locations without changing the manifest:

```bash
make vane_ci \
  VANE_SOURCE_DIR=/tmp/vane-source \
  VANE_NATIVE_BUILD_DIR=/tmp/vane-native \
  VANE_BUILD_JOBS=8
```

## Local wheel lane

The wheel lane is intentionally Linux x86_64 only. It bootstraps Arrow Flight,
gRPC, and the rest of Vane's native dependencies from the exact pinned Vane
checkout, then builds the extension's own vcpkg manifest separately:

```bash
python3 -m pip install \
  build \
  "cmake>=3.29" \
  "ninja>=1.10" \
  "pybind11[global]>=3.0.0" \
  "scikit-build-core>=0.11.4" \
  "setuptools-scm>=9.2.0"
export VCPKG_TOOLCHAIN_PATH=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
make vane_wheel VANE_BUILD_JOBS=8
```

The output is written to `build/vane-wheel/dist`. A fresh virtual environment
installs that wheel and its Python dependencies, with DuckDB extension
autoinstall and autoload disabled. Verification performs `LOAD` without
`INSTALL`, requires the manifest target to be statically linked, and compares
the embedded Vane fork version and the 10-character DuckDB SourceID reported
by Vane with the corresponding prefix of the exact checkout's verified full
SourceID.

Generated locations can be overridden with `VANE_WHEEL_BUILD_DIR`,
`VANE_WHEEL_DIST_DIR`, `VANE_VCPKG_ROOT`, and
`VANE_VCPKG_INSTALLED_DIR`. The supported vcpkg triplet is exactly
`x64-linux`; there is no alternate-platform or dependency fallback.

## GitHub Actions

An extension calls the reusable workflow with the same exact tool revision as
its `vane-extension-ci-tools` submodule:

```yaml
jobs:
  vane-extension:
    permissions:
      contents: read
    uses: AstroVela/vane-extension-ci-tools/.github/workflows/_vane_extension_ci.yml@TOOLS_COMMIT_SHA
    with:
      ci_tools_version: TOOLS_COMMIT_SHA
      manifest: vane-extension.toml
      build_jobs: 8
```

The caller's `uses` target, `ci_tools_version`, and CI-tools submodule gitlink
must name the same full commit SHA. The read-only verification job checks that
gitlink and GitHub's `job.workflow_repository`, `job.workflow_ref`, and
`job.workflow_sha` metadata for the actually called reusable workflow. It then
checks out that SHA directly from the hard-coded official
`AstroVela/vane-extension-ci-tools` repository, and verifies both the checkout
and a fresh official fetch resolve to it. The extension build and tests also run
with `contents: read` alone. The verified wheel is uploaded as
`vane-<extension-name>-wheel`, and the workflow uses no deployment secrets.

## Development

Run the self-contained test suite with Python 3.11 or newer:

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/vane_extension.py tests/test_vane_extension.py
```
