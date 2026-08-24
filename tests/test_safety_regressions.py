import importlib
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.fixtures import get_fixture
from backend.models import (
    LockedAssignment,
    SolverStatus,
    StrategyExperiment,
    UnassignedReason,
    UnassignedWorkOrder,
    WorkOrderStatus,
)
from backend.normalization import normalize_schedule
from backend.scheduler import baseline_schedule, optimized_schedule, solver_status_from_routing
from backend.storage import Store
from backend.travel import EuclideanTravelTimeProvider, MatrixTravelTimeProvider
from backend.verification import verify_schedule


def _codes(report) -> set[str]:
    return {item.code for item in report.errors}


def _warning_codes(report) -> set[str]:
    return {item.code for item in report.warnings}


def test_legacy_semantic_upgrade_is_persisted_once_instead_of_mutating_reads(tmp_path):
    database = tmp_path / "semantic-upgrade.db"
    initial = Store(database)
    scenario = initial.get_scenario("main")
    assert scenario is not None
    scenario.solver_config.travel_weight = 1
    scenario.solver_config.sla_late_weight = 8
    scenario.solver_config.overtime_weight = 4
    scenario.solver_config.imbalance_weight = 2
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE scenarios SET payload=? WHERE id='main'",
            (scenario.model_dump_json(),),
        )
        connection.execute("PRAGMA user_version=7")

    migrated = Store(database)
    first = migrated.get_scenario("main")
    second = migrated.get_scenario("main")
    assert first is not None and second is not None
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.revision == scenario.revision + 1
    assert (
        first.solver_config.travel_weight,
        first.solver_config.sla_late_weight,
        first.solver_config.overtime_weight,
        first.solver_config.imbalance_weight,
    ) == (4, 12, 30, 1)
    with closing(sqlite3.connect(database)) as connection, connection:
        stored = connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
    assert stored == first.model_dump_json()
    assert migrated.list_revisions("main")[-1].reason == "v8 旧数据语义升级"


def test_concurrent_idempotent_optimize_runs_solver_only_once(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "concurrent-command.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    original = main_module.optimized_schedule
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_solver(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "optimized_schedule", slow_solver)
    request = main_module.OptimizeRequest(strategy="balanced", time_limit_seconds=1)
    idempotency_key = "concurrent-optimize-command"
    with TestClient(main_module.app), ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(main_module.run_optimize, "main", request, idempotency_key)
        assert entered.wait(timeout=5)
        with pytest.raises(main_module.HTTPException) as duplicate:
            main_module.run_optimize("main", request, idempotency_key)
        assert duplicate.value.status_code == 409
        assert duplicate.value.detail["code"] == "IDEMPOTENT_REQUEST_IN_PROGRESS"
        release.set()
        first = first_future.result(timeout=10)
        replay = main_module.run_optimize("main", request, idempotency_key)
        assert replay.id == first.id
    assert calls == 1


def test_replan_idempotency_key_cannot_replay_an_old_aggregate(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "replan-stale-replay.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    payload = {
        "planning_time": 600,
        "time_limit_seconds": 1,
        "idempotency_key": "replan-stale-aggregate-001",
    }
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        first = client.post("/api/scenarios/main/replan", json=payload)
        assert first.status_code == 200, first.text
        assert (
            client.put(
                "/api/scenarios/main/work-orders/WO-1021",
                json={"note": "幂等重放前的业务修订"},
            ).status_code
            == 200
        )
        stale_replay = client.post("/api/scenarios/main/replan", json=payload)
        assert stale_replay.status_code == 409
        assert "不同请求" in str(stale_replay.json()["detail"])


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
    overlap.unassigned.append(
        UnassignedWorkOrder(
            work_order_id=overlap.assignments[0].work_order_id,
            reason=UnassignedReason.dropped_by_objective,
            detail="test",
        )
    )
    assert "ASSIGNED_AND_UNASSIGNED" in _codes(verify_schedule(scenario, overlap))

    forged = original.model_copy(deep=True)
    forged.kpis.total_travel_minutes += 999
    assert "KPI_MISMATCH" in _codes(verify_schedule(scenario, forged))


def test_verifier_rejects_unassigned_locks_and_started_work_without_assignment():
    scenario = get_fixture("main")
    original = normalize_schedule(scenario, baseline_schedule(scenario, 0))
    assignment = original.assignments[0]
    scenario.locked_assignments = [
        LockedAssignment(
            work_order_id=assignment.work_order_id,
            technician_id=assignment.technician_id,
        )
    ]
    missing = original.model_copy(deep=True)
    missing.assignments = [item for item in missing.assignments if item.work_order_id != assignment.work_order_id]
    missing.unassigned.append(
        UnassignedWorkOrder(
            work_order_id=assignment.work_order_id,
            reason=UnassignedReason.locked_plan_conflict,
            detail="test",
        )
    )
    assert "LOCKED_WORK_ORDER_UNASSIGNED" in _codes(verify_schedule(scenario, missing))

    next(item for item in scenario.work_orders if item.id == assignment.work_order_id).status = WorkOrderStatus.started
    scenario.locked_assignments = []
    assert "STARTED_WORK_ORDER_UNASSIGNED" in _codes(verify_schedule(scenario, missing))


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
        "EVIDENCE_MISMATCH",
    }.issubset(_codes(report))
    assert "EXPLANATION_TEMPLATE_OUTDATED" in _warning_codes(report)

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
    assert solver_status_from_routing(2, True) is SolverStatus.feasible
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
        policy = result["solver_policy"]
        assert policy["profile_id"] == "balanced"
        assert policy["profile_snapshot"]["weights"]["unassigned_penalty_scale"] == 1
        assert policy["time_limit_ms"] == 1000
        assert policy["solution_limit"] == 120
        assert policy["first_solution_strategy"] == "PARALLEL_CHEAPEST_INSERTION"
        assert policy["local_search_metaheuristic"] == "GUIDED_LOCAL_SEARCH"
        assert policy["fingerprint"]
        run = client.get("/api/scenarios/main/schedule-runs").json()[-1]
        assert run["solver_policy_fingerprint"] == policy["fingerprint"]


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
    changed_provider = MatrixTravelTimeProvider(
        matrix={
            ("DEPOT", "DEPOT"): 0,
            ("DEPOT", "JOB"): 42,
            ("JOB", "DEPOT"): 23,
            ("JOB", "JOB"): 0,
        },
        point_ids={(depot.x, depot.y): "DEPOT", (job.x, job.y): "JOB"},
        version="TEST_ASYMMETRIC_MATRIX",
    )
    assert provider.fingerprint != changed_provider.fingerprint
    assert "TRAVEL_MODEL_FINGERPRINT_MISMATCH" in _codes(verify_schedule(scenario, result, provider=changed_provider))


def test_reported_at_is_a_hard_earliest_start_for_every_solver_path():
    scenario = get_fixture("main")
    original = scenario.work_orders[0]
    scenario.work_orders = [
        original.model_validate(
            {
                **original.model_dump(),
                "is_emergency": True,
                "reported_at": original.window_start + 45,
            }
        )
    ]
    baseline = baseline_schedule(scenario, 0)
    optimized = optimized_schedule(scenario, 0, baseline, time_limit_seconds=1)
    ready_at = scenario.work_orders[0].reported_at
    assert ready_at is not None
    assert baseline.assignments[0].start_time >= ready_at
    assert optimized.assignments[0].start_time >= ready_at

    forged = normalize_schedule(scenario, baseline)
    forged.assignments[0].start_time = ready_at - 1
    forged.assignments[0].finish_time = ready_at - 1 + scenario.work_orders[0].service_duration
    assert "BEFORE_DEMAND_REPORTED" in _codes(verify_schedule(scenario, forged))


def test_inserting_new_work_marks_the_following_committed_stop_changed():
    scenario = get_fixture("main")
    previous = normalize_schedule(scenario, baseline_schedule(scenario, 0))
    technician_id = next(
        technician.id
        for technician in scenario.technicians
        if sum(item.technician_id == technician.id for item in previous.assignments) >= 2
    )
    route = sorted(
        [item for item in previous.assignments if item.technician_id == technician_id],
        key=lambda item: item.sequence,
    )
    following = route[1]
    template = next(item for item in scenario.work_orders if item.id == following.work_order_id)
    inserted_order = template.model_copy(deep=True)
    inserted_order.id = "WO-INSERTED-CHANGE"
    scenario.work_orders.append(inserted_order)

    candidate = previous.model_copy(deep=True)
    candidate.kind = "replan"
    for assignment in candidate.assignments:
        if assignment.technician_id == technician_id and assignment.sequence >= following.sequence:
            assignment.sequence += 1
    inserted = following.model_copy(deep=True)
    inserted.work_order_id = inserted_order.id
    inserted.sequence = following.sequence
    candidate.assignments.append(inserted)

    normalized = normalize_schedule(scenario, candidate, previous)
    changed = next(item for item in normalized.assignments if item.work_order_id == following.work_order_id)
    assert changed.changed is True


def test_experiment_cancel_cannot_be_overwritten_by_a_stale_worker(tmp_path):
    store = Store(tmp_path / "cancel-race.db")
    experiment = StrategyExperiment(
        id="EXP-CANCEL-RACE",
        scenario_id="main",
        dataset="current",
        data_revision=0,
        status="RUNNING",
        progress=10,
        created_at="2026-08-24T00:00:00+00:00",
    )
    store.save_experiment(experiment)
    stale_worker = experiment.model_copy(deep=True)
    requested = store.request_experiment_cancel(experiment.id)
    assert requested is not None and requested.status == "CANCEL_REQUESTED"

    stale_worker.progress = 50
    store.save_experiment(stale_worker)
    assert store.get_experiment(experiment.id).status == "CANCEL_REQUESTED"

    stale_worker.status = "COMPLETED"
    stale_worker.progress = 100
    store.save_experiment(stale_worker)
    cancelled = store.get_experiment(experiment.id)
    assert cancelled is not None and cancelled.status == "CANCELLED"
    assert cancelled.cancel_requested_at == requested.cancel_requested_at
    stale_worker.status = "FAILED"
    store.save_experiment(stale_worker)
    assert store.get_experiment(experiment.id).status == "CANCELLED"


def test_strategy_experiment_rejects_started_work_and_preserves_active_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "started-experiment.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        revision = client.get("/api/scenarios/main").json()["revision"]
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": revision,
                "idempotency_key": "experiment-start-001",
            },
        )
        assert started.status_code == 200
        before = client.get("/api/scenarios/main/plan-versions").json()
        rejected = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"dataset": "current", "profile_ids": ["balanced"]},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "EXECUTION_CONTEXT_REQUIRED"
        after = client.get("/api/scenarios/main/plan-versions").json()
        assert [(item["id"], item["number"], item["active"]) for item in after] == [(before[0]["id"], 1, True)]
        assert client.get("/api/scenarios/main/execution-events").json()[0]["sequence"] == 1


def test_started_candidate_requires_authoritative_execution_context(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "execution-context.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        revision = client.get("/api/scenarios/main").json()["revision"]
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
                json={
                    "technician_id": assignment["technician_id"],
                    "occurred_at": assignment["start_time"],
                    "expected_revision": revision,
                    "idempotency_key": "context-source-001",
                },
            ).status_code
            == 200
        )
        store = main_module.require_store()
        scenario = store.get_scenario("main")
        source = store.active_plan_version("main")
        assert scenario is not None and source is not None
        execution = store.execution_source_context("main")
        planning = main_module.build_planning_context(
            scenario,
            source,
            assignment["start_time"],
            execution,
        )
        candidate = source.selected.model_copy(deep=True)
        candidate.kind = "replan"
        candidate = normalize_schedule(scenario, candidate, source.selected)

        valid = verify_schedule(
            scenario,
            candidate,
            source.selected,
            planning,
            store.travel_provider,
            execution,
        )
        assert valid.publishable

        without_context = verify_schedule(
            scenario,
            candidate,
            source.selected,
            None,
            store.travel_provider,
            execution,
        )
        assert "EXECUTION_CONTEXT_REQUIRED" in _codes(without_context)

        missing_frozen = planning.model_copy(deep=True)
        missing_frozen.frozen_assignments = []
        assert "FROZEN_CONTEXT_INCOMPLETE" in _codes(
            verify_schedule(scenario, candidate, source.selected, missing_frozen, store.travel_provider, execution)
        )

        changed_watermark = execution.model_copy(deep=True)
        changed_watermark.execution_event_sequence += 1
        assert "EXECUTION_WATERMARK_MISMATCH" in _codes(
            verify_schedule(scenario, candidate, source.selected, planning, store.travel_provider, changed_watermark)
        )

        missing_plan = execution.model_copy(deep=True)
        missing_plan.active_plan_version_id = None
        assert "ACTIVE_EXECUTION_PLAN_MISSING" in _codes(
            verify_schedule(scenario, candidate, source.selected, planning, store.travel_provider, missing_plan)
        )

        moved = candidate.model_copy(deep=True)
        started_assignment = next(
            item for item in moved.assignments if item.work_order_id == assignment["work_order_id"]
        )
        started_assignment.technician_id = next(
            item.id for item in scenario.technicians if item.id != assignment["technician_id"]
        )
        assert "STARTED_TECHNICIAN_CHANGED" in _codes(
            verify_schedule(scenario, moved, source.selected, planning, store.travel_provider, execution)
        )


def test_execution_order_and_late_start_control_following_capacity(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "execution-order.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        routes: dict[str, list[dict]] = {}
        for item in baseline["assignments"]:
            routes.setdefault(item["technician_id"], []).append(item)
        route = next(sorted(items, key=lambda item: item["sequence"]) for items in routes.values() if len(items) >= 2)
        first, second = route[:2]
        revision = client.get("/api/scenarios/main").json()["revision"]
        skipped = client.post(
            f"/api/scenarios/main/work-orders/{second['work_order_id']}/start",
            json={
                "technician_id": second["technician_id"],
                "occurred_at": second["start_time"],
                "expected_revision": revision,
                "idempotency_key": "skip-predecessor-001",
            },
        )
        assert skipped.status_code == 409
        assert "前序工单" in skipped.json()["detail"]["message"]

        actual_start = first["start_time"] + 45
        started = client.post(
            f"/api/scenarios/main/work-orders/{first['work_order_id']}/start",
            json={
                "technician_id": first["technician_id"],
                "occurred_at": actual_start,
                "expected_revision": revision,
                "idempotency_key": "late-start-001",
            },
        )
        assert started.status_code == 200
        double_start = client.post(
            f"/api/scenarios/main/work-orders/{second['work_order_id']}/start",
            json={
                "technician_id": second["technician_id"],
                "occurred_at": second["start_time"],
                "expected_revision": started.json()["scenario"]["revision"],
                "idempotency_key": "double-start-001",
            },
        )
        assert double_start.status_code == 409
        replanned = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": actual_start, "time_limit_seconds": 1},
        )
        assert replanned.status_code == 200, replanned.text
        context = client.get("/api/scenarios/main/schedule-runs").json()[-1]["planning_context"]
        source = context["execution_source_context"]["started_assignments"][0]
        assert source["actual_start_at"] == actual_start
        assert source["projected_available_at"] == actual_start + next(
            item["service_duration"]
            for item in client.get("/api/scenarios/main").json()["work_orders"]
            if item["id"] == first["work_order_id"]
        )
        following = sorted(
            [
                item
                for item in replanned.json()["assignments"]
                if item["technician_id"] == first["technician_id"] and item["sequence"] > first["sequence"]
            ],
            key=lambda item: item["sequence"],
        )
        if following:
            assert following[0]["arrival_time"] >= source["projected_available_at"]
        current = client.get("/api/scenarios/main").json()
        duration = next(
            item["service_duration"] for item in current["work_orders"] if item["id"] == first["work_order_id"]
        )
        actual_complete = actual_start + duration + 10
        completed = client.post(
            f"/api/scenarios/main/work-orders/{first['work_order_id']}/complete",
            json={
                "technician_id": first["technician_id"],
                "occurred_at": actual_complete,
                "expected_revision": current["revision"],
                "idempotency_key": "actual-complete-001",
            },
        )
        assert completed.status_code == 200
        after_complete = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": actual_complete, "time_limit_seconds": 1},
        )
        assert after_complete.status_code == 200, after_complete.text
        assert all(item["work_order_id"] != first["work_order_id"] for item in after_complete.json()["assignments"])
        completed_context = client.get("/api/scenarios/main/schedule-runs").json()[-1]["planning_context"]
        projection = next(
            item
            for item in completed_context["execution_source_context"]["technician_projections"]
            if item["technician_id"] == first["technician_id"]
        )
        assert projection["state"] == "completed"
        assert projection["available_at"] == actual_complete


def test_execution_history_blocks_reset_and_metadata_edit_keeps_completion_source(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "execution-reset.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        revision = client.get("/api/scenarios/main").json()["revision"]
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": revision,
                "idempotency_key": "metadata-start-001",
            },
        )
        assert started.status_code == 200
        replanned = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": assignment["start_time"], "time_limit_seconds": 1},
        )
        assert replanned.status_code == 200, replanned.text
        execution_plan = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        cloned = client.post(
            f"/api/scenarios/main/plan-versions/{execution_plan['id']}/clone-scenario",
            json={
                "name": "执行快照规划副本",
                "idempotency_key": "clone-execution-snapshot-001",
            },
        )
        assert cloned.status_code == 201, cloned.text
        cloned_order = next(item for item in cloned.json()["work_orders"] if item["id"] == assignment["work_order_id"])
        assert cloned_order["status"] == "pending"
        edited = client.put(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}",
            json={"note": "现场确认后继续"},
        )
        assert edited.status_code == 200
        assert any(item["active"] for item in client.get("/api/scenarios/main/plan-versions").json())
        completed = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["finish_time"] + 1,
                "expected_revision": edited.json()["revision"],
                "idempotency_key": "metadata-complete-001",
            },
        )
        assert completed.status_code == 200
        edited_after_completion = client.put(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}",
            json={"note": "完工记录已复核"},
        )
        assert edited_after_completion.status_code == 200
        assert any(item["active"] for item in client.get("/api/scenarios/main/plan-versions").json())
        reset = client.post("/api/scenarios/main/reset")
        assert reset.status_code == 409
        assert reset.json()["detail"]["code"] == "EXECUTION_HISTORY_PRESENT"


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
        baseline = client.post("/api/scenarios/main/baseline").json()
        assigned = baseline["assignments"][0]
        assert (
            client.put(
                f"/api/scenarios/main/work-orders/{assigned['work_order_id']}", json={"status": "started"}
            ).status_code
            == 409
        )
        locked = client.post(
            "/api/scenarios/main/lock",
            json={
                "work_order_id": assigned["work_order_id"],
                "technician_id": assigned["technician_id"],
                "locked": True,
            },
        )
        assert locked.status_code == 200
        baseline = client.post("/api/scenarios/main/baseline").json()
        assigned = next(item for item in baseline["assignments"] if item["work_order_id"] == assigned["work_order_id"])
        wrong_technician = next(
            item["id"]
            for item in client.get("/api/scenarios/main").json()["technicians"]
            if item["id"] != assigned["technician_id"]
        )
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/start",
                json={
                    "technician_id": wrong_technician,
                    "occurred_at": assigned["start_time"],
                    "expected_revision": locked.json()["revision"],
                    "idempotency_key": "state-wrong-tech-001",
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/complete",
                json={
                    "technician_id": assigned["technician_id"],
                    "occurred_at": assigned["finish_time"],
                    "expected_revision": locked.json()["revision"],
                    "idempotency_key": "state-early-complete-001",
                },
            ).status_code
            == 409
        )
        started = client.post(
            f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/start",
            json={
                "technician_id": assigned["technician_id"],
                "occurred_at": assigned["start_time"],
                "expected_revision": locked.json()["revision"],
                "idempotency_key": "state-start-001",
            },
        )
        assert started.status_code == 200
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/start",
                json={
                    "technician_id": assigned["technician_id"],
                    "occurred_at": assigned["start_time"],
                    "expected_revision": locked.json()["revision"],
                    "idempotency_key": "state-start-001",
                },
            ).json()
            == started.json()
        )
        rollback = client.put(
            f"/api/scenarios/main/work-orders/{assigned['work_order_id']}", json={"status": "pending"}
        )
        assert rollback.status_code == 409
        immutable = client.put(
            f"/api/scenarios/main/work-orders/{assigned['work_order_id']}", json={"title": "rewrite"}
        )
        assert immutable.status_code == 409
        started_lock = client.post(
            "/api/scenarios/main/lock",
            json={
                "work_order_id": assigned["work_order_id"],
                "technician_id": assigned["technician_id"],
                "locked": True,
            },
        )
        assert started_lock.status_code == 409
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/complete",
                json={
                    "technician_id": assigned["technician_id"],
                    "occurred_at": assigned["finish_time"],
                    "expected_revision": locked.json()["revision"],
                    "idempotency_key": "state-stale-complete-001",
                },
            ).status_code
            == 409
        )
        completed = client.post(
            f"/api/scenarios/main/work-orders/{assigned['work_order_id']}/complete",
            json={
                "technician_id": assigned["technician_id"],
                "occurred_at": assigned["finish_time"],
                "expected_revision": started.json()["scenario"]["revision"],
                "idempotency_key": "state-complete-001",
            },
        )
        assert completed.status_code == 200
        assert not any(
            item["work_order_id"] == assigned["work_order_id"]
            for item in completed.json()["scenario"]["locked_assignments"]
        )
        events = client.get("/api/scenarios/main/execution-events").json()
        assert [item["action"] for item in events] == ["start", "complete"]


def test_blank_names_labels_and_invalid_colors_are_rejected_as_validation_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "strings.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan_id = client.get("/api/scenarios/main/plan-versions").json()[0]["id"]
        assert (
            client.patch(
                f"/api/scenarios/main/plan-versions/{plan_id}",
                json={"label": "   "},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/strategy-profiles",
                json={"name": "   ", "description": "test", "time_limit_seconds": 1},
            ).status_code
            == 422
        )
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
        assert client.put("/api/scenarios/main/work-orders/WO-1021", json={"title": None}).status_code == 422
        assert client.put("/api/scenarios/main/technicians/TECH-01", json={"name": None}).status_code == 422
        invalid_shift = client.put("/api/scenarios/main/technicians/TECH-01", json={"shift_end": 400})
        assert invalid_shift.status_code == 422
        assert client.put("/api/scenarios/main/technicians/TECH-01", json={"color": " #315c4b "}).status_code == 200

        with_note = client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "  上门前联系  "})
        assert with_note.status_code == 200
        order = next(item for item in with_note.json()["work_orders"] if item["id"] == "WO-1021")
        assert order["note"] == "上门前联系"
        cleared = client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": None})
        assert cleared.status_code == 200
        order = next(item for item in cleared.json()["work_orders"] if item["id"] == "WO-1021")
        assert order["note"] == ""
        assert cleared.json()["revision"] == original["revision"] + 3


def test_compound_emergency_replan_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    emergency = {
        "id": "WO-EMG-IDEMP",
        "customer_name": "应急客户",
        "title": "机房断电",
        "required_skills": ["electrical"],
        "location": {"x": 50, "y": 50},
        "service_duration": 30,
        "window_start": 600,
        "window_end": 750,
        "sla_deadline": 660,
        "priority": "urgent",
        "drop_penalty": 10000,
        "status": "pending",
        "vip": True,
        "is_emergency": True,
        "reported_at": 600,
        "note": "",
    }
    request = {
        "emergency_order": emergency,
        "planning_time": 600,
        "current_time": 600,
        "time_limit_seconds": 1,
        "strategy": "stable",
        "idempotency_key": "emergency-idemp-001",
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
        revision = client.get("/api/scenarios/main").json()["revision"]
        assert (
            client.post(
                f"/api/scenarios/main/work-orders/{started['work_order_id']}/start",
                json={
                    "technician_id": started["technician_id"],
                    "occurred_at": started["start_time"],
                    "expected_revision": revision,
                    "idempotency_key": "context-start-001",
                },
            ).status_code
            == 200
        )
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
        candidate = client.get(f"/api/scenarios/main/schedule-candidates/{run['candidate_id']}").json()
        assert candidate["planning_context_hash"] == run["planning_context_hash"]


def test_failed_compound_emergency_replan_keeps_demand_and_last_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "emergency-rollback.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    emergency = {
        "id": "WO-EMG-ROLLBACK",
        "customer_name": "应急客户",
        "title": "机房断电",
        "required_skills": ["electrical"],
        "location": {"x": 50, "y": 50},
        "service_duration": 30,
        "window_start": 600,
        "window_end": 750,
        "sla_deadline": 660,
        "priority": "urgent",
        "drop_penalty": 10000,
        "status": "pending",
        "vip": True,
        "is_emergency": True,
        "reported_at": 600,
        "note": "",
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
                "planning_time": 600,
                "time_limit_seconds": 1,
                "emergency_order": emergency,
                "idempotency_key": "emergency-rollback-001",
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
                "planning_time": 600,
                "time_limit_seconds": 1,
                "emergency_order": emergency,
                "idempotency_key": "emergency-rollback-001",
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
        "id": "WO-EMG-PREPARE",
        "customer_name": "应急客户",
        "title": "机房断电",
        "required_skills": ["electrical"],
        "location": {"x": 50, "y": 50},
        "service_duration": 30,
        "window_start": 600,
        "window_end": 750,
        "sla_deadline": 660,
        "priority": "urgent",
        "drop_penalty": 10000,
        "status": "pending",
        "vip": True,
        "is_emergency": True,
        "reported_at": 600,
        "note": "",
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
        order = next(
            item for item in second.get("/api/scenarios/main").json()["work_orders"] if item["id"] == "WO-1021"
        )
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
        same_origin_custom_port = client.post(
            "/api/scenarios/main/baseline",
            headers={
                "Host": "127.0.0.1:8012",
                "Origin": "http://127.0.0.1:8012",
            },
        )
        assert same_origin_custom_port.status_code == 200
        allowed = client.post(
            "/api/scenarios/main/baseline",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert allowed.status_code == 200
    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200
        assert main_module.experiment_executor is not None
    assert main_module.experiment_executor is None
