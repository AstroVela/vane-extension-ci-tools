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
- DuckDB's content-derived SourceID and Vane fork version are obtained through
  the scripts in the selected Vane checkout.
- The standard DuckDB extension targets are not replaced. All Make targets
  provided here use the `vane_` prefix.
- The initial native lane disables Arrow Flight exchange while compiling and
  running the extension's original DuckDB tests. Full Vane wheel, Ray, and FTE
  lanes will use Vane's complete pinned native dependency set.

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

The second include only defines `vane_validate`, `vane_prepare`,
`vane_identity`, `vane_native`, and `vane_ci`.

## Manifest

Copy [`templates/vane-extension.toml`](templates/vane-extension.toml) into the
extension repository and replace every placeholder with a reviewed value. The
Vane revision must be a complete lowercase 40-character commit SHA.

The manifest describes source and test selection only. Distributed scan and
write protocol versions remain owned by the extension's C++ registrations and
the runtime capability manifest.

## Local native lane

Set the vcpkg toolchain used by the extension, then run:

```bash
export VCPKG_TOOLCHAIN_PATH=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
make vane_ci
```

`vane_prepare` clones the exact manifest revision into `build/vane-source` with
the complete Git history required by Vane's identity resolver. If that checkout
already exists, it must be clean and at the exact revision; a shallow checkout
is safely unshallowed. Existing checkouts are always verified by fetching the
exact revision directly from the hard-coded official `AstroVela/vane` URL; a
different `origin` cannot substitute a fork-only commit. The tools never reset
or clean working-tree changes.

Override generated locations without changing the manifest:

```bash
make vane_ci \
  VANE_SOURCE_DIR=/tmp/vane-source \
  VANE_NATIVE_BUILD_DIR=/tmp/vane-native \
  VANE_BUILD_JOBS=8
```

## GitHub Actions

An extension calls the reusable workflow with the same exact tool revision as
its `vane-extension-ci-tools` submodule:

```yaml
jobs:
  vane-native:
    # Reusable workflows cannot elevate this permission from the caller.
    permissions:
      contents: read
      id-token: write
    uses: AstroVela/vane-extension-ci-tools/.github/workflows/_vane_extension_ci.yml@TOOLS_COMMIT_SHA
    with:
      ci_tools_version: TOOLS_COMMIT_SHA
      manifest: vane-extension.toml
```

The caller must grant `id-token: write`: a reusable workflow cannot elevate
permissions inherited from its caller. The workflow verifies the caller's
submodule gitlink and uses GitHub's signed OIDC claims to verify that the
reusable workflow itself was invoked at that same commit SHA. Only the isolated
verification job receives OIDC permission; the extension build and tests run in
a separate job with `contents: read` alone. The workflow uses no deployment
secrets and requests an OIDC token only for this identity attestation.

## Development

Run the self-contained test suite with Python 3.11 or newer:

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/vane_extension.py tests/test_vane_extension.py
```
