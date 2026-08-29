import importlib
import json
import sqlite3
import zlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.hashing import content_hash


def _client(monkeypatch, database, **kwargs):
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    return TestClient(main_module.app, **kwargs)


def test_content_addressed_artifact_round_trip_prune_and_cli(monkeypatch, tmp_path, capsys):
    database = tmp_path / "artifact-blobs.db"
    with _client(monkeypatch, database) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        analysis = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "RISK", "request": {"seed": 51, "trials": 50}},
        )
        assert analysis.status_code == 201, analysis.text
        run = analysis.json()

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT payload, artifact_blob_hash FROM decision_analysis_artifacts "
            "WHERE analysis_run_id=? ORDER BY option_id LIMIT 1",
            (run["id"],),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == {"artifact_blob_hash": row[1]}
        blob_hash = str(row[1])

    export = tmp_path / "artifact.json"
    assert cli_main(["--database", str(database), "artifacts", "export", blob_hash, str(export)]) == 0
    assert content_hash(json.loads(export.read_text(encoding="utf-8"))) == blob_hash
    capsys.readouterr()

    orphan_value = {"orphan": True}
    orphan_text = json.dumps(orphan_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    orphan_hash = content_hash(orphan_value)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO artifact_blobs(content_hash, codec, compressed_payload, uncompressed_size, created_at) "
            "VALUES (?, 'ZLIB_JSON_V1', ?, ?, '2000-01-01T00:00:00+00:00')",
            (orphan_hash, zlib.compress(orphan_text.encode(), level=9), len(orphan_text.encode())),
        )
    assert cli_main(["--database", str(database), "artifacts", "prune", "--retention-days", "0", "--apply"]) == 0
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT 1 FROM artifact_blobs WHERE content_hash=?", (orphan_hash,)).fetchone() is None
        )
        assert (
            connection.execute("SELECT 1 FROM artifact_blobs WHERE content_hash=?", (blob_hash,)).fetchone() is not None
        )


def test_risk_comparison_saga_resumes_after_second_child_crash(monkeypatch, tmp_path):
    database = tmp_path / "risk-saga-resume.db"
    with _client(monkeypatch, database, raise_server_exceptions=False) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        assert (
            client.post(
                "/api/scenarios/main/optimize", json={"strategy": "balanced", "time_limit_seconds": 1}
            ).status_code
            == 200
        )
        plans = client.get("/api/scenarios/main/plan-versions").json()

        import backend.main as main_module

        original = main_module.execute_decision_analysis_run
        calls = 0

        def crash_second_child(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected after-child crash")
            return original(*args, **kwargs)

        monkeypatch.setattr(main_module, "execute_decision_analysis_run", crash_second_child)
        endpoint = f"/api/scenarios/main/risk-comparison?before={plans[0]['id']}&after={plans[1]['id']}"
        payload = {"analysis_scope": "EX_ANTE_FROZEN_PLAN", "seed": 83, "trials": 50}
        first = client.post(endpoint, headers={"Idempotency-Key": "risk-saga-resume-001"}, json=payload)
        assert first.status_code == 500
        with closing(sqlite3.connect(database)) as connection:
            failed = connection.execute(
                "SELECT status, before_analysis_id, after_analysis_id FROM risk_comparison_sagas"
            ).fetchone()
        assert failed[0] == "FAILED" and failed[1] and failed[2] is None

        monkeypatch.setattr(main_module, "execute_decision_analysis_run", original)
        resumed = client.post(endpoint, headers={"Idempotency-Key": "risk-saga-resume-001"}, json=payload)
        assert resumed.status_code == 200, resumed.text
        with closing(sqlite3.connect(database)) as connection:
            completed = connection.execute(
                "SELECT status, before_analysis_id, after_analysis_id, comparison_id FROM risk_comparison_sagas"
            ).fetchone()
        assert completed[0] == "COMPARISON_COMPLETED"
        assert completed[1] == failed[1] and completed[2] and completed[3] == resumed.json()["id"]


def test_restore_keeps_run_base_identity_and_binds_all_manifests(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path / "restore-manifests.db") as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        optimized = client.post(
            "/api/scenarios/main/optimize",
            json={"strategy": "balanced", "time_limit_seconds": 1},
        ).json()
        plans = client.get("/api/scenarios/main/plan-versions").json()
        source = next(item for item in plans if item["selected"]["id"] == baseline["id"])
        active = next(item for item in plans if item["selected"]["id"] == optimized["id"])
        preview = client.get(f"/api/scenarios/main/plan-versions/{source['id']}/rollback-preview").json()
        restored_response = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/restore",
            json={
                "expected_revision": 0,
                "expected_active_plan_version_id": active["id"],
                "confirmation_token": preview["confirmation_token"],
                "reason": "清单验收",
                "allow_delete_new_orders": False,
                "idempotency_key": "restore-manifest-proof-001",
            },
        )
        assert restored_response.status_code == 200, restored_response.text
        restored = restored_response.json()
        assert restored["data_revision"] == 1

        runs = client.get("/api/scenarios/main/schedule-runs").json()
        restore_run = next(item for item in runs if item["action"] == "restore")
        assert restore_run["scenario_revision"] == 0
        assert restore_run["target_scenario_revision"] == 1
        assert restore_run["input_manifest"]["command_base_scenario_revision"] == 0
        assert restore_run["input_manifest"]["target_scenario_revision"] == 1
        assert restore_run["restore_transform_manifest"]["target_scenario_hash"] == restored["scenario_snapshot_hash"]
        assert restore_run["result_manifest"]["candidate_manifest_hash"]

        candidate = client.get(f"/api/scenarios/main/schedule-candidates/{restore_run['candidate_id']}").json()
        assert (
            candidate["candidate_manifest"]["run_input_manifest_hash"] == restore_run["input_manifest"]["manifest_hash"]
        )
        assert (
            candidate["restore_transform_manifest"]["manifest_hash"]
            == restore_run["restore_transform_manifest"]["manifest_hash"]
        )
        artifact = restored["publication_verification_artifact"]
        assert artifact["run_input_manifest_hash"] == restore_run["input_manifest"]["manifest_hash"]
        assert artifact["run_result_manifest_hash"] == restore_run["result_manifest"]["manifest_hash"]
        assert artifact["candidate_manifest_hash"] == candidate["candidate_manifest"]["manifest_hash"]


def test_run_input_and_candidate_are_database_immutable(monkeypatch, tmp_path):
    database = tmp_path / "manifest-triggers.db"
    with _client(monkeypatch, database) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        run = client.get("/api/scenarios/main/schedule-runs").json()[0]
        candidate_id = run["candidate_id"]

    with closing(sqlite3.connect(database)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="input identity is immutable"):
            connection.execute(
                "UPDATE schedule_runs SET scenario_revision=scenario_revision + 1 WHERE id=?",
                (run["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="schedule candidate is immutable"):
            connection.execute(
                "UPDATE schedule_candidates SET payload=json_set(payload, '$.publishable', 0) WHERE id=?",
                (candidate_id,),
            )


def test_v2_plan_commands_require_explicit_active_plan_precondition(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path / "v2-precondition.db") as client:
        missing = client.post("/api/v2/scenarios/main/baseline", json={})
        assert missing.status_code == 428
        assert missing.json()["detail"]["code"] == "ACTIVE_PLAN_PRECONDITION_REQUIRED"

        baseline = client.post(
            "/api/v2/scenarios/main/baseline",
            json={"expected_active_plan_version_id": None},
        )
        assert baseline.status_code == 200, baseline.text
        active = client.get("/api/scenarios/main/plan-versions").json()[0]

        missing_optimize = client.post(
            "/api/v2/scenarios/main/optimize",
            json={"strategy": "balanced", "time_limit_seconds": 1},
        )
        assert missing_optimize.status_code == 428
        optimized = client.post(
            "/api/v2/scenarios/main/optimize",
            json={
                "strategy": "balanced",
                "time_limit_seconds": 1,
                "expected_active_plan_version_id": active["id"],
            },
        )
        assert optimized.status_code == 200, optimized.text


def test_report_modes_separate_frozen_metrics_from_current_coverage(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path / "report-v2.db") as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        scenario = client.get("/api/scenarios/main").json()
        new_order = dict(scenario["work_orders"][0])
        new_order.pop("status", None)
        new_order.update(
            {
                "id": "WO-REPORT-NEW",
                "customer_name": "报告新需求",
                "title": "临时检修",
                "is_emergency": False,
                "reported_at": None,
            }
        )
        created = client.post(
            "/api/v2/scenarios/main/work-orders",
            headers={"If-Match": '"D000"'},
            json=new_order,
        )
        assert created.status_code == 200, created.text

        frozen = client.get(f"/api/scenarios/main/plan-versions/{plan['id']}/report")
        assert frozen.status_code == 200
        assert "FROZEN_PLAN_REPORT" in frozen.text
        assert "只反映该版本发布时的状态" in frozen.text
        assert "D000" in frozen.text

        current = client.get(
            f"/api/scenarios/main/report?schedule_id={plan['selected']['id']}&mode=CURRENT_OPERATIONAL_REPORT"
        )
        assert current.status_code == 200, current.text
        assert "CURRENT_OPERATIONAL_REPORT" in current.text
        assert "D001" in current.text
        assert "新增未覆盖</span><b>1" in current.text


def test_dispatch_reports_head_and_full_history_integrity_separately(monkeypatch, tmp_path):
    database = tmp_path / "revision-history-status.db"
    with _client(monkeypatch, database) as client:
        scenario = client.get("/api/scenarios/main").json()
        order = scenario["work_orders"][0]
        assert (
            client.put(
                f"/api/v2/scenarios/main/work-orders/{order['id']}",
                headers={"If-Match": '"D000"'},
                json={"note": "形成第二个修订"},
            ).status_code
            == 200
        )

    with closing(sqlite3.connect(database)) as connection, connection:
        payload = connection.execute(
            "SELECT payload FROM scenario_revisions WHERE scenario_id='main' AND number=0"
        ).fetchone()[0]
        tampered = json.loads(payload)
        tampered["reason"] = "被篡改的旧历史"
        connection.execute(
            "UPDATE scenario_revisions SET payload=? WHERE scenario_id='main' AND number=0",
            (json.dumps(tampered, ensure_ascii=False),),
        )

    with _client(monkeypatch, database) as client:
        snapshot = client.get("/api/scenarios/main/dispatch-snapshot")
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["revision_head_integrity"] == "VERIFIED"
        assert body["revision_history_integrity"] == "FAILED"
        assert body["revision_history_issue_count"] >= 1


def test_rollback_preview_does_not_treat_latest_history_as_active(monkeypatch, tmp_path):
    database = tmp_path / "rollback-no-active.db"
    with _client(monkeypatch, database) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE scenarios SET active_plan_version_id=NULL WHERE id='main'")
        connection.execute("UPDATE plan_applicability SET active=0 WHERE scenario_id='main'")

    with _client(monkeypatch, database) as client:
        preview = client.get(f"/api/scenarios/main/plan-versions/{plan['id']}/rollback-preview")
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["current_plan_version_id"] is None
        assert body["current_plan_number"] is None
        assert body["changed_plan_work_orders"] == []


def test_command_relation_tamper_is_rejected_and_quarantined(monkeypatch, tmp_path):
    database = tmp_path / "command-quarantine.db"
    with _client(monkeypatch, database) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        request = {
            "technician_id": assignment["technician_id"],
            "occurred_at": assignment["start_time"],
            "expected_revision": 0,
            "idempotency_key": "command-relation-proof-001",
        }
        endpoint = f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start"
        assert client.post(endpoint, json=request).status_code == 200

        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE command_keys SET resource_id='EVENT-FORGED' WHERE key=?",
                (request["idempotency_key"],),
            )

        replay = client.post(endpoint, json=request)
        assert replay.status_code == 409
        assert replay.json()["detail"]["record_type"] == "COMMAND_RECORD"

    with closing(sqlite3.connect(database)) as connection:
        quarantined = connection.execute(
            "SELECT COUNT(*) FROM migration_orphans WHERE source_table='command_keys' AND source_id LIKE ?",
            (f"%:{request['idempotency_key']}",),
        ).fetchone()[0]
        assert quarantined == 1
