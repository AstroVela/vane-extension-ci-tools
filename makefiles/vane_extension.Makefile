# Vane-only targets for an out-of-tree DuckDB extension.
#
# This file is safe to include after DuckDB's duckdb_extension.Makefile: it does
# not replace generic build, test, formatting, or deployment targets.

VANE_EXTENSION_CI_TOOLS_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
VANE_EXTENSION_ROOT ?= $(CURDIR)
VANE_MANIFEST ?= $(VANE_EXTENSION_ROOT)/vane-extension.toml
VANE_SOURCE_DIR ?= $(VANE_EXTENSION_ROOT)/build/vane-source
VANE_NATIVE_BUILD_DIR ?= $(VANE_EXTENSION_ROOT)/build/vane-native
VANE_WHEEL_BUILD_DIR ?= $(VANE_EXTENSION_ROOT)/build/vane-wheel
VANE_WHEEL_DIST_DIR ?= $(VANE_WHEEL_BUILD_DIR)/dist
VANE_VCPKG_ROOT ?= $(VANE_SOURCE_DIR)/.cache/vcpkg
VANE_VCPKG_INSTALLED_DIR ?= $(VANE_SOURCE_DIR)/vcpkg_installed
VANE_BUILD_JOBS ?= 8
VANE_PYTHON ?= python3
VANE_SKIP_NATIVE_TESTS ?= 0
VCPKG_TARGET_TRIPLET ?= x64-linux
override _VANE_EXPECTED_CI_TOOLS_VERSION := $(shell \
	git -C "$(VANE_EXTENSION_ROOT)" \
		rev-parse "HEAD:vane-extension-ci-tools" 2>/dev/null)

VANE_EXTENSION_COMMAND = $(VANE_PYTHON) \
	"$(VANE_EXTENSION_CI_TOOLS_DIR)/scripts/vane_extension.py" \
	--manifest "$(VANE_MANIFEST)" \
	--extension-root "$(VANE_EXTENSION_ROOT)"

.PHONY: vane_verify_ci_tools vane_validate vane_prepare vane_identity vane_native vane_ci \
	vane_wheel_dependencies vane_wheel

vane_verify_ci_tools:
	$(VANE_EXTENSION_COMMAND) verify-ci-tools \
		--ci-tools-source "$(VANE_EXTENSION_CI_TOOLS_DIR)" \
		--expected-sha "$(_VANE_EXPECTED_CI_TOOLS_VERSION)"

vane_validate: vane_verify_ci_tools
	$(VANE_EXTENSION_COMMAND) manifest

vane_prepare: vane_validate
	$(VANE_EXTENSION_COMMAND) prepare --vane-source "$(VANE_SOURCE_DIR)"

vane_identity: vane_prepare
	$(VANE_EXTENSION_COMMAND) identity --vane-source "$(VANE_SOURCE_DIR)"

vane_native: vane_prepare
	$(VANE_EXTENSION_COMMAND) native \
		--vane-source "$(VANE_SOURCE_DIR)" \
		--build-dir "$(VANE_NATIVE_BUILD_DIR)" \
		--jobs "$(VANE_BUILD_JOBS)" \
		$(if $(filter 1,$(VANE_SKIP_NATIVE_TESTS)),--skip-tests,)

vane_ci: vane_native

vane_wheel_dependencies: vane_prepare
	@case "$(VANE_BUILD_JOBS)" in \
		''|*[!0-9]*|0) echo "VANE_BUILD_JOBS must be a positive integer" >&2; exit 2 ;; \
	esac
	@test "$(VCPKG_TARGET_TRIPLET)" = "x64-linux" || \
		{ echo "VCPKG_TARGET_TRIPLET must be x64-linux" >&2; exit 2; }
	@test "$$(uname -s)" = "Linux" && test "$$(uname -m)" = "x86_64" && \
		test "$$(getconf LONG_BIT)" = "64" || \
		{ echo "Vane wheel builds require 64-bit x86 Linux" >&2; exit 2; }
	VCPKG_ROOT="$(VANE_VCPKG_ROOT)" \
	VCPKG_INSTALLED_DIR="$(VANE_VCPKG_INSTALLED_DIR)" \
	VCPKG_TARGET_TRIPLET="$(VCPKG_TARGET_TRIPLET)" \
	VCPKG_MAX_CONCURRENCY="$(VANE_BUILD_JOBS)" \
	bash "$(VANE_SOURCE_DIR)/scripts/bootstrap_vcpkg.sh" "$(VANE_SOURCE_DIR)"

vane_wheel: vane_wheel_dependencies
	VANE_VCPKG_INSTALLED_DIR="$(VANE_VCPKG_INSTALLED_DIR)" \
	VCPKG_MAX_CONCURRENCY="$(VANE_BUILD_JOBS)" \
	$(VANE_EXTENSION_COMMAND) wheel \
		--vane-source "$(VANE_SOURCE_DIR)" \
		--build-dir "$(VANE_WHEEL_BUILD_DIR)" \
		--dist-dir "$(VANE_WHEEL_DIST_DIR)" \
		--jobs "$(VANE_BUILD_JOBS)"
