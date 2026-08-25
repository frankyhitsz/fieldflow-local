from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.models import FieldImpact, WorkOrderStatus
from backend.storage import ActivePlanConflict, Store


def _create_payload(client: TestClient, *, order_id: str, emergency: bool = False) -> dict:
    source = client.get("/api/scenarios/main").json()["work_orders"][0]
    payload = {**source, "id": order_id, "is_emergency": emergency}
    payload.pop("status", None)
    payload["reported_at"] = payload["window_start"] if emergency else None
    return payload


def test_public_create_cannot_forge_execution_status(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "create-status.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        payload = _create_payload(client, order_id="WO-STATUS")
        for status in ("started", "completed", "pending"):
            forged = client.post(
                "/api/v2/scenarios/main/work-orders",
                headers={"If-Match": "D0"},
                json={**payload, "status": status},
            )
            assert forged.status_code == 422
        created = client.post(
            "/api/v2/scenarios/main/work-orders",
            headers={"If-Match": "D0"},
            json=payload,
        )
        assert created.status_code == 200
        assert next(item for item in created.json()["work_orders"] if item["id"] == "WO-STATUS")["status"] == (
            "pending"
        )


def test_emergency_intake_forces_pending_and_marks_all_applicability_axes(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency-applicability.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)

    def fail_replan(*_args, **_kwargs):
        raise RuntimeError("forced solver failure")

    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        monkeypatch.setattr(main_module, "replan_schedule", fail_replan)
        emergency = _create_payload(client, order_id="WO-EMERGENCY-FAILED", emergency=True)
        response = client.post(
            "/api/scenarios/main/replan",
            json={
                "planning_time": emergency["reported_at"],
                "emergency_order": emergency,
                "idempotency_key": "failed-emergency-replan-001",
            },
        )
        assert response.status_code == 500
        scenario = client.get("/api/scenarios/main").json()
        persisted = next(item for item in scenario["work_orders"] if item["id"] == emergency["id"])
        assert persisted["status"] == "pending"
        active = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        assert active["coverage_status"] == "PARTIAL_NEW_DEMAND"
        assert active["applicability"] == {
            "route_executable": True,
            "coverage_complete": False,
            "planning_current": False,
            "metrics_current": False,
            "commercial_current": True,
            "reoptimization_opportunity": False,
            "invalid_assignment_ids": [],
        }


def test_applicability_accumulates_invalid_assignments_and_recomputes_coverage(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "applicability-reducer.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        first, second = schedule["assignments"][:2]
        for revision, assignment in enumerate((first, second)):
            changed = client.put(
                f"/api/v2/scenarios/main/work-orders/{assignment['work_order_id']}",
                headers={"If-Match": f"D{revision}"},
                json={"service_duration": 47 + revision},
            )
            assert changed.status_code == 200
        active = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        assert active["applicability"]["invalid_assignment_ids"] == sorted(
            {first["work_order_id"], second["work_order_id"]}
        )
        assert active["applicability"]["route_executable"] is False

        first_new = _create_payload(client, order_id="WO-NEW-ONE")
        second_new = _create_payload(client, order_id="WO-NEW-TWO")
        assert (
            client.post("/api/v2/scenarios/main/work-orders", headers={"If-Match": "D2"}, json=first_new).status_code
            == 200
        )
        assert (
            client.post("/api/v2/scenarios/main/work-orders", headers={"If-Match": "D3"}, json=second_new).status_code
            == 200
        )
        deleted = client.delete(
            "/api/v2/scenarios/main/work-orders/WO-NEW-ONE",
            headers={"If-Match": "D4"},
        )
        assert deleted.status_code == 200
        active = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        assert active["coverage_status"] == "PARTIAL_NEW_DEMAND"
        assert active["applicability"]["coverage_complete"] is False


def test_v19_partial_coverage_migrates_to_conservative_multi_axis_projection(monkeypatch, tmp_path):
    database = tmp_path / "v19-applicability.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            UPDATE plan_applicability
            SET coverage_status='PARTIAL_NEW_DEMAND', route_executable=1, coverage_complete=1,
                planning_current=1, metrics_current=1, commercial_current=1,
                reoptimization_opportunity=0, invalid_assignment_ids='[]'
            """
        )
        connection.execute("PRAGMA user_version=19")
    migrated = Store(database).active_plan_version("main")
    assert migrated is not None
    assert migrated.coverage_status.value == "PARTIAL_NEW_DEMAND"
    assert migrated.applicability.route_executable is True
    assert migrated.applicability.coverage_complete is False
    assert migrated.applicability.planning_current is False
    assert migrated.applicability.metrics_current is False


def test_v20_malformed_applicability_json_is_quarantined_and_fails_closed(monkeypatch, tmp_path):
    database = tmp_path / "v20-malformed-applicability.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE plan_applicability SET invalid_assignment_ids='not-json'")
        connection.execute("PRAGMA user_version=20")

    migrated_store = Store(database)
    migrated = migrated_store.active_plan_version("main")
    assert migrated is not None
    assert migrated.coverage_status.value == "STALE_DATA_CHANGED"
    assert migrated.applicability.route_executable is False
    assert migrated.applicability.invalid_assignment_ids == []
    assert any(
        item["source_table"] == "plan_applicability"
        and item["source_id"] == migrated.id
        and item["reason"] == "v21 applicability constraint repair"
        for item in migrated_store.list_integrity_issues()
    )


def test_v2_writes_require_if_match_and_reject_stale_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "if-match.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        endpoint = "/api/v2/scenarios/main/work-orders/WO-1021"
        missing = client.put(endpoint, json={"note": "missing precondition"})
        assert missing.status_code == 428
        assert missing.json()["detail"]["code"] == "PRECONDITION_REQUIRED"
        accepted = client.put(endpoint, headers={"If-Match": "D0"}, json={"note": "first"})
        assert accepted.status_code == 200
        stale = client.put(endpoint, headers={"If-Match": "D0"}, json={"note": "stale"})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "SCENARIO_REVISION_CONFLICT"


def test_data_edit_cas_rejects_projection_for_a_different_active_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "active-plan-cas.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        stale_active_id = client.get("/api/scenarios/main/plan-versions").json()[0]["id"]
        client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1})
        store = main_module.require_store()
        scenario = store.get_scenario("main")
        assert scenario is not None
        scenario.work_orders[0].note = "must not commit"
        scenario.revision += 1
        with pytest.raises(ActivePlanConflict):
            store.save_scenario(
                scenario,
                "stale projection",
                expected_revision=0,
                preserve_active_plan=True,
                change_impact=FieldImpact.metadata_only,
                expected_active_plan_id=stale_active_id,
                check_active_plan=True,
            )
        unchanged = store.get_scenario("main")
        assert unchanged is not None
        assert unchanged.revision == 0
        assert unchanged.work_orders[0].note != "must not commit"


def test_orphan_execution_status_is_reported_by_integrity_scan(tmp_path):
    database = tmp_path / "orphan-execution-status.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    scenario.work_orders[0].status = WorkOrderStatus.started
    scenario.revision += 1
    store.save_scenario(scenario, "inject legacy orphan", expected_revision=0)
    issues = Store(database).list_integrity_issues()
    assert any(
        item["source_table"] == "scenario_work_order_status"
        and item["source_id"] == f"main:{scenario.work_orders[0].id}"
        and "missing events: start" in item["reason"]
        for item in issues
    )


def test_execution_event_relational_or_content_tampering_is_rejected(monkeypatch, tmp_path):
    database = tmp_path / "execution-event-integrity.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = min(
            (item for item in schedule["assignments"] if item["sequence"] == 1),
            key=lambda item: item["start_time"],
        )
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "execution-integrity-start-001",
            },
        )
        assert started.status_code == 200
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE work_order_execution_events SET booking_id='BOOK-TAMPERED' WHERE id=?",
                (started.json()["event"]["id"],),
            )
        completed = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["finish_time"],
                "expected_revision": 1,
                "idempotency_key": "execution-integrity-complete-001",
            },
        )
        assert completed.status_code == 409
        assert completed.json()["detail"]["code"] == "EXECUTION_EVENT_INTEGRITY_FAILED"
        listed = client.get("/api/scenarios/main/execution-events")
        assert listed.status_code == 409
        assert listed.json()["detail"]["code"] == "RECORD_INTEGRITY_FAILED"
        assert listed.json()["detail"]["record_type"] == "WORK_ORDER_EXECUTION_EVENT"


def test_risk_comparison_preflights_scope_before_creating_child_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "risk-preflight.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        first = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": 600, "time_limit_seconds": 1},
        )
        second = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": 650, "time_limit_seconds": 1},
        )
        assert first.status_code == second.status_code == 200
        plans = client.get("/api/scenarios/main/plan-versions").json()
        before, after = plans[-2:]
        compared = client.post(
            f"/api/scenarios/main/risk-comparison?before={before['id']}&after={after['id']}",
            headers={"Idempotency-Key": "risk-preflight-context-001"},
            json={"seed": 17, "trials": 50},
        )
        assert compared.status_code == 409
        assert compared.json()["detail"]["code"] == "PAIRED_ANALYSIS_CONTEXT_MISMATCH"
        assert client.get(f"/api/scenarios/main/plan-versions/{before['id']}/analysis-runs").json() == []
        assert client.get(f"/api/scenarios/main/plan-versions/{after['id']}/analysis-runs").json() == []
