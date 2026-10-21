#!/usr/bin/env python3
"""Qualify signed installed providers with local UDFs, without starting Ray."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import traceback
from pathlib import Path

CONNECTORS = ("lance", "paimon", "ducklake", "iceberg", "vortex")
ROWS = 4101
OFFSET = 10000
TRACE: Path | None = None


def quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def observe(kind: str, **details: object) -> None:
    if TRACE is not None:
        with TRACE.open("a") as stream:
            stream.write(
                json.dumps({"kind": kind, "pid": os.getpid(), **details}) + "\n"
            )


def transform(batch):
    import pyarrow as pa
    import pyarrow.compute as pc

    observe("batch", rows=batch.num_rows)
    return pa.table(
        {
            "id": pc.add(batch.column("id"), pa.scalar(OFFSET, type=pa.int32())),
            "payload": batch.column("payload"),
        }
    )


class TransformActor:
    def __init__(self):
        observe("actor_open")

    def __call__(self, batch):
        return transform(batch)

    def _vane_close(self):
        observe("actor_close")


def fail_after_batch(batch):
    if batch.column("id")[0].as_py() >= 127:
        raise ValueError("local connector injected failure")
    return transform(batch)


class FailingActor(TransformActor):
    def __call__(self, batch):
        return fail_after_batch(batch)


def check_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def check_provider(name: str, trust: str, version: str, source_id: str) -> dict:
    entries = [
        entry
        for entry in importlib.metadata.entry_points(
            group="vane.dynamic_extension_providers"
        )
        if entry.name == name
    ]
    if len(entries) != 1:
        raise AssertionError(
            f"expected one installed {name} provider, found {len(entries)}"
        )
    descriptor = importlib.import_module(entries[0].module).descriptor()
    check_equal(descriptor.name, name, "provider name")
    check_equal(descriptor.trust_identity, trust, "provider trust identity")
    check_equal(descriptor.vane_version, version, "provider runtime version")
    check_equal(descriptor.duckdb_source_id, source_id, "provider native SourceID")
    return {
        "name": name,
        "identity": descriptor.identity,
        "sha256": descriptor.sha256,
        "dependencies": [dependency.name for dependency in descriptor.dependencies],
    }


def run_case(args) -> dict:
    global TRACE
    root = args.work_directory.resolve()
    TRACE = root / "workers.jsonl"
    os.environ["VANE_RUNNER"] = args.runner
    import ray
    import vane
    from vane.execution import ref_bundle

    check_equal(ray.is_initialized(), False, "initial Ray state")

    def forbid_ray(*_args, **_kwargs):
        raise AssertionError(
            "local connector qualification attempted to initialize Ray"
        )

    ray.init = forbid_ray
    config = {
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
        "extension_directory": str(root / "extension-cache"),
    }
    con = vane.connect(config=config)
    original = ref_bundle.materialize_ref_bundle
    materializations = []

    def materialize(refs, *positional, **keywords):
        materializations.append([type(ref).__name__ for ref in refs])
        return original(refs, *positional, **keywords)

    ref_bundle.materialize_ref_bundle = materialize
    result = {
        "connector": args.connector,
        "runner": args.runner,
        "backend": args.backend,
        "vane_revision": args.vane_revision,
        "ci_tools_version": args.ci_tools_version,
        "operations": [],
        "providers": {},
    }
    try:
        result["engine"] = con.execute("PRAGMA version").fetchone()
        version = importlib.metadata.version("vane-ai")
        pending = [args.connector]
        while pending:
            name = pending.pop()
            if name in result["providers"]:
                continue
            provider = check_provider(
                name, args.trust_identity, version, result["engine"][1]
            )
            result["providers"][name] = provider
            pending.extend(provider["dependencies"])
        check_equal(
            con.execute(
                "SELECT current_setting('allow_unsigned_extensions'), "
                "current_setting('autoinstall_known_extensions'), "
                "current_setting('autoload_known_extensions')"
            ).fetchone(),
            (False, False, False),
            "extension security settings",
        )
        vane.load_installed_extension(args.connector, connection=con)
        for name in result["providers"]:
            check_equal(
                con.execute(
                    "SELECT loaded, installed, install_mode FROM duckdb_extensions() "
                    "WHERE extension_name = ?",
                    [name],
                ).fetchone(),
                (True, False, "NOT_INSTALLED"),
                "provider-only extension loading",
            )
        if args.connector == "lance":
            (root / "warehouse").mkdir()
            con.execute(f"ATTACH {quote(root / 'warehouse')} AS target (TYPE lance)")
            prefix = "target.main"
        elif args.connector == "paimon":
            (root / "warehouse").mkdir()
            con.execute(f"ATTACH {quote(root / 'warehouse')} AS target (TYPE paimon)")
            con.execute("CREATE SCHEMA target.probe")
            prefix = "target.probe"
        elif args.connector == "ducklake":
            attach = (
                f"ATTACH {quote('ducklake:sqlite:' + str(root / 'metadata.sqlite'))} AS target "
                f"(DATA_PATH {quote(root / 'data')}, DATA_INLINING_ROW_LIMIT 0)"
            )
            con.execute(attach)
            prefix = "target.main"
        else:
            prefix = ""

        source_sql = (
            "SELECT i::INTEGER AS id, CASE WHEN i % 3 = 0 THEN NULL ELSE 'row-' || i END AS payload "
            f"FROM range({ROWS}) t(i)"
        )

        def source(udf: bool, *, empty=False, failing=False):
            relation = con.sql(source_sql + (" WHERE false" if empty else ""))
            if not udf:
                return relation
            function = (
                (FailingActor if args.backend == "actor" else fail_after_batch)
                if failing
                else (TransformActor if args.backend == "actor" else transform)
            )
            options = {
                "schema": {
                    "id": vane.sqltypes.INTEGER,
                    "payload": vane.sqltypes.VARCHAR,
                },
                "batch_size": 127,
                "cpus": 1,
                "gpus": 0,
                "execution_backend": f"subprocess_{args.backend}",
            }
            if args.backend == "actor":
                options["actor_number"] = 1
            return relation.map_batches(function, **options)

        expected = [(i, None if i % 3 == 0 else f"row-{i}") for i in range(ROWS)]
        transformed = [(i + OFFSET, text) for i, text in expected]
        check_equal(
            sorted(source(True).fetchall()), transformed, "direct local UDF control"
        )
        con.execute("CREATE TABLE local_control(id INTEGER, payload VARCHAR)")
        source(True).insert_into("local_control")
        check_equal(
            con.execute("SELECT * FROM local_control ORDER BY id").fetchall(),
            transformed,
            "ordinary DuckDB UDF INSERT control",
        )
        modes = ("insert", "ctas") if prefix else ("copy",)
        persisted = []

        def write(label: str, mode: str, *, udf=False, empty=False, failing=False):
            relation = source(udf, empty=empty, failing=failing)
            relation.create_view("probe_input", replace=True)
            target = f"{prefix}.{label}" if prefix else root / label
            if mode == "insert":
                con.execute(f"CREATE TABLE {target}(id INTEGER, payload VARCHAR)")
                relation.insert_into(target)
            elif mode == "ctas":
                relation.create(target)
            elif args.connector == "vortex":
                target = root / f"{label}.vortex"
                con.execute(
                    f"COPY (SELECT * FROM probe_input) TO {quote(target)} (FORMAT vortex)"
                )
            else:
                con.execute(
                    f"COPY (SELECT * FROM probe_input) TO {quote(target)} "
                    f"(FORMAT iceberg, DATA_FORMAT {args.iceberg_data_format})"
                )
            return target

        def read(target):
            if prefix:
                query = str(target)
            elif args.connector == "vortex":
                query = f"read_vortex({quote(target)})"
                if not target.is_file() or target.stat().st_size == 0:
                    raise AssertionError("COPY did not write an actual Vortex file")
            else:
                query = f"iceberg_scan({quote(target)})"
            return con.execute(
                f"SELECT id, payload FROM {query} ORDER BY id"
            ).fetchall()

        for mode in modes:
            for kind, udf, empty in (
                ("plain", False, False),
                ("udf", True, False),
                ("empty", True, True),
            ):
                label = f"{kind}_{mode}"
                materializations.clear()
                target = write(label, mode, udf=udf, empty=empty)
                wanted = [] if empty else transformed if udf else expected
                check_equal(read(target), wanted, label)
                if udf and not empty and not materializations:
                    raise AssertionError(
                        "connector write did not consume host materialized local UDF output"
                    )
                if args.connector == "iceberg" and not empty:
                    check_equal(
                        con.execute(
                            f"SELECT DISTINCT file_format FROM iceberg_metadata({quote(target)})"
                        ).fetchall(),
                        [(args.iceberg_data_format,)],
                        "actual Iceberg data file format",
                    )
                persisted.append((target, wanted))
                result["operations"].append(
                    {
                        "name": label,
                        "rows": len(wanted),
                        "materializations": len(materializations),
                    }
                )

            label = f"failed_{mode}"
            try:
                write(label, mode, udf=True, failing=True)
            except Exception as error:
                if "local connector injected failure" not in str(error):
                    raise
            else:
                raise AssertionError("injected worker failure was swallowed")
            if mode == "insert":
                check_equal(
                    read(f"{prefix}.{label}"),
                    [],
                    "failed INSERT published no partial rows",
                )
            elif mode == "ctas" and args.connector == "paimon":
                # Paimon's documented failure contract retains its prepared
                # table. Require no committed data, then retry after explicit
                # cleanup, as its existing distributed write tests do.
                target = f"{prefix}.{label}"
                table_path = root / "warehouse" / "probe.db" / label
                check_equal(
                    read(target), [], "failed Paimon CTAS retained an empty table"
                )
                snapshot_sql = (
                    f"SELECT count(*) FROM paimon_snapshots({quote(table_path)})"
                )
                check_equal(
                    con.execute(snapshot_sql).fetchone(),
                    (0,),
                    "failed Paimon CTAS snapshots",
                )
                con.execute(f"DROP TABLE {target}")
                if table_path.exists():
                    raise AssertionError(
                        "explicit Paimon CTAS cleanup did not remove the retained target"
                    )
                retried = write(label, mode, udf=True)
                check_equal(
                    read(retried),
                    transformed,
                    "Paimon CTAS retry after explicit cleanup",
                )
                check_equal(
                    con.execute(snapshot_sql).fetchone(),
                    (1,),
                    "Paimon CTAS retry snapshots",
                )
                persisted.append((retried, transformed))
                result["operations"].append(
                    {"name": "explicit_cleanup_retry_ctas", "rows": len(transformed)}
                )
            elif mode == "ctas":
                check_equal(
                    con.execute(
                        "SELECT count(*) FROM duckdb_tables() WHERE database_name='target' "
                        "AND table_name=?",
                        [label],
                    ).fetchone(),
                    (0,),
                    "failed CTAS published no table",
                )
            elif args.connector == "iceberg":
                if list((root / label).glob("metadata/*metadata.json")):
                    raise AssertionError("failed Iceberg COPY published a snapshot")
            elif (root / f"{label}.vortex").exists():
                raise AssertionError("failed Vortex COPY published a final output file")
            result["operations"].append({"name": label, "failure_propagated": True})
            check_equal(
                con.execute("SELECT 1").fetchone(),
                (1,),
                "connection remains usable after failure",
            )

        con.close()
        con = vane.connect(config=config)
        vane.load_installed_extension(args.connector, connection=con)
        if prefix:
            if args.connector == "ducklake":
                con.execute(attach)
            else:
                con.execute(
                    f"ATTACH {quote(root / 'warehouse')} AS target (TYPE {args.connector})"
                )
        for target, wanted in persisted:
            check_equal(read(target), wanted, "reopened connector readback")
        result["ok"] = True
    except Exception as error:  # noqa: BLE001 - preserve connector diagnostics and fail the case
        result.update(ok=False, error=str(error), traceback=traceback.format_exc())
    finally:
        con.close()
        vane.teardown_runner()
        ref_bundle.materialize_ref_bundle = original
    check_equal(ray.is_initialized(), False, "final Ray state")
    result["workers"] = (
        [json.loads(line) for line in TRACE.read_text().splitlines()]
        if TRACE.exists()
        else []
    )
    if result.get("ok"):
        opens = [
            event["pid"] for event in result["workers"] if event["kind"] == "actor_open"
        ]
        closes = [
            event["pid"]
            for event in result["workers"]
            if event["kind"] == "actor_close"
        ]
        check_equal(sorted(opens), sorted(closes), "actor lifecycle cleanup")
    return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connector", choices=CONNECTORS, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--trust-identity", required=True)
    parser.add_argument("--vane-revision", required=True)
    parser.add_argument("--ci-tools-version", required=True)
    parser.add_argument(
        "--iceberg-data-format", choices=("parquet", "vortex"), default="parquet"
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--runner", choices=("local", "local-fast"), help=argparse.SUPPRESS
    )
    parser.add_argument("--backend", choices=("task", "actor"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not all(
        re.fullmatch("[0-9a-f]{40}", value)
        for value in (args.vane_revision, args.ci_tools_version)
    ):
        parser.error("Vane and CI tools revisions must be exact lowercase commit SHAs")
    if not args.trust_identity or args.timeout <= 0:
        parser.error("an explicit trust identity and a positive timeout are required")
    if bool(args.runner) != bool(args.backend):
        parser.error("internal cases require both runner and backend")
    tools_root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "-C", str(tools_root), "rev-parse", "HEAD"], text=True
    ).strip()
    check_equal(revision, args.ci_tools_version, "exact CI tools revision")
    dirty = subprocess.check_output(
        [
            "git",
            "-C",
            str(tools_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        text=True,
    ).strip()
    check_equal(dirty, "", "clean CI tools source")
    return args


def main() -> int:
    args = parse_args()
    args.work_directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    if args.runner:
        try:
            result = run_case(args)
        except Exception as error:  # noqa: BLE001 - preserve setup/cleanup diagnostics and fail the case
            result = {
                "ok": False,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        (args.work_directory / "result.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
        return 0 if result["ok"] else 1
    results = []
    for runner in ("local", "local-fast"):
        for backend in ("task", "actor"):
            directory = args.work_directory / f"{runner}-{backend}"
            command = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--connector",
                args.connector,
                "--work-directory",
                str(directory),
                "--trust-identity",
                args.trust_identity,
                "--vane-revision",
                args.vane_revision,
                "--ci-tools-version",
                args.ci_tools_version,
                "--iceberg-data-format",
                args.iceberg_data_format,
                "--runner",
                runner,
                "--backend",
                backend,
            ]
            with (args.work_directory / f"{runner}-{backend}.log").open("w") as stream:
                process = subprocess.Popen(
                    command,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    returncode = process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    returncode = -1
            path = directory / "result.json"
            result = (
                json.loads(path.read_text())
                if path.is_file()
                else {"ok": False, "error": "case did not complete"}
            )
            if returncode != 0:
                result["ok"] = False
            result.update(runner=runner, backend=backend, returncode=returncode)
            results.append(result)
            print(
                json.dumps(
                    {
                        "runner": runner,
                        "backend": backend,
                        "ok": result["ok"],
                        "error": result.get("error"),
                    }
                ),
                flush=True,
            )
    (args.work_directory / "matrix.json").write_text(
        json.dumps(results, indent=2) + "\n"
    )
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
