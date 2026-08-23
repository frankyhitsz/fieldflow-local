import importlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.fixtures import get_fixture
from backend.models import SolverStatus, UnassignedReason, UnassignedWorkOrder
from backend.scheduler import baseline_schedule, solver_status_from_routing
from backend.travel import EuclideanTravelTimeProvider, MatrixTravelTimeProvider
from backend.verification import verify_schedule


def _codes(report) -> set[str]:
    return {item.code for item in report.errors}


def test_verifier_rejects_missing_duplicate_overlap_and_forged_kpis():
    scenario = get_fixture("main")
    original = baseline_schedule(scenario, 0)

    missing = original.model_copy(deep=True)
    missing.assignments.pop()
    assert "MISSING_WORK_ORDER" in _codes(verify_schedule(scenario, missing))

    empty = original.model_copy(deep=True)
    empty.assignments = []
    empty.unassigned = []
    assert {"EMPTY_CANDIDATE", "MISSING_WORK_ORDER"}.issubset(_codes(verify_schedule(scenario, empty)))

    duplicate = original.model_copy(deep=True)
    duplicate.assignments.append(duplicate.assignments[0].model_copy(deep=True))
    assert "DUPLICATE_ASSIGNMENT" in _codes(verify_schedule(scenario, duplicate))

    overlap = original.model_copy(deep=True)
    overlap.unassigned.append(UnassignedWorkOrder(
        work_order_id=overlap.assignments[0].work_order_id,
        reason=UnassignedReason.dropped_by_objective,
        detail="test",
    ))
    assert "ASSIGNED_AND_UNASSIGNED" in _codes(verify_schedule(scenario, overlap))

    forged = original.model_copy(deep=True)
    forged.kpis.total_travel_minutes += 999
    assert "KPI_MISMATCH" in _codes(verify_schedule(scenario, forged))


def test_non_publishable_solver_result_keeps_current_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "failed-solver.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        published = client.post("/api/scenarios/main/baseline").json()

        def no_solution(scenario, version, *args, **kwargs):
            result = baseline_schedule(scenario, version, kwargs.get("strategy", "balanced"))
            result.kind = "optimized"
            result.solver_status = SolverStatus.time_limit_no_solution
            result.solution_found = False
            result.termination_reason = "ROUTING_FAIL_TIMEOUT"
            return result

        monkeypatch.setattr(main_module, "optimized_schedule", no_solution)
        response = client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["active_plan_version_id"] is not None
        assert any(item["code"] == "SOLVER_STATUS_NOT_PUBLISHABLE" for item in detail["errors"])
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]
        assert client.get("/api/scenarios/main/schedules").json()[0]["id"] == published["id"]
        run = client.get(f"/api/scenarios/main/schedule-runs/{detail['run_id']}").json()
        candidate = client.get(f"/api/scenarios/main/schedule-candidates/{detail['candidate_id']}").json()
        assert run["status"] == "TIME_LIMIT_NO_SOLUTION"
        assert candidate["publishable"] is False


def test_solver_limit_and_status_contract(monkeypatch, tmp_path):
    assert solver_status_from_routing(7, True) is SolverStatus.optimal
    assert solver_status_from_routing(2, True) is SolverStatus.time_limit_feasible
    assert solver_status_from_routing(4, False) is SolverStatus.time_limit_no_solution
    assert solver_status_from_routing(6, False) is SolverStatus.infeasible

    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "limits.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 0.05}).status_code == 422
        result = client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1}).json()
        assert result["requested_time_limit_ms"] == 1000
        assert result["effective_time_limit_ms"] == 1000
        assert result["solver_status_code"] in range(8)
        assert result["termination_reason"].startswith("ROUTING_")


def test_timeout_replan_does_not_drop_pending_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "timeout-replan.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        active = client.post("/api/scenarios/main/baseline").json()

        def timeout_result(scenario, version, previous, *args, **kwargs):
            result = previous.model_copy(deep=True)
            result.id = "CANDIDATE-TIMEOUT"
            result.kind = "replan"
            result.version = version
            result.assignments = []
            result.unassigned = []
            result.solver_status = SolverStatus.time_limit_no_solution
            result.solution_found = False
            result.termination_reason = "ROUTING_FAIL_TIMEOUT"
            return result

        monkeypatch.setattr(main_module, "replan_schedule", timeout_result)
        response = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": 600, "time_limit_seconds": 1},
        )
        assert response.status_code == 422
        plans = client.get("/api/scenarios/main/plan-versions").json()
        assert [item["number"] for item in plans] == [1]
        assert plans[0]["active"] is True
        assert client.get("/api/scenarios/main/schedules").json()[0]["id"] == active["id"]


def test_travel_provider_handles_same_location_and_matrix_validation():
    scenario = get_fixture("main")
    point = scenario.technicians[0].start_location
    assert EuclideanTravelTimeProvider().minutes(point, point) == 0

    other = scenario.work_orders[0].location
    provider = MatrixTravelTimeProvider(
        matrix={("DEPOT", "JOB"): 17, ("JOB", "DEPOT"): 23},
        point_ids={(point.x, point.y): "DEPOT", (other.x, other.y): "JOB"},
    )
    assert provider.minutes(point, other) == 17
    assert provider.minutes(other, point) == 23
    with pytest.raises(KeyError):
        provider.minutes(point, scenario.work_orders[1].location)


def test_scenario_aggregate_and_work_order_state_machine(monkeypatch, tmp_path):
    scenario = get_fixture("main")
    payload = scenario.model_dump()
    payload["work_orders"].append(payload["work_orders"][0])
    with pytest.raises(ValidationError, match="work order IDs must be unique"):
        scenario.model_validate(payload)

    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "state.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.put("/api/scenarios/main/work-orders/WO-1021", json={"status": "started"}).status_code == 200
        rollback = client.put("/api/scenarios/main/work-orders/WO-1021", json={"status": "pending"})
        assert rollback.status_code == 409
        immutable = client.put("/api/scenarios/main/work-orders/WO-1021", json={"title": "rewrite"})
        assert immutable.status_code == 409
        started_lock = client.post(
            "/api/scenarios/main/lock",
            json={"work_order_id": "WO-1021", "technician_id": "TECH-01", "locked": True},
        )
        assert started_lock.status_code == 409
        assert client.post(
            "/api/scenarios/main/lock",
            json={"work_order_id": "WO-1022", "technician_id": "TECH-01", "locked": True},
        ).status_code == 200
        completed = client.put("/api/scenarios/main/work-orders/WO-1022", json={"status": "completed"})
        assert completed.status_code == 200
        assert not any(item["work_order_id"] == "WO-1022" for item in completed.json()["locked_assignments"])


def test_compound_emergency_replan_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    emergency = {
        "id": "WO-EMG-IDEMP", "customer_name": "应急客户", "title": "机房断电",
        "required_skills": ["electrical"], "location": {"x": 50, "y": 50},
        "service_duration": 30, "window_start": 600, "window_end": 750,
        "sla_deadline": 660, "priority": "urgent", "drop_penalty": 10000,
        "status": "pending", "vip": True, "is_emergency": True,
        "reported_at": 600, "note": "",
    }
    request = {
        "emergency_order": emergency, "planning_time": 600, "current_time": 600,
        "time_limit_seconds": 1, "strategy": "stable", "idempotency_key": "emergency-idemp-001",
    }
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        first = client.post("/api/scenarios/main/replan", json=request)
        second = client.post("/api/scenarios/main/replan", json=request)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        scenario = client.get("/api/scenarios/main").json()
        assert sum(item["id"] == emergency["id"] for item in scenario["work_orders"]) == 1
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2]


def test_failed_compound_emergency_replan_rolls_back_data_and_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency-rollback.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    emergency = {
        "id": "WO-EMG-ROLLBACK", "customer_name": "应急客户", "title": "机房断电",
        "required_skills": ["electrical"], "location": {"x": 50, "y": 50},
        "service_duration": 30, "window_start": 600, "window_end": 750,
        "sla_deadline": 660, "priority": "urgent", "drop_penalty": 10000,
        "status": "pending", "vip": True, "is_emergency": True,
        "reported_at": 600, "note": "",
    }

    with TestClient(main_module.app) as client:
        original = client.post("/api/scenarios/main/baseline").json()

        def failed_replan(scenario, version, previous, *args, **kwargs):
            result = previous.model_copy(deep=True)
            result.id = "FAILED-EMERGENCY-CANDIDATE"
            result.kind = "replan"
            result.version = version
            result.scenario_id = scenario.id
            result.scenario_revision = scenario.revision
            result.assignments = []
            result.unassigned = []
            result.solver_status = SolverStatus.time_limit_no_solution
            result.solution_found = False
            result.termination_reason = "ROUTING_FAIL_TIMEOUT"
            return result

        monkeypatch.setattr(main_module, "replan_schedule", failed_replan)
        response = client.post(
            "/api/scenarios/main/replan",
            json={
                "planning_time": 600, "time_limit_seconds": 1,
                "emergency_order": emergency, "idempotency_key": "emergency-rollback-001",
            },
        )
        assert response.status_code == 422
        scenario = client.get("/api/scenarios/main").json()
        assert scenario["revision"] == 0
        assert not any(item["id"] == emergency["id"] for item in scenario["work_orders"])
        plans = client.get("/api/scenarios/main/plan-versions").json()
        assert [(item["number"], item["active"]) for item in plans] == [(1, True)]
        assert client.get("/api/scenarios/main/schedules").json()[0]["id"] == original["id"]


def test_application_lifespan_closes_experiment_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "lifespan.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    assert main_module.experiment_executor is None
    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200
        assert main_module.experiment_executor is not None
    assert main_module.experiment_executor is None
