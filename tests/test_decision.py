import importlib
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.decision import (
    DecisionAnalysisError,
    analyze_plan_cost,
    capacity_analysis,
    cost_analysis,
    schedule_signature,
    simulate_plan_risk,
)
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import (
    CapacityAnalysisRequest,
    CapacityReferenceMode,
    PlanVersion,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    Technician,
    TechnicianUpdate,
    WorkOrderStatus,
)
from backend.scheduler import baseline_schedule
from backend.storage import Store
from backend.travel import EuclideanTravelTimeProvider


def _plan(fixture_id: str = "main") -> PlanVersion:
    scenario = get_fixture(fixture_id)
    schedule = baseline_schedule(scenario, 1)
    return PlanVersion(
        id=f"PV-{scenario.id}-decision",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="决策测算",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
    )


def test_cost_model_uses_integer_cents_and_reconciles_total():
    plan = _plan()
    scenario = plan.scenario_snapshot
    assert scenario is not None
    breakdown = analyze_plan_cost(scenario, plan.selected)
    components = (
        breakdown.labor_cost_cents,
        breakdown.travel_cost_cents,
        breakdown.overtime_cost_cents,
        breakdown.sla_penalty_cents,
        breakdown.unserved_revenue_cents,
        breakdown.outsourcing_cost_cents,
    )
    assert all(isinstance(item, int) and item >= 0 for item in components)
    assert breakdown.total_cost_cents == sum(components)
    assert breakdown.cash_operating_cost_cents == sum(components[index] for index in (0, 1, 2, 5))
    assert breakdown.service_failure_loss_cents == components[3] + components[4]
    assert breakdown.total_economic_impact_cents == breakdown.total_cost_cents
    expected_labor = sum(
        kpi.occupied_minutes
        * next(item.cost_per_minute_cents for item in scenario.technicians if item.id == kpi.technician_id)
        for kpi in plan.selected.kpis.technician
    )
    assert breakdown.labor_cost_cents == expected_labor


def test_capacity_analysis_declares_reference_mode_and_selected_plan_signature():
    plan = _plan("strategy-medium")
    result = capacity_analysis(plan, CapacityAnalysisRequest())
    assert result.reference_mode is CapacityReferenceMode.selected_plan_delta
    assert result.evaluation_method == "SELECTED_PLAN_ANCHORED_INCREMENTAL_GREEDY_V2"
    assert result.selected_plan_signature == schedule_signature(plan.selected)
    assert result.reference_schedule_signature == result.selected_plan_signature
    assert result.base_schedule_signature == result.reference_schedule_signature
    assert result.reference_solver_policy_fingerprint == plan.selected.solver_policy.fingerprint
    assert result.reference_travel_model_fingerprint == plan.selected.travel_model_fingerprint
    assert {item.option_id for item in result.options} == {
        "add_technician",
        "add_skill",
        "extend_shift",
        "allow_overtime",
        "outsource_unserved",
        "relocate_one_technician_start",
    }
    assert len({item.schedule_signature for item in result.options}) >= 2
    assert all(isinstance(item.marginal_cost_cents, int) for item in result.options)


def test_selected_plan_mode_uses_plan_selected_as_base():
    plan = _plan("strategy-medium")
    plan.selected.assignments[0].start_time += 1
    plan.selected.assignments[0].finish_time += 1
    result = capacity_analysis(
        plan,
        CapacityAnalysisRequest(reference_mode=CapacityReferenceMode.selected_plan_delta),
    )
    assert result.reference_schedule_signature == schedule_signature(plan.selected)
    assert result.reference_kpis == plan.selected.kpis


def test_controlled_mode_uses_identical_solver_policy():
    plan = _plan("strategy-medium")
    result = capacity_analysis(
        plan,
        CapacityAnalysisRequest(reference_mode=CapacityReferenceMode.controlled_reoptimization),
    )
    assert result.evaluation_method == "CONTROLLED_DETERMINISTIC_GREEDY_REOPTIMIZATION_V2"
    assert result.reference_mode is CapacityReferenceMode.controlled_reoptimization
    assert result.reference_solver_policy_fingerprint
    assert result.reference_schedule_signature == result.base_schedule_signature


def test_two_plans_same_snapshot_preserve_distinct_selected_references():
    first = _plan("strategy-medium")
    second = first.model_copy(deep=True)
    second.id = "PV-strategy-medium-second"
    second.number = 2
    second.selected.assignments[0].start_time += 1
    second.selected.assignments[0].finish_time += 1
    first_result = capacity_analysis(first, CapacityAnalysisRequest())
    second_result = capacity_analysis(second, CapacityAnalysisRequest())
    assert first_result.selected_plan_signature != second_result.selected_plan_signature
    assert first_result.reference_schedule_signature != second_result.reference_schedule_signature


def test_capacity_analysis_rejects_travel_model_mismatch():
    plan = _plan()
    provider = EuclideanTravelTimeProvider(minutes_per_grid_unit=0.72)
    with pytest.raises(DecisionAnalysisError, match="旅行模型") as caught:
        capacity_analysis(plan, CapacityAnalysisRequest(), provider)
    assert caught.value.code == "TRAVEL_MODEL_NOT_AVAILABLE"


def test_capacity_analysis_rejects_started_plan_without_context():
    plan = _plan()
    assigned_id = plan.selected.assignments[0].work_order_id
    next(item for item in plan.scenario_snapshot.work_orders if item.id == assigned_id).status = WorkOrderStatus.started
    with pytest.raises(DecisionAnalysisError, match="执行水位") as caught:
        capacity_analysis(plan, CapacityAnalysisRequest())
    assert caught.value.code == "EXECUTION_ANALYSIS_CONTEXT_REQUIRED"


def test_cost_analysis_rejects_completed_plan_without_context():
    plan = _plan()
    completed_id = plan.selected.assignments[0].work_order_id
    next(
        item for item in plan.scenario_snapshot.work_orders if item.id == completed_id
    ).status = WorkOrderStatus.completed
    with pytest.raises(DecisionAnalysisError, match="全日经营分析") as caught:
        cost_analysis(plan)
    assert caught.value.code == "EXECUTION_ANALYSIS_CONTEXT_REQUIRED"
    assert caught.value.details["completed_work_order_ids"] == [completed_id]


def test_relocation_option_is_not_labelled_as_new_depot_and_legacy_input_migrates():
    request = CapacityAnalysisRequest.model_validate({"option_ids": ["add_service_depot"]})
    result = capacity_analysis(_plan(), request)
    assert result.options[0].option_id == "relocate_one_technician_start"
    assert "站点" not in result.options[0].name
    assert "不创建站点" in result.options[0].assumption


def test_capacity_response_is_deterministic():
    plan = _plan("strategy-medium")
    request = CapacityAnalysisRequest(reference_mode=CapacityReferenceMode.controlled_reoptimization)
    assert capacity_analysis(plan, request).model_dump() == capacity_analysis(plan, request).model_dump()


def test_outsource_capacity_option_includes_configured_fixed_cost():
    plan = _plan()
    base = capacity_analysis(
        plan,
        CapacityAnalysisRequest(option_ids=["outsource_unserved"]),
    ).options[0]
    with_fixed_cost = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["outsource_unserved"],
                "capacity_policy": {"outsource_unserved_fixed_cost_cents": 4_321},
            }
        ),
    ).options[0]
    assert with_fixed_cost.fixed_capacity_cost_cents == 4_321
    assert with_fixed_cost.projected_total_cost_cents == base.projected_total_cost_cents + 4_321
    assert with_fixed_cost.marginal_cost_cents == base.marginal_cost_cents + 4_321


def test_risk_simulation_is_seeded_and_percentiles_are_monotonic():
    plan = _plan()
    request = RiskSimulationRequest(seed=314159, trials=100)
    first = simulate_plan_risk(plan, request)
    second = simulate_plan_risk(plan, request)
    assert first.model_dump() == second.model_dump()
    assert first.late_minutes_p50 <= first.late_minutes_p90 <= first.late_minutes_p95
    assert 0 <= first.expected_sla_on_time_rate <= 1
    assert 0 <= first.sla_rate_ci_low <= first.expected_sla_on_time_rate <= first.sla_rate_ci_high <= 1
    assert first.additional_disruption_probability == first.plan_failure_probability
    assert first.expected_total_unserved_orders == first.expected_unserved_orders


def test_risk_simulation_respects_published_start_time_and_explicit_earliest_mode():
    plan = _plan()
    assignment = plan.selected.assignments[0]
    order = next(item for item in plan.scenario_snapshot.work_orders if item.id == assignment.work_order_id)
    assignment.start_time = order.sla_deadline
    assignment.finish_time = assignment.start_time + order.service_duration
    common = {
        "seed": 11,
        "trials": 50,
        "travel_delay_max_percent": 0,
        "service_duration_jitter_percent": 0,
        "technician_absence_basis_points": 0,
        "emergency_order_basis_points": 0,
        "customer_no_show_basis_points": 0,
    }
    follow = simulate_plan_risk(
        plan,
        RiskSimulationRequest(**common, execution_policy=RiskExecutionPolicy.follow_published_schedule),
    )
    earliest = simulate_plan_risk(
        plan,
        RiskSimulationRequest(**common, execution_policy=RiskExecutionPolicy.earliest_feasible_execution),
    )
    assert follow.execution_policy is RiskExecutionPolicy.follow_published_schedule
    assert earliest.execution_policy is RiskExecutionPolicy.earliest_feasible_execution
    assert follow.late_minutes_p50 > earliest.late_minutes_p50
    assert follow.simulation_input_hash != earliest.simulation_input_hash


def test_cost_analysis_separates_cash_loss_and_total_impact():
    result = cost_analysis(_plan())
    breakdown = result.breakdown
    assert breakdown.total_economic_impact_cents == (
        breakdown.cash_operating_cost_cents + breakdown.service_failure_loss_cents
    )
    assert result.analysis_scope.value == "FULL_DAY_PLAN"


def test_decision_endpoints_use_frozen_plan_without_consuming_versions(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline")
        assert baseline.status_code == 200
        versions = client.get("/api/scenarios/main/plan-versions").json()
        assert [item["number"] for item in versions] == [1]
        version_id = versions[0]["id"]

        cost = client.get(f"/api/scenarios/main/plan-versions/{version_id}/cost-analysis")
        capacity = client.post(f"/api/scenarios/main/plan-versions/{version_id}/capacity-analysis", json={})
        risk = client.post(
            f"/api/scenarios/main/plan-versions/{version_id}/risk-simulation",
            json={"seed": 7, "trials": 50},
        )
        assert cost.status_code == capacity.status_code == risk.status_code == 200
        assert len(capacity.json()["options"]) == 6
        assert risk.json()["seed"] == 7
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]


def test_decision_analysis_runs_are_persisted_deduplicated_and_separately_numbered(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-runs.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"

        cost = client.post(endpoint, json={"analysis_type": "COST"})
        cost_replay = client.post(endpoint, json={"analysis_type": "COST"})
        risk = client.post(
            endpoint,
            json={"analysis_type": "RISK", "risk_request": {"seed": 7, "trials": 50}},
        )
        selected_capacity = client.post(endpoint, json={"analysis_type": "CAPACITY"})
        controlled_capacity = client.post(
            endpoint,
            json={
                "analysis_type": "CAPACITY",
                "capacity_request": {"reference_mode": "CONTROLLED_REOPTIMIZATION"},
            },
        )

        assert all(
            response.status_code == 201
            for response in (cost, cost_replay, risk, selected_capacity, controlled_capacity)
        )
        assert cost_replay.json()["id"] == cost.json()["id"]
        assert cost_replay.json()["number"] == 1
        assert [risk.json()["number"], selected_capacity.json()["number"], controlled_capacity.json()["number"]] == [
            2,
            3,
            4,
        ]
        assert controlled_capacity.json()["result"]["reference_mode"] == "CONTROLLED_REOPTIMIZATION"

        by_public_version = client.get("/api/scenarios/main/plan-versions/V001/analysis-runs")
        assert by_public_version.status_code == 200
        assert [item["number"] for item in by_public_version.json()] == [1, 2, 3, 4]
        assert client.get("/api/scenarios/main/analysis-runs/A001").json()["id"] == cost.json()["id"]
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]
        assert client.get("/api/scenarios/main").json()["revision"] == 0


def test_active_started_plan_is_rejected_without_execution_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-started.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "decision-started-reject-001",
            },
        )
        assert started.status_code == 200
        responses = (
            client.get(f"/api/scenarios/main/plan-versions/{version['id']}/cost-analysis"),
            client.post(f"/api/scenarios/main/plan-versions/{version['id']}/capacity-analysis", json={}),
            client.post(
                f"/api/scenarios/main/plan-versions/{version['id']}/risk-simulation",
                json={"seed": 7, "trials": 50},
            ),
            client.post(
                f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
                json={"analysis_type": "COST"},
            ),
        )
        assert all(response.status_code == 409 for response in responses)
        assert {response.json()["detail"]["code"] for response in responses} == {"EXECUTION_ANALYSIS_CONTEXT_REQUIRED"}


def test_v10_migration_preserves_legacy_technician_cost_value(tmp_path):
    database = tmp_path / "money-migration.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        payload = json.loads(connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0])
        payload["technicians"][0].pop("cost_per_minute_cents")
        payload["technicians"][0]["cost_per_minute"] = 1.25
        connection.execute(
            "UPDATE scenarios SET payload=? WHERE id='main'",
            (json.dumps(payload, ensure_ascii=False),),
        )
        connection.execute("PRAGMA user_version=9")

    migrated = Store(database).get_scenario("main")
    assert migrated is not None
    assert migrated.technicians[0].cost_per_minute_cents == 125
    with closing(sqlite3.connect(database)) as connection, connection:
        stored = json.loads(connection.execute("SELECT payload FROM scenarios WHERE id='main'").fetchone()[0])
        assert stored["technicians"][0]["cost_per_minute_cents"] == 125
        assert "cost_per_minute" not in stored["technicians"][0]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13


def test_legacy_technician_cost_input_remains_compatible():
    technician = Technician.model_validate(
        {
            "id": "TECH-LEGACY",
            "name": "旧客户端",
            "skills": ["electrical"],
            "shift_start": 480,
            "shift_end": 1020,
            "start_location": {"x": 50, "y": 50},
            "cost_per_minute": 1.75,
        }
    )
    update = TechnicianUpdate.model_validate({"cost_per_minute": 2.25})
    assert technician.cost_per_minute_cents == 175
    assert update.cost_per_minute_cents == 225
