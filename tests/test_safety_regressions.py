import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.fixtures import get_fixture
from backend.models import SolverStatus, UnassignedReason, UnassignedWorkOrder
from backend.normalization import normalize_schedule
from backend.scheduler import baseline_schedule, solver_status_from_routing
from backend.storage import Store
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


def test_normalizer_overwrites_and_verifier_rejects_forged_assignment_facts():
    scenario = get_fixture("main")
    original = normalize_schedule(scenario, baseline_schedule(scenario, 0))
    forged = original.model_copy(deep=True)
    forged.assignments[0].changed = True
    forged.assignments[0].locked = True
    forged.assignments[0].travel_minutes += 17
    forged.assignments[0].explanation.append("伪造解释")
    forged.assignments[0].evidence["forged"] = True

    report = verify_schedule(scenario, forged)
    assert {
        "CHANGED_FLAG_MISMATCH",
        "LOCKED_FLAG_MISMATCH",
        "TRAVEL_TIME_MISMATCH",
        "EXPLANATION_MISMATCH",
        "EVIDENCE_MISMATCH",
    }.issubset(_codes(report))

    normalized = normalize_schedule(scenario, forged)
    assert normalized.assignments[0].changed is False
    assert normalized.assignments[0].locked is False
    assert "伪造解释" not in normalized.assignments[0].explanation
    assert "forged" not in normalized.assignments[0].evidence
    assert verify_schedule(scenario, normalized).publishable


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
        assert response.status_code == 422, response.text
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


def test_same_travel_provider_is_used_by_scheduler_normalizer_and_verifier():
    scenario = get_fixture("main")
    scenario.technicians = [scenario.technicians[0]]
    scenario.work_orders = [scenario.work_orders[0]]
    depot = scenario.technicians[0].start_location
    job = scenario.work_orders[0].location
    provider = MatrixTravelTimeProvider(
        matrix={
            ("DEPOT", "DEPOT"): 0,
            ("DEPOT", "JOB"): 41,
            ("JOB", "DEPOT"): 23,
            ("JOB", "JOB"): 0,
        },
        point_ids={(depot.x, depot.y): "DEPOT", (job.x, job.y): "JOB"},
        version="TEST_ASYMMETRIC_MATRIX",
    )
    result = normalize_schedule(
        scenario,
        baseline_schedule(scenario, 0, provider=provider),
        provider=provider,
    )
    assert result.assignments[0].travel_minutes == 41
    assert result.travel_model_version == "TEST_ASYMMETRIC_MATRIX"
    assert result.kpis.total_travel_minutes == 64
    assert verify_schedule(scenario, result, provider=provider).publishable
    assert "TRAVEL_MODEL_MISMATCH" in _codes(verify_schedule(scenario, result))


def test_app_and_publication_use_the_store_travel_provider(tmp_path):
    import backend.main as main_module

    scenario = get_fixture("main")
    scenario.id = "matrix-route"
    scenario.name = "矩阵行程测试"
    scenario.technicians = [scenario.technicians[0]]
    scenario.work_orders = [scenario.work_orders[0]]
    scenario.locked_assignments = []
    depot = scenario.technicians[0].start_location
    job = scenario.work_orders[0].location
    provider = MatrixTravelTimeProvider(
        matrix={
            ("DEPOT", "DEPOT"): 0,
            ("DEPOT", "JOB"): 41,
            ("JOB", "DEPOT"): 23,
            ("JOB", "JOB"): 0,
        },
        point_ids={(depot.x, depot.y): "DEPOT", (job.x, job.y): "JOB"},
        version="TEST_APP_MATRIX",
    )
    store = Store(tmp_path / "provider-app.db", travel_provider=provider)
    store.save_scenario(scenario, "创建矩阵测试场景")
    with TestClient(main_module.create_app(store_override=store)) as client:
        response = client.post("/api/scenarios/matrix-route/baseline")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["travel_model_version"] == "TEST_APP_MATRIX"
        assert result["assignments"][0]["travel_minutes"] == 41
        assert result["kpis"]["total_travel_minutes"] == 64
        plan = client.get("/api/scenarios/matrix-route/plan-versions").json()[0]
        assert plan["candidate_id"]


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


def test_blank_names_labels_and_invalid_colors_are_rejected_as_validation_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "strings.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan_id = client.get("/api/scenarios/main/plan-versions").json()[0]["id"]
        assert client.patch(
            f"/api/scenarios/main/plan-versions/{plan_id}",
            json={"label": "   "},
        ).status_code == 422
        assert client.post(
            "/api/strategy-profiles",
            json={"name": "   ", "description": "test", "time_limit_seconds": 1},
        ).status_code == 422
        technician = client.get("/api/scenarios/main").json()["technicians"][0]
        technician["id"] = "TECH-BAD-COLOR"
        technician["color"] = "red"
        assert client.post("/api/scenarios/main/technicians", json=technician).status_code == 422


def test_patch_contract_distinguishes_omitted_null_and_clearable_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "patch-contract.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        original = client.get("/api/scenarios/main").json()
        assert client.put("/api/scenarios/main/work-orders/WO-1021", json={}).status_code == 422
        assert client.put(
            "/api/scenarios/main/work-orders/WO-1021", json={"title": None}
        ).status_code == 422
        assert client.put(
            "/api/scenarios/main/technicians/TECH-01", json={"name": None}
        ).status_code == 422
        invalid_shift = client.put(
            "/api/scenarios/main/technicians/TECH-01", json={"shift_end": 400}
        )
        assert invalid_shift.status_code == 422
        assert client.put(
            "/api/scenarios/main/technicians/TECH-01", json={"color": " #315c4b "}
        ).status_code == 200

        with_note = client.put(
            "/api/scenarios/main/work-orders/WO-1021", json={"note": "  上门前联系  "}
        )
        assert with_note.status_code == 200
        order = next(
            item for item in with_note.json()["work_orders"] if item["id"] == "WO-1021"
        )
        assert order["note"] == "上门前联系"
        cleared = client.put(
            "/api/scenarios/main/work-orders/WO-1021", json={"note": None}
        )
        assert cleared.status_code == 200
        order = next(
            item for item in cleared.json()["work_orders"] if item["id"] == "WO-1021"
        )
        assert order["note"] == ""
        assert cleared.json()["revision"] == original["revision"] + 3


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


def test_replan_persists_explicit_frozen_context_and_only_warns_on_planned_departure(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "planning-context.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        started = min(baseline["assignments"], key=lambda item: item["start_time"])
        assert client.put(
            f"/api/scenarios/main/work-orders/{started['work_order_id']}",
            json={"status": "started"},
        ).status_code == 200
        response = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": 700, "time_limit_seconds": 1},
        )
        assert response.status_code == 200
        runs = client.get("/api/scenarios/main/schedule-runs").json()
        run = runs[-1]
        context = run["planning_context"]
        assert run["planning_context_hash"]
        assert [item["work_order_id"] for item in context["frozen_assignments"]] == [started["work_order_id"]]
        assert context["frozen_assignments"][0]["reason"] == "STARTED"
        assert context["inferred_departure_warnings"]
        assert started["work_order_id"] not in context["inferred_departure_warnings"]
        candidate = client.get(
            f"/api/scenarios/main/schedule-candidates/{run['candidate_id']}"
        ).json()
        assert candidate["planning_context_hash"] == run["planning_context_hash"]


def test_failed_compound_emergency_replan_keeps_demand_and_last_plan(monkeypatch, tmp_path):
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
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["emergency_work_order_persisted"] is True
        assert detail["coverage_status"] == "PARTIAL_NEW_DEMAND"
        scenario = client.get("/api/scenarios/main").json()
        assert scenario["revision"] == 1
        assert any(item["id"] == emergency["id"] for item in scenario["work_orders"])
        plans = client.get("/api/scenarios/main/plan-versions").json()
        assert [(item["number"], item["active"]) for item in plans] == [(1, True)]
        assert plans[0]["coverage_status"] == "PARTIAL_NEW_DEMAND"
        assert client.get("/api/scenarios/main/schedules").json()[0]["id"] == original["id"]
        repeated = client.post(
            "/api/scenarios/main/replan",
            json={
                "planning_time": 600, "time_limit_seconds": 1,
                "emergency_order": emergency, "idempotency_key": "emergency-rollback-001",
            },
        )
        assert repeated.status_code == 422
        assert repeated.json()["detail"]["run_id"] == detail["run_id"]

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as restarted:
        scenario = restarted.get("/api/scenarios/main").json()
        assert any(item["id"] == emergency["id"] for item in scenario["work_orders"])
        plans = restarted.get("/api/scenarios/main/plan-versions").json()
        assert plans[0]["coverage_status"] == "PARTIAL_NEW_DEMAND"


def test_emergency_preparation_failure_is_persisted_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency-prepare.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    calls = 0

    def failed_initial_solver(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected preparation failure")

    monkeypatch.setattr(main_module, "optimized_schedule", failed_initial_solver)
    emergency = {
        "id": "WO-EMG-PREPARE", "customer_name": "应急客户", "title": "机房断电",
        "required_skills": ["electrical"], "location": {"x": 50, "y": 50},
        "service_duration": 30, "window_start": 600, "window_end": 750,
        "sla_deadline": 660, "priority": "urgent", "drop_penalty": 10000,
        "status": "pending", "vip": True, "is_emergency": True,
        "reported_at": 600, "note": "",
    }
    payload = {
        "planning_time": 600,
        "time_limit_seconds": 1,
        "emergency_order": emergency,
        "idempotency_key": "emergency-prepare-001",
    }
    with TestClient(main_module.app) as client:
        first = client.post("/api/scenarios/main/replan", json=payload)
        second = client.post("/api/scenarios/main/replan", json=payload)
        assert first.status_code == second.status_code == 500
        assert first.json() == second.json()
        assert first.json()["detail"]["emergency_work_order_persisted"] is True
        scenario = client.get("/api/scenarios/main").json()
        assert sum(item["id"] == emergency["id"] for item in scenario["work_orders"]) == 1
        assert scenario["revision"] == 1
        assert calls == 1
        assert client.get("/api/scenarios/main/plan-versions").json() == []


def test_application_lifespan_closes_experiment_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "lifespan.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    assert main_module.experiment_executor is None


def test_import_has_no_database_side_effect_and_app_factory_isolates_stores(tmp_path):
    import_only_database = tmp_path / "import-only.db"
    environment = os.environ.copy()
    environment["FIELDFLOW_DB"] = str(import_only_database)
    completed = subprocess.run(
        [sys.executable, "-c", "import backend.main"],
        cwd=tmp_path.parent,
        env={**environment, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not import_only_database.exists()

    import backend.main as main_module
    first_store = main_module.Store(tmp_path / "factory-one.db")
    second_store = main_module.Store(tmp_path / "factory-two.db")
    with TestClient(main_module.create_app(store_override=first_store)) as first:
        assert first.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "仅第一个 Store"}).status_code == 200
    with TestClient(main_module.create_app(store_override=second_store)) as second:
        order = next(item for item in second.get("/api/scenarios/main").json()["work_orders"] if item["id"] == "WO-1021")
        assert order["note"] != "仅第一个 Store"


def test_local_host_origin_and_report_filename_boundaries(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "local-boundary.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    assert main_module.safe_filename_component("main\r\nX-Evil: yes") == "main-X-Evil-yes"
    with TestClient(main_module.app) as client:
        assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400
        forbidden = client.post(
            "/api/scenarios/main/baseline",
            headers={"Origin": "https://attacker.example"},
        )
        assert forbidden.status_code == 403
        allowed = client.post(
            "/api/scenarios/main/baseline",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert allowed.status_code == 200
    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200
        assert main_module.experiment_executor is not None
    assert main_module.experiment_executor is None
