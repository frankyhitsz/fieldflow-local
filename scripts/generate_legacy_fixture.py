"""Run with PYTHONPATH pointing at an unmodified historical checkout.

Example: PYTHONPATH=/tmp/fieldflow-v0510 .venv/bin/python scripts/generate_legacy_fixture.py output.sqlite.gz
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="fieldflow-legacy-") as temporary:
        database = Path(temporary) / "legacy.db"
        os.environ["FIELDFLOW_DB"] = str(database)
        from fastapi.testclient import TestClient

        import backend.main as application

        def checked(response):
            assert response.status_code < 300, response.text
            return response.json()

        with TestClient(application.app) as client:
            checked(client.post("/api/scenarios/main/baseline"))
            first = checked(client.get("/api/scenarios/main/plan-versions"))[0]
            checked(client.post("/api/scenarios/main/baseline"))
            second = checked(client.get("/api/scenarios/main/plan-versions"))[-1]
            endpoint = f"/api/scenarios/main/plan-versions/{first['id']}/analysis-runs"
            for body in (
                {"analysis_type": "COST"},
                {"analysis_type": "CAPACITY"},
                {"analysis_type": "RISK", "request": {"seed": 17, "trials": 50}},
                {
                    "analysis_type": "RISK",
                    "request": {"seed": 18, "trials": 50, "artifact_detail_policy": "FULL_TRIAL_DETAIL"},
                },
            ):
                analysis = checked(client.post(endpoint, json=body))
                assert analysis["integrity_status"] == "VERIFIED", analysis
            comparison = checked(
                client.post(
                    f"/api/scenarios/main/risk-comparison?before={first['id']}&after={second['id']}",
                    headers={"Idempotency-Key": "legacy-golden-comparison"},
                    json={"seed": 19, "trials": 50},
                )
            )
            assert comparison["integrity_status"] == "VERIFIED", comparison
            # Two real Booking identities: one completed and one still started.
            first_visits = [item for item in second["selected"]["assignments"] if item["sequence"] == 1][:2]
            assert len(first_visits) == 2
            for index, assignment in enumerate(first_visits):
                current = checked(client.get("/api/scenarios/main"))
                started = checked(
                    client.post(
                        f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
                        json={
                            "technician_id": assignment["technician_id"],
                            "occurred_at": assignment["start_time"],
                            "expected_revision": current["revision"],
                            "idempotency_key": f"golden-start-{index}",
                        },
                    )
                )
                if index == 0:
                    checked(
                        client.post(
                            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
                            json={
                                "technician_id": assignment["technician_id"],
                                "occurred_at": assignment["finish_time"],
                                "expected_revision": started["scenario"]["revision"],
                                "idempotency_key": "golden-complete-0",
                            },
                        )
                    )
        with closing(sqlite3.connect(database)) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            schema = con.execute("PRAGMA user_version").fetchone()[0]
            summary = {
                table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "plan_versions",
                    "decision_analysis_runs",
                    "decision_analysis_artifacts",
                    "risk_comparison_runs",
                    "work_order_execution_events",
                )
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(gzip.compress(database.read_bytes(), mtime=0))
        print(json.dumps({"schema": schema, "backend": application.__file__, "rows": summary, "output": str(output)}))


if __name__ == "__main__":
    main()
