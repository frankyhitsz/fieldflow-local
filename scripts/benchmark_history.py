"""Measure current dispatch separately from explicit whole-history audit."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import tempfile
import time
from pathlib import Path

from backend.storage import Store


def benchmark(root: Path) -> list[dict]:
    database = root / "history.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    scenario.id = "history-benchmark"
    scenario.work_orders = scenario.work_orders[:1]
    scenario.technicians = scenario.technicians[:1]
    scenario.locked_assignments = []
    store.save_scenario(scenario, "history benchmark")
    rows = []
    for size in (10, 1000, 10000):
        while scenario.revision < size - 1:
            previous = scenario.revision
            scenario.revision += 1
            scenario.description = f"revision {scenario.revision}"
            store.save_scenario(scenario, "benchmark edit", expected_revision=previous)
        first = time.perf_counter()
        store.dispatch_snapshot(scenario.id)
        cold_ms = (time.perf_counter() - first) * 1000
        warm, queries = [], []
        for _ in range(15):
            statements = []
            original = store._unlocked_connect
            from contextlib import contextmanager

            @contextmanager
            def traced(connect=original, sink=statements):
                with connect() as con:
                    con.set_trace_callback(sink.append)
                    yield con

            store._unlocked_connect = traced
            started = time.perf_counter()
            snapshot = store.dispatch_snapshot(scenario.id)
            warm.append((time.perf_counter() - started) * 1000)
            store._unlocked_connect = original
            assert snapshot.revision_history_integrity.value == "VERIFIED"
            queries.append(len(statements))
        with store._connect() as con:
            start = time.perf_counter()
            con.execute("BEGIN IMMEDIATE")
            write_lock_ms = (time.perf_counter() - start) * 1000
            blob_bytes = con.execute(
                "SELECT COALESCE(SUM(LENGTH(compressed_payload)),0) FROM artifact_blobs"
            ).fetchone()[0]
        started = time.perf_counter()
        Store(database, allow_migration=False)
        open_ms = (time.perf_counter() - started) * 1000
        rows.append(
            {
                "revisions": size,
                "cold_dispatch_ms": round(cold_ms, 3),
                "p50_dispatch_ms": round(statistics.median(warm), 3),
                "p95_dispatch_ms": round(sorted(warm)[-2], 3),
                "queries_per_dispatch": max(queries),
                "worker_open_ms": round(open_ms, 3),
                "write_lock_ms": round(write_lock_ms, 3),
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if platform.system() == "Darwin" else 1024),
                "db_bytes": database.stat().st_size,
                "wal_bytes": Path(str(database) + "-wal").stat().st_size
                if Path(str(database) + "-wal").exists()
                else 0,
                "blob_bytes": blob_bytes,
            }
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fieldflow-history-") as directory:
        results = benchmark(Path(directory))
    report = {"platform": platform.platform(), "python": platform.python_version(), "samples": results}
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    # Structural budget: growing history must not add per-refresh SQL work.
    assert results[-1]["queries_per_dispatch"] == results[0]["queries_per_dispatch"]


if __name__ == "__main__":
    main()
