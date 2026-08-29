from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.hashing import content_hash
from backend.models import FieldImpact, WorkOrder, WorkOrderStatus
from backend.storage import ActivePlanConflict, PublicationConflict, Store


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


def test_new_external_codes_are_url_safe_while_legacy_domain_ids_remain_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "url-identifiers.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        order = _create_payload(client, order_id="WO/BAD")
        assert (
            client.post(
                "/api/v2/scenarios/main/work-orders",
                headers={"If-Match": "D0"},
                json=order,
            ).status_code
            == 422
        )
        technician = client.get("/api/scenarios/main").json()["technicians"][0]
        assert (
            client.post(
                "/api/v2/scenarios/main/technicians",
                headers={"If-Match": "D0"},
                json={**technician, "id": "TECH/BAD"},
            ).status_code
            == 422
        )
        legacy = main_module.WorkOrder.model_validate({**order, "status": "pending"})
        assert legacy.id == "WO/BAD"


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
        applicability = active["applicability"]
        assert applicability["route_executable"] is True
        assert applicability["coverage_complete"] is False
        assert applicability["planning_current"] is False
        assert applicability["metrics_current"] is False
        assert applicability["commercial_current"] is True
        assert applicability["reoptimization_opportunity"] is False
        assert applicability["invalid_assignment_ids"] == []
        assert applicability["evaluated_scenario_revision"] == 1
        assert applicability["evaluated_scenario_snapshot_hash"]
        assert applicability["projection_hash"]


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


def test_v1_cas_write_declares_sunset_and_v2_successor(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "v1-sunset.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        response = client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "legacy client"})
        assert response.status_code == 200
        assert response.headers["Deprecation"] == "true"
        assert response.headers["Sunset"] == "Wed, 31 Mar 2027 00:00:00 GMT"
        assert response.headers["Link"] == '</api/v2/scenarios/main/work-orders/WO-1021>; rel="successor-version"'


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


def test_replan_and_publication_context_reject_tampered_execution_payload(monkeypatch, tmp_path):
    database = tmp_path / "execution-context-tamper.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "trusted-context-start-001",
                "estimated_remaining_minutes": 30,
            },
        )
        assert started.status_code == 200
        with closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute(
                "SELECT id, payload FROM work_order_execution_events WHERE id=?",
                (started.json()["event"]["id"],),
            ).fetchone()
            payload = json.loads(row[1])
            payload["estimated_remaining_minutes"] = 1
            connection.execute(
                "UPDATE work_order_execution_events SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), row[0]),
            )
        rejected = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": assignment["start_time"], "time_limit_seconds": 1},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "RECORD_INTEGRITY_FAILED"
        assert rejected.json()["detail"]["record_type"] == "WORK_ORDER_EXECUTION_EVENT"
        assert client.get("/api/scenarios/main/plan-versions").json()[-1]["number"] == 1


def test_execution_command_replay_reloads_event_and_rejects_missing_resource(monkeypatch, tmp_path):
    database = tmp_path / "execution-replay.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    request = None
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        request = {
            "technician_id": assignment["technician_id"],
            "occurred_at": assignment["start_time"],
            "expected_revision": 0,
            "idempotency_key": "trusted-replay-start-001",
        }
        endpoint = f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start"
        first = client.post(endpoint, json=request)
        assert first.status_code == 200
        event_id = first.json()["event"]["id"]
        assert first.json()["event"]["self_integrity"] == "VERIFIED"
        assert first.json()["event"]["effective_integrity"] == "VERIFIED"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE command_keys SET payload=? WHERE resource_id=?",
                ('{"result":{"event":{"occurred_at":0,"technician_id":"TAMPERED"}}}', event_id),
            )
        replay = client.post(endpoint, json=request)
        assert replay.status_code == 200
        assert replay.json()["event"] == first.json()["event"]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DELETE FROM work_order_execution_events WHERE id=?", (event_id,))
        missing = client.post(endpoint, json=request)
        assert missing.status_code == 409
        assert missing.json()["detail"]["code"] == "EXECUTION_REPLAY_EVENT_MISSING"


def test_execution_event_trust_labels_are_recomputed_not_read_from_payload(monkeypatch, tmp_path):
    database = tmp_path / "execution-trust-projection.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        request = {
            "technician_id": assignment["technician_id"],
            "occurred_at": assignment["start_time"],
            "expected_revision": 0,
            "idempotency_key": "execution-trust-label-001",
        }
        endpoint = f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start"
        started = client.post(endpoint, json=request)
        assert started.status_code == 200, started.text
        event_id = started.json()["event"]["id"]
        with closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute(
                "SELECT payload FROM work_order_execution_events WHERE id=?",
                (event_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["self_integrity"] = "FAILED"
            payload["source_plan_integrity"] = "FAILED"
            payload["effective_integrity"] = "FAILED"
            connection.execute(
                "UPDATE work_order_execution_events SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), event_id),
            )
        listed = client.get("/api/scenarios/main/execution-events")
        assert listed.status_code == 200, listed.text
        event = listed.json()[0]
        assert event["self_integrity"] == "VERIFIED"
        assert event["source_plan_integrity"] == "VERIFIED"
        assert event["effective_integrity"] == "VERIFIED"
        replay = client.post(endpoint, json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()["event"] == event


def test_execution_command_replay_survives_application_restart(monkeypatch, tmp_path):
    database = tmp_path / "execution-replay-restart.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        request = {
            "technician_id": assignment["technician_id"],
            "occurred_at": assignment["start_time"],
            "expected_revision": 0,
            "idempotency_key": "restart-replay-start-001",
        }
        endpoint = f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start"
        first = client.post(endpoint, json=request)
        assert first.status_code == 200
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as restarted:
        replay = restarted.post(endpoint, json=request)
        assert replay.status_code == 200
        assert replay.json()["event"]["id"] == first.json()["event"]["id"]


@pytest.mark.parametrize(
    ("field", "value_factory"),
    [
        ("shift_end", lambda technician, _order: technician["shift_end"] + 30),
        (
            "skills",
            lambda _technician, order: [
                next(skill for skill in ("electrical", "hvac", "network") if skill not in order["required_skills"])
            ],
        ),
        (
            "start_location",
            lambda technician, _order: {
                "x": technician["start_location"]["x"] + 1,
                "y": technician["start_location"]["y"],
            },
        ),
    ],
    ids=["shift-change", "skill-change", "start-location-change"],
)
def test_started_work_order_completes_after_technician_planning_change(monkeypatch, tmp_path, field, value_factory):
    database = tmp_path / f"complete-after-{field}.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        scenario = client.get("/api/scenarios/main").json()
        technician = next(item for item in scenario["technicians"] if item["id"] == assignment["technician_id"])
        order = next(item for item in scenario["work_orders"] if item["id"] == assignment["work_order_id"])
        started = client.post(
            f"/api/scenarios/main/work-orders/{order['id']}/start",
            json={
                "technician_id": technician["id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": scenario["revision"],
                "idempotency_key": f"start-before-{field}-001",
            },
        )
        assert started.status_code == 200, started.text
        edited = client.put(
            f"/api/v2/scenarios/main/technicians/{technician['id']}",
            headers={"If-Match": f"D{started.json()['scenario']['revision']}"},
            json={field: value_factory(technician, order)},
        )
        assert edited.status_code == 200, edited.text
        active = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        assert order["id"] not in active["applicability"]["invalid_assignment_ids"]

        completed = client.post(
            f"/api/scenarios/main/work-orders/{order['id']}/complete",
            json={
                "technician_id": technician["id"],
                "occurred_at": assignment["finish_time"] + 1,
                "expected_revision": edited.json()["revision"],
                "idempotency_key": f"complete-after-{field}-001",
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["event"]["booking_id"] == started.json()["event"]["booking_id"]
        assert completed.json()["event"]["plan_version_id"] == started.json()["event"]["plan_version_id"]
        assert completed.json()["event"]["source_assignment_hash"] == started.json()["event"]["source_assignment_hash"]


def test_complete_uses_verified_start_when_original_plan_is_no_longer_active(monkeypatch, tmp_path):
    database = tmp_path / "complete-without-active-plan.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "start-before-plan-removal-001",
            },
        )
        assert started.status_code == 200, started.text
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE scenarios SET active_plan_version_id=NULL WHERE id='main'")
            connection.execute("UPDATE plan_applicability SET active=0 WHERE scenario_id='main'")

        completed = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["finish_time"] + 1,
                "expected_revision": 1,
                "idempotency_key": "complete-after-plan-removal-001",
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["event"]["plan_version_id"] == started.json()["event"]["plan_version_id"]


def test_operational_view_exposes_independent_start_and_complete_authority(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "operational-transition-authority.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        before = client.get("/api/scenarios/main/operational-view").json()
        pending = next(item for item in before["work_orders"] if item["work_order_id"] == assignment["work_order_id"])
        assert pending["start_allowed"] is True
        assert pending["complete_allowed"] is False
        assert pending["complete_blocking_reason_code"] == "WORK_ORDER_NOT_STARTED"

        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "operational-authority-start-001",
            },
        )
        assert started.status_code == 200, started.text
        after = client.get("/api/scenarios/main/operational-view").json()
        active = next(item for item in after["work_orders"] if item["work_order_id"] == assignment["work_order_id"])
        assert active["start_allowed"] is False
        assert active["start_blocking_reason_code"] == "WORK_ORDER_ALREADY_STARTED"
        assert active["complete_allowed"] is True
        assert active["complete_blocking_reason_code"] is None


def test_dispatch_snapshot_binds_one_scenario_plan_and_execution_context(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "dispatch-snapshot.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        first = client.get("/api/scenarios/main/dispatch-snapshot")
        assert first.status_code == 200, first.text
        first_snapshot = first.json()
        assert first.headers["etag"] == f'"{first_snapshot["snapshot_token"]}"'
        assert (
            first_snapshot["scenario_head_snapshot_hash"]
            == first_snapshot["operational_view"]["scenario_snapshot_hash"]
        )
        assert first_snapshot["active_plan"]["id"] == first_snapshot["operational_view"]["active_plan_version_id"]
        assert first_snapshot["execution_watermark"] == first_snapshot["operational_view"]["execution_watermark"]
        assert first_snapshot["execution_context_hash"] == first_snapshot["operational_view"]["execution_context_hash"]

        optimized = client.post(
            "/api/scenarios/main/optimize",
            json={
                "time_limit_seconds": 1,
                "expected_active_plan_version_id": first_snapshot["active_plan"]["id"],
            },
        )
        assert optimized.status_code == 200, optimized.text
        second_snapshot = client.get("/api/scenarios/main/dispatch-snapshot").json()
        assert second_snapshot["scenario"]["revision"] == first_snapshot["scenario"]["revision"]
        assert second_snapshot["active_plan"]["id"] != first_snapshot["active_plan"]["id"]
        assert second_snapshot["snapshot_token"] != first_snapshot["snapshot_token"]


def test_dispatch_snapshot_validates_execution_event_chain(monkeypatch, tmp_path):
    database = tmp_path / "dispatch-snapshot-event-integrity.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in schedule["assignments"] if item["sequence"] == 1)
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "dispatch-integrity-start-001",
            },
        )
        assert started.status_code == 200, started.text
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE work_order_execution_events SET booking_id='BOOK-TAMPERED' WHERE id=?",
                (started.json()["event"]["id"],),
            )
        rejected = client.get("/api/scenarios/main/dispatch-snapshot")
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["record_type"] == "WORK_ORDER_EXECUTION_EVENT"


def _intake_emergency(store: Store, order: WorkOrder, key: str = "receipt-intake-001"):
    fingerprint = content_hash({"scenario_id": "main", "work_order": order})
    return store.intake_emergency_work_order(
        "main",
        order,
        namespace="main:emergency-intake",
        idempotency_key=key,
        request_fingerprint=fingerprint,
    )


def test_replayed_emergency_intake_requires_the_same_work_order_resource(tmp_path):
    store = Store(tmp_path / "emergency-receipt-resource.db")
    scenario = store.get_scenario("main")
    assert scenario is not None
    order = scenario.work_orders[0].model_copy(
        update={
            "id": "WO-EMG-RECEIPT",
            "is_emergency": True,
            "reported_at": scenario.work_orders[0].window_start,
            "status": WorkOrderStatus.pending,
        }
    )
    committed, created = _intake_emergency(store, order)
    assert created is True
    receipt = store.active_emergency_intake_receipt("main", order.id)
    assert receipt is not None
    assert receipt.work_order_hash == content_hash(order)

    committed.work_orders = [item for item in committed.work_orders if item.id != order.id]
    committed.revision += 1
    store.save_scenario(committed, "simulate legacy deletion", expected_revision=1)
    with pytest.raises(PublicationConflict) as caught:
        _intake_emergency(store, order)
    assert caught.value.code == "EMERGENCY_INTAKE_RESOURCE_MISSING"


def test_changed_emergency_payload_rejects_intake_replay(tmp_path):
    store = Store(tmp_path / "emergency-receipt-change.db")
    scenario = store.get_scenario("main")
    assert scenario is not None
    order = scenario.work_orders[0].model_copy(
        update={
            "id": "WO-EMG-CHANGED",
            "is_emergency": True,
            "reported_at": scenario.work_orders[0].window_start,
            "status": WorkOrderStatus.pending,
        }
    )
    committed, _ = _intake_emergency(store, order, "receipt-change-001")
    stored_order = next(item for item in committed.work_orders if item.id == order.id)
    stored_order.note = "changed after intake"
    committed.revision += 1
    store.save_scenario(committed, "change emergency", expected_revision=1)
    with pytest.raises(PublicationConflict) as caught:
        _intake_emergency(store, order, "receipt-change-001")
    assert caught.value.code == "EMERGENCY_INTAKE_RESOURCE_CHANGED"


def test_emergency_intake_cancel_is_explicit_atomic_and_restart_safe(monkeypatch, tmp_path):
    database = tmp_path / "emergency-receipt-cancel.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        store = main_module.require_store()
        scenario = store.get_scenario("main")
        assert scenario is not None
        order = scenario.work_orders[0].model_copy(
            update={
                "id": "WO-EMG-CANCEL",
                "is_emergency": True,
                "reported_at": scenario.work_orders[0].window_start,
                "status": WorkOrderStatus.pending,
            }
        )
        committed, _ = _intake_emergency(store, order, "receipt-cancel-intake-001")
        blocked = client.delete(
            f"/api/v2/scenarios/main/work-orders/{order.id}",
            headers={"If-Match": f"D{committed.revision}"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "EMERGENCY_INTAKE_CANCEL_REQUIRED"
        cancelled = client.post(
            f"/api/v2/scenarios/main/emergency-intakes/{order.id}/cancel",
            json={
                "expected_revision": committed.revision,
                "idempotency_key": "receipt-cancel-command-001",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert order.id not in {item["id"] for item in cancelled.json()["work_orders"]}
        replay = client.post(
            f"/api/v2/scenarios/main/emergency-intakes/{order.id}/cancel",
            json={
                "expected_revision": committed.revision,
                "idempotency_key": "receipt-cancel-command-001",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["revision"] == cancelled.json()["revision"]

    restarted = Store(database)
    with pytest.raises(PublicationConflict) as caught:
        _intake_emergency(restarted, order, "receipt-cancel-intake-001")
    assert caught.value.code == "EMERGENCY_INTAKE_CANCELLED"


def test_concurrent_same_emergency_intake_creates_one_receipt(tmp_path):
    database = tmp_path / "emergency-receipt-concurrent.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    order = scenario.work_orders[0].model_copy(
        update={
            "id": "WO-EMG-CONCURRENT",
            "is_emergency": True,
            "reported_at": scenario.work_orders[0].window_start,
            "status": WorkOrderStatus.pending,
        }
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _intake_emergency(store, order, "receipt-concurrent-001"), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM emergency_intake_receipts WHERE work_order_id=?",
                (order.id,),
            ).fetchone()[0]
            == 1
        )
        payload = json.loads(connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0])
        assert sum(item["id"] == order.id for item in payload["work_orders"]) == 1


def test_candidate_publication_cas_rejects_newer_active_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "publication-active-cas.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        first_plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        assert client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1}).status_code == 200
        store = main_module.require_store()
        stale_candidate = store.get_schedule_candidate(first_plan["candidate_id"])
        scenario = store.get_scenario("main")
        assert stale_candidate is not None and scenario is not None
        with pytest.raises(PublicationConflict) as caught:
            store.publish_plan(
                scenario,
                stale_candidate.schedule,
                "baseline",
                candidate_id=stale_candidate.id,
                expected_revision=scenario.revision,
            )
        assert caught.value.code == "ACTIVE_PLAN_CHANGED_DURING_COMMAND"
        plans = store.list_plan_versions("main")
        assert [item.number for item in plans] == [1, 2]
        assert plans[-1].active


def test_reattest_uses_request_active_plan_precondition(monkeypatch, tmp_path):
    database = tmp_path / "reattest-active-precondition.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        legacy = client.get("/api/scenarios/main/plan-versions").json()[0]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER prevent_plan_attestation_change")
            connection.execute(
                "UPDATE plan_versions SET attestation_requirement='LEGACY_MIGRATED' WHERE id=?",
                (legacy["id"],),
            )

        conflict = client.post(
            f"/api/scenarios/main/plan-versions/{legacy['id']}/reattest",
            json={
                "expected_revision": 0,
                "expected_active_plan_version_id": "PV-stale-client-view",
                "idempotency_key": "reattest-active-precondition-001",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == {
            "code": "ACTIVE_PLAN_CHANGED_DURING_COMMAND",
            "message": "命令开始前活动方案已变化，请刷新后重试",
            "retryable": True,
            "refresh_required": True,
            "expected_active_plan_id": "PV-stale-client-view",
            "current_active_plan_id": legacy["id"],
        }
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]


def test_revision_proof_tampering_blocks_reset_and_is_found_at_startup(monkeypatch, tmp_path):
    database = tmp_path / "revision-proof.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        with closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute(
                "SELECT id, payload FROM scenario_revisions WHERE scenario_id='main' ORDER BY number LIMIT 1"
            ).fetchone()
            payload = json.loads(row[1])
            payload["scenario"]["name"] = "tampered genesis"
            connection.execute(
                "UPDATE scenario_revisions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), row[0]),
            )
        reset = client.post("/api/scenarios/main/reset")
        assert reset.status_code == 409
        assert reset.json()["detail"]["record_type"] == "SCENARIO_REVISION"
    restarted = Store(database)
    assert any(
        item["source_table"] == "scenario_revisions" and item["source_id"] == row[0]
        for item in restarted.list_integrity_issues()
    )


def test_current_scenario_payload_tampering_blocks_reads_solving_and_laundering(monkeypatch, tmp_path):
    database = tmp_path / "scenario-head-tamper.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        with closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()
            payload = json.loads(row[0])
            payload["work_orders"][0]["service_duration"] += 17
            connection.execute(
                "UPDATE scenarios SET payload=? WHERE id='main'",
                (json.dumps(payload, ensure_ascii=False),),
            )
        for method, path, body in (
            ("get", "/api/scenarios/main", None),
            ("post", "/api/scenarios/main/baseline", None),
            ("put", "/api/scenarios/main/work-orders/WO-1021", {"note": "不能洗入历史"}),
        ):
            response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "SCENARIO_HEAD_INTEGRITY_FAILED"
        with closing(sqlite3.connect(database)) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM scenario_revisions WHERE scenario_id='main'").fetchone()[0]
                == 1
            )


def test_invalid_revision_descendant_never_becomes_verified(tmp_path):
    database = tmp_path / "revision-continuity.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    for index in range(2):
        scenario.description = f"revision {index + 1}"
        scenario.revision += 1
        store.save_scenario(scenario, f"revision {index + 1}", expected_revision=index)
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            "SELECT id, payload FROM scenario_revisions WHERE scenario_id='main' AND number=1"
        ).fetchone()
        payload = json.loads(row[1])
        payload["scenario"]["description"] = "tampered ancestor"
        connection.execute(
            "UPDATE scenario_revisions SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )
    Store(database)
    with closing(sqlite3.connect(database)) as connection:
        statuses = connection.execute(
            "SELECT number, chain_status FROM scenario_revisions WHERE scenario_id='main' ORDER BY number"
        ).fetchall()
    assert statuses == [(0, "VERIFIED"), (1, "ANCESTOR_INVALID"), (2, "ANCESTOR_INVALID")]


def test_revision_numbers_must_start_at_d0_and_remain_contiguous(tmp_path):
    database = tmp_path / "revision-gap.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    scenario.description = "D001"
    scenario.revision = 1
    store.save_scenario(scenario, "create D001", expected_revision=0)
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            "SELECT id, payload FROM scenario_revisions WHERE scenario_id='main' AND number=1"
        ).fetchone()
        payload = json.loads(row[1])
        payload["number"] = 2
        payload["scenario"]["revision"] = 2
        connection.execute(
            "UPDATE scenario_revisions SET number=2, payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )
    Store(database)
    with closing(sqlite3.connect(database)) as connection:
        statuses = connection.execute(
            "SELECT number, chain_status FROM scenario_revisions WHERE scenario_id='main' ORDER BY number"
        ).fetchall()
    assert statuses == [(0, "VERIFIED"), (2, "GAP_DETECTED")]

    root_database = tmp_path / "revision-root.db"
    Store(root_database)
    with closing(sqlite3.connect(root_database)) as connection, connection:
        row = connection.execute(
            "SELECT id, payload FROM scenario_revisions WHERE scenario_id='main' AND number=0"
        ).fetchone()
        payload = json.loads(row[1])
        payload["number"] = 5
        payload["scenario"]["revision"] = 5
        connection.execute(
            "UPDATE scenario_revisions SET number=5, payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )
    Store(root_database)
    with closing(sqlite3.connect(root_database)) as connection:
        assert connection.execute(
            "SELECT number, chain_status FROM scenario_revisions WHERE scenario_id='main'"
        ).fetchone() == (5, "ROOT_INVALID")


def test_schedule_comparison_includes_route_timing_lock_and_source_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "comparison-detail.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    scenario = main_module.get_fixture("main")
    before = main_module.baseline_schedule(scenario, 0)
    after = before.model_copy(deep=True)
    changed = after.assignments[0]
    changed.arrival_time += 3
    changed.start_time += 3
    changed.finish_time += 8
    changed.travel_minutes += 3
    changed.locked = not changed.locked
    changed.source_sequence = (before.assignments[0].source_sequence or changed.sequence) + 100
    changed.source_assignment_hash = "changed-source-identity"
    rows = main_module.schedule_change_rows(before, after)
    row = next(item for item in rows if item["work_order_id"] == changed.work_order_id)
    assert set(str(row["changed_fields"]).split(",")) >= {
        "arrival",
        "start",
        "finish",
        "travel",
        "locked",
        "source_sequence",
        "source_assignment_hash",
    }

    before.strategy = "custom"
    after.strategy = "custom"
    assert before.solver_policy is not None and after.solver_policy is not None
    after.solver_policy.fingerprint = "different-custom-policy"
    comparison = main_module.build_comparison(scenario.id, before, after, scenario, scenario)
    assert comparison.delta["objective"] is None
    assert comparison.raw_objective_comparable is False
    assert comparison.before_schedule_id == before.id
    assert comparison.after_schedule_id == after.id


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
