# Agent guidelines

This repository owns only Vane-specific orchestration for external DuckDB
extensions. Do not copy or modify DuckDB's generic extension distribution and
deployment implementation here.

## Invariants

- Require exact Vane and CI-tools commit SHAs.
- Treat `AstroVela/vane` as the source of `external/duckdb`.
- Never download or install a DuckDB extension at Ray runtime.
- Keep every public Make target prefixed with `vane_`.
- Do not add compatibility selection or a fallback DuckDB checkout.
- Keep reusable workflows read-only and pin third-party actions by commit SHA.

## Tests

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/vane_extension.py tests/test_vane_extension.py
```
