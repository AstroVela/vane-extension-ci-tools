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
- Every build pins full Vane and official `microsoft/vcpkg` commit SHAs in the
  integration manifest. Branch names, tags, `main`, version guesses, native
  manifest inference, and runtime fallbacks are rejected.
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
`vane_prepare`, `vane_identity`, `vane_verify_vcpkg`, `vane_native`,
`vane_ci`, `vane_wheel_dependencies`, and `vane_wheel`.

## Manifest

Copy [`templates/vane-extension.toml`](templates/vane-extension.toml) into the
extension repository and replace every placeholder with a reviewed value.
Schema 2 requires complete lowercase 40-character Vane and vcpkg commit SHAs.
The vcpkg revision is an explicit Vane-lane input; it is not inferred from the
extension's native `vcpkg.json`.

The manifest describes exact source and toolchain selection plus native test
selection. `build_extensions` lists supporting extensions; it must not repeat
the target `name`, whose source and link policy are owned by
`extension_config`. Distributed scan and write protocol versions remain owned
by the extension's C++ registrations and the runtime capability manifest.

## Local native lane

Set the vcpkg toolchain used by the extension, then run:

```bash
export VCPKG_TOOLCHAIN_PATH=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
make vane_ci
```

Before either build lane starts, `vane_verify_vcpkg` requires the toolchain to
come from a clean Git checkout at the manifest's exact vcpkg revision. A
different revision, nonstandard toolchain location, or non-Git installation is
rejected.

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
metadata cannot use missing, moved, or private tags. Vane source preparation
never resets or cleans working-tree changes.

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

The native and wheel lanes keep separate, bounded 750 MiB `ccache` directories.
Their cache keys include the runner, vcpkg triplet, and exact Vane and vcpkg
revisions, so either source change starts a new compiler cache without sharing
a parallel lane's write. The workflow explicitly passes `ccache` as CMake's C
and C++ compiler launcher, reports both the Actions cache restore result and
per-run `ccache` statistics, and never caches a CMake build directory or a
`vcpkg_installed` tree. `lukka/run-vcpkg` remains responsible for vcpkg's binary
package cache. After bootstrapping, each lane validates that run-vcpkg's
`vcpkgLastBuiltCommitId` state marker is a regular file containing the manifest's
exact vcpkg revision, removes only that marker, and requires the vcpkg checkout to
be clean before the extension build starts.

## Provider release validation

The native build layer above and the dynamic provider release gate are separate.
DuckDB's upstream [distribution workflow](https://github.com/duckdb/extension-ci-tools/blob/c67b0681a594af88529ad4dd06ed874b91b1a198/.github/workflows/_extension_distribution.yml)
already supports repository/ref overrides, toolchains and platform matrices.
Keep using upstream extension Make targets and tooling for those generic tasks;
do not copy its binary deployment implementation here. Vane-specific source
identity and Python provider release contracts belong in this repository.

`scripts/vane_provider_release.py` replaces per-extension copies of release
matrix and package-index hash validation. It uses the Python Packaging Authority's
`packaging` library for versions, requirements and wheel filenames. It requires
Python 3.11+ to run, independently of the interpreters targeted by the wheels.

Copy [`templates/vane-provider-release.toml`](templates/vane-provider-release.toml)
into the extension repository. Declare the complete interpreter/platform matrix,
the actual index/project upload limit in bytes, and each provider's distribution
name and direct dependencies. All dependencies must be providers in that same
candidate set. Unknown fields, duplicate identities and cycles are errors.
For a two-provider graph, for example:

```toml
interpreters = ["cp310", "cp311", "cp312", "cp313", "cp314"]
platforms = ["manylinux_2_28_x86_64"]
max_wheel_bytes = 100000000

[providers.avro]
distribution = "vane-extension-avro"
dependencies = []

[providers.iceberg]
distribution = "vane-extension-iceberg"
dependencies = ["avro"]
```

Run against an exact, reviewed checkout of these tools (use the same committed
gitlink as the native integration):

```bash
python -m pip install -r vane-extension-ci-tools/requirements-release.txt
python -I vane-extension-ci-tools/scripts/vane_provider_release.py validate \
  --channel testpypi-dev \
  --manifest vane-extension.toml --extension-root . \
  --vane-source ../vane --ci-tools-version "$CI_TOOLS_REVISION" \
  --config vane-provider-release.toml \
  --directory dist/providers \
  --vane-version 0.2.0.dev612 \
  --require-publishable-on testpypi \
  --github-output "$GITHUB_OUTPUT"
```

Select the release channel explicitly: `testpypi-dev` requires a canonical
development Vane version; `release` accepts canonical non-development Vane
versions, including alpha, beta, release-candidate and post releases. Both require
an `X.Y.Z` release segment without an epoch or local version. Development
candidates cannot request PyPI publication checks. The gate requires
one wheel per declared interpreter/platform pair, one immutable version per
provider, exact `===` dependencies on Vane and the full transitive provider
closure, matching filename/METADATA identities, and the configured upload size
limit. The limit is an index/project setting, **not** a replacement for Vane's
native artifact safety budgets. Only top-level `*.whl` files are considered;
upload that same wheel set without changing it after validation.

All commands require the native integration manifest, its extension root, an
existing Vane checkout, and the expected full CI-tools commit SHA. Set
`CI_TOOLS_REVISION` to the reviewed pin (for a submodule integration, the
committed `HEAD:vane-extension-ci-tools` gitlink). The gate reuses the native
tooling to validate the manifest's full Vane SHA, verify that both revisions
are available from the official repositories, and reject wrong or dirty
checkouts before accepting artifacts or writing outputs. A shallow Vane
checkout is sufficient here: release assembly does not derive an engine
version or build native code. These are read-only source checks; the gate
does not prepare, reset or replace checkouts.

Standard output is JSON with `vane_version` and `<provider>_version` keys. The
optional GitHub output file receives the same keys only after all checks succeed.
Before upload, an absent version or an existing byte-identical subset is allowed
for immutable retries. Conflicting, extra, malformed or yanked indexed files
fail validation; a network/server error is never treated as an absent version.
After upload, check each provider against its exact local wheel matrix:

```bash
python -I vane-extension-ci-tools/scripts/vane_provider_release.py verify-index \
  --index testpypi \
  --manifest vane-extension.toml --extension-root . \
  --vane-source ../vane --ci-tools-version "$CI_TOOLS_REVISION" \
  --config vane-provider-release.toml \
  --directory dist/providers \
  --provider iceberg \
  --version "$ICEBERG_VERSION"
```

Use `--index pypi` to verify a formal publication. These two named indexes are
the only supported endpoints; there is no custom URL, automatic index selection,
redirect following, or fallback. Post-upload verification retries a bounded
number of times and requires all filenames and SHA-256 digests to match, not
merely that the version exists.
It does not upload, install packages, load extensions or use signing secrets.

### Formal release promotion

Build and sign a new formal candidate against the exact released Vane runtime.
Do not rename a development wheel or reuse the TestPyPI development signing key.
The production signer must be explicitly trusted by the selected official Vane
runtime. Keep provider versions generated by Vane's descriptor-bound builder;
an extension repository tag does not relabel the wheels.

Before staging, run `validate --channel release --vane-version <version>` with
the same source and matrix arguments above. Repeat `--require-publishable-on`
for `testpypi` and `pypi` to check both indexes for immutable retry conflicts.
Upload the exact approved candidate to TestPyPI, verify the indexed wheels, and
pass the extension's local and two-worker Ray smoke tests.

After those tests and the protected production environment's approval, recheck
the downloaded candidate before the top-level PyPI upload job:

```bash
python -I vane-extension-ci-tools/scripts/vane_provider_release.py verify-promotion \
  --manifest vane-extension.toml --extension-root . \
  --vane-source ../vane --ci-tools-version "$CI_TOOLS_REVISION" \
  --config vane-provider-release.toml \
  --directory dist/providers \
  --vane-version 0.2.0 \
  --github-output "$GITHUB_OUTPUT"
```

This command rejects development Vane versions and validates the complete local
matrix and exact dependency graph. Every configured provider must already be
fully indexed on TestPyPI with identical filenames and SHA-256 digests. Only
then does it check PyPI, where an absent version or byte-identical partial upload
is accepted for a retry. Missing, partial, changed or yanked TestPyPI candidates
cannot be promoted, and a conflicting PyPI release cannot be replaced. Both
indexes are compared with one local hash snapshot, and the local wheel set is
checked again for changes before success. Outputs are emitted only after all
gates succeed. The command does not authorize an
upload or attest that smoke tests ran; the caller must enforce those job
dependencies, approvals and production signing checks separately.

Upload the same saved wheel files without rebuilding or modifying them, then
run `verify-index --index pypi` for each provider. Publish dependencies first:
for example, Avro must be available before Iceberg. Keep the complete candidate
set together for promotion validation even when upload jobs publish one provider
at a time.

The caller's release workflow must validate that its selected tag resolves to
the reviewed commit reachable from the protected release branch, retain exact
Vane and tooling pins, and scope environment access to its authorized refs.
Register TestPyPI and PyPI Trusted Publishers independently, using the actual
top-level workflow filename and matching protected environment. This shared
tool does not configure environments, create tags, generate keys, or add
production trust to Vane.

When adopting this tools revision, update callers together with their exact
gitlink/workflow pin: `validate` now requires `--channel`, the old
`--require-testpypi-publishable` flag is replaced by
`--require-publishable-on testpypi`, and `verify-index` requires `--index`.
There are no compatibility aliases or manifest schema changes.

This release gate is **not** a replacement for the exact Vane checkout's
`scripts/build_extension_wheel.py` and `scripts/verify_extension_wheel.py`:
descriptor, trust/signature, SourceID, ELF, license, archive safety and clean
runtime verification remain there. Extension-specific native builders and local
and two-worker Ray smoke tests remain in the extension repositories.

Keep the final TestPyPI and PyPI upload jobs in the top-level publisher workflow.
PyPI
[does not currently support a reusable workflow as a Trusted Publisher](https://docs.pypi.org/trusted-publishers/troubleshooting/#reusable-workflows-on-github).
Sharing these checks does not require moving signing keys, environments or
publisher registrations. A central release coordinator, reusable build adapters
and migration of additional extensions can build on this gate separately.

## Development

Run the self-contained test suite with Python 3.11 or newer:

```bash
python -m pip install -r requirements-release.txt
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```
