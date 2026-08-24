import importlib
import json
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.decision import (
    DecisionAnalysisError,
    analyze_plan_cost,
    canonical_decision_input_hash,
    capacity_analysis,
    cost_analysis,
    schedule_signature,
    simulate_plan_risk,
    verify_counterfactual_schedule,
)
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import (
    AnalysisHorizon,
    CapacityAnalysisRequest,
    CapacityReferenceMode,
    CostCadence,
    DecisionAnalysisContext,
    DecisionAnalysisScope,
    DecisionCostPolicy,
    LaborCostMode,
    PlanVersion,
    Point,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    Skill,
    Technician,
    TechnicianArchetype,
    TechnicianUpdate,
    WorkOrderStatus,
)
from backend.provenance import decision_build_sha
from backend.scheduler import baseline_schedule, calculate_kpis
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
    assert result.evaluation_method == "SELECTED_PLAN_TAIL_APPEND_COUNTERFACTUAL_V3"
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
    assert all(item.feasible == (item.option_applicable and item.schedule_feasible) for item in result.options)


def test_selected_plan_mode_uses_plan_selected_as_base():
    plan = _plan("strategy-medium")
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
    second.selected.assignments[0].evidence["dispatcher_note"] = "同路线、不同冻结证据"
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


def test_capacity_analysis_rejects_tampered_snapshot_hash():
    plan = _plan()
    assigned_id = plan.selected.assignments[0].work_order_id
    next(item for item in plan.scenario_snapshot.work_orders if item.id == assigned_id).status = WorkOrderStatus.started
    with pytest.raises(DecisionAnalysisError, match="冻结哈希") as caught:
        capacity_analysis(plan, CapacityAnalysisRequest())
    assert caught.value.code == "PLAN_SNAPSHOT_HASH_MISMATCH"


def test_cost_analysis_accepts_explicit_ex_ante_snapshot_with_execution_state():
    plan = _plan()
    started_id = plan.selected.assignments[0].work_order_id
    next(item for item in plan.scenario_snapshot.work_orders if item.id == started_id).status = WorkOrderStatus.started
    plan.scenario_snapshot_hash = content_hash(plan.scenario_snapshot)
    plan.selected.scenario_snapshot_hash = plan.scenario_snapshot_hash
    result = cost_analysis(
        plan,
        context=DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.ex_ante_frozen_plan),
    )
    assert result.analysis_scope is DecisionAnalysisScope.ex_ante_frozen_plan
    assert result.actual_execution_included is False


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


def test_every_feasible_capacity_option_passes_full_verification():
    result = capacity_analysis(_plan("strategy-medium"), CapacityAnalysisRequest())
    assert result.options
    assert all(item.schedule_feasible for item in result.options if item.feasible)
    assert all(not item.violations for item in result.options if item.feasible)
    assert all(item.placement_mode.value == "TAIL_APPEND_ONLY" for item in result.options)


def test_capacity_verifier_checks_real_depot_return_and_fixed_assignments():
    plan = _plan()
    scenario = plan.scenario_snapshot.model_copy(deep=True)
    route = sorted(
        (item for item in plan.selected.assignments if item.technician_id == "TECH-01"),
        key=lambda item: item.sequence,
    )
    assert route
    technician = next(item for item in scenario.technicians if item.id == "TECH-01")
    technician.shift_end = route[-1].finish_time
    technician.overtime_limit = 0
    report = verify_counterfactual_schedule(
        scenario,
        plan.selected,
        EuclideanTravelTimeProvider(),
        fixed_schedule=plan.selected,
    )
    assert not report.valid
    assert "RETURN_OVERTIME_LIMIT_EXCEEDED" in {item.code for item in report.violations}

    changed = plan.selected.model_copy(deep=True)
    changed.assignments[0].start_time += 1
    changed.assignments[0].finish_time += 1
    changed_report = verify_counterfactual_schedule(
        plan.scenario_snapshot,
        changed,
        EuclideanTravelTimeProvider(),
        fixed_schedule=plan.selected,
    )
    assert "FIXED_ASSIGNMENT_CHANGED" in {item.code for item in changed_report.violations}


def test_decision_schedule_hash_covers_kpi_travel_and_evidence():
    original = _plan()
    variants = []
    kpi_changed = original.model_copy(deep=True)
    kpi_changed.selected.kpis.total_travel_minutes += 1
    variants.append(kpi_changed)
    travel_changed = original.model_copy(deep=True)
    travel_changed.selected.assignments[0].travel_minutes += 1
    variants.append(travel_changed)
    evidence_changed = original.model_copy(deep=True)
    evidence_changed.selected.assignments[0].evidence["audit_note"] = "changed"
    variants.append(evidence_changed)
    original_signature = schedule_signature(original.selected)
    assert all(schedule_signature(item.selected) != original_signature for item in variants)


def test_build_sha_is_part_of_canonical_decision_input_hash(monkeypatch):
    plan = _plan()
    provider = EuclideanTravelTimeProvider()
    context = DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.ex_ante_frozen_plan)
    monkeypatch.setenv("FIELDFLOW_BUILD_SHA", "commit-a")
    decision_build_sha.cache_clear()
    first = canonical_decision_input_hash(plan, "COST", {"policy": "same"}, context, provider)
    monkeypatch.setenv("FIELDFLOW_BUILD_SHA", "commit-b")
    decision_build_sha.cache_clear()
    second = canonical_decision_input_hash(plan, "COST", {"policy": "same"}, context, provider)
    monkeypatch.delenv("FIELDFLOW_BUILD_SHA")
    decision_build_sha.cache_clear()
    assert first != second


def test_analysis_scope_is_part_of_canonical_input_hash():
    plan = _plan()
    provider = EuclideanTravelTimeProvider()
    ex_ante = canonical_decision_input_hash(
        plan,
        "COST",
        {"policy": "same"},
        DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.ex_ante_frozen_plan),
        provider,
    )
    remaining = canonical_decision_input_hash(
        plan,
        "COST",
        {"policy": "same"},
        DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.remaining_forecast),
        provider,
    )
    assert ex_ante != remaining


def test_cost_horizon_and_capacity_cost_cadence_are_not_mixed():
    plan = _plan()
    daily = cost_analysis(plan, horizon=AnalysisHorizon(days=1))
    weekly = cost_analysis(plan, horizon=AnalysisHorizon(days=5))
    assert weekly.horizon_total_economic_impact_cents == daily.breakdown.total_economic_impact_cents * 5

    option = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["add_skill"],
                "analysis_horizon": {"days": 5},
                "capacity_policy": {
                    "add_skill_fixed_cost_cents": 12_345,
                    "add_skill_cost_cadence": CostCadence.one_time.value,
                },
            }
        ),
    ).options[0]
    assert option.fixed_cost_cadence is CostCadence.one_time
    assert option.one_time_investment_cents == 12_345
    assert option.horizon_total_impact_cents == option.daily_operating_delta_cents * 5 + 12_345


def test_paid_shift_labor_mode_uses_full_shift_minutes():
    plan = _plan()
    policy = DecisionCostPolicy(labor_cost_mode=LaborCostMode.paid_shift)
    result = analyze_plan_cost(plan.scenario_snapshot, plan.selected, policy)
    expected = sum(
        (item.shift_end - item.shift_start) * item.cost_per_minute_cents for item in plan.scenario_snapshot.technicians
    )
    assert result.labor_cost_cents == expected


def test_new_technician_uses_conservative_or_explicit_archetype():
    plan = _plan()
    inferred = capacity_analysis(
        plan,
        CapacityAnalysisRequest(option_ids=["add_technician"]),
    ).options[0]
    inferred_skills = set(inferred.changed_inputs["candidate_technician"]["skills"])
    assert inferred_skills
    assert inferred_skills != {item.value for item in Skill}

    archetype = TechnicianArchetype(
        name="暖通候选人",
        skills=[Skill.hvac],
        shift_start=480,
        shift_end=960,
        start_location=Point(x=40, y=40),
        overtime_limit=30,
        cost_per_minute_cents=150,
    )
    explicit = capacity_analysis(
        plan,
        CapacityAnalysisRequest(option_ids=["add_technician"], candidate_technician=archetype),
    ).options[0]
    assert set(explicit.changed_inputs["candidate_technician"]["skills"]) == {Skill.hvac}


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
    outsourced = len(plan.selected.unassigned)
    assert with_fixed_cost.projected_total_cost_cents == base.projected_total_cost_cents + 4_321 * outsourced
    assert with_fixed_cost.marginal_cost_cents == base.marginal_cost_cents + 4_321 * outsourced


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
    assert first.full_day_total_late_minutes_p50 == first.late_minutes_p50
    assert first.full_day_total_late_minutes_p90 == first.late_minutes_p90
    assert first.full_day_total_late_minutes_p95 == first.late_minutes_p95
    assert first.monte_carlo_mean_ci_low == first.sla_rate_ci_low
    assert first.monte_carlo_mean_ci_high == first.sla_rate_ci_high
    for probability in (
        first.absence_disruption_probability,
        first.no_show_disruption_probability,
        first.window_failure_probability,
        first.overtime_failure_probability,
        first.emergency_capacity_disruption_probability,
    ):
        assert 0 <= probability <= 1


def test_risk_simulation_respects_published_start_time_and_explicit_earliest_mode():
    plan = _plan()
    assignment = plan.selected.assignments[0]
    order = next(item for item in plan.scenario_snapshot.work_orders if item.id == assignment.work_order_id)
    assignment.start_time = order.sla_deadline
    assignment.finish_time = assignment.start_time + order.service_duration
    assignment.sla_late_minutes = max(0, assignment.finish_time - order.sla_deadline)
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
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
    assert result.analysis_scope.value == "EX_ANTE_FROZEN_PLAN"
    assert result.horizon_total_economic_impact_cents == result.breakdown.total_economic_impact_cents


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


def test_failed_decision_analysis_is_persisted_and_deduplicated(monkeypatch, tmp_path):
    database = tmp_path / "decision-failed.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (version["id"],)).fetchone()[0]
            )
            payload["selected"]["kpis"]["total_travel_minutes"] += 1
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), version["id"]),
            )
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"
        first = client.post(endpoint, json={"analysis_type": "COST"})
        replay = client.post(endpoint, json={"analysis_type": "COST"})
        assert first.status_code == replay.status_code == 201
        assert first.json()["status"] == "FAILED"
        assert first.json()["error"]["code"] == "SCHEDULE_KPI_INTEGRITY_FAILED"
        assert replay.json()["id"] == first.json()["id"]
        assert [item["number"] for item in client.get(endpoint).json()] == [1]
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]
        assert client.get("/api/scenarios/main").json()["revision"] == 0


def test_direct_decision_endpoints_are_deprecated_in_openapi(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-openapi.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/cost-analysis"]["get"]["deprecated"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/capacity-analysis"]["post"]["deprecated"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/risk-simulation"]["post"]["deprecated"]


def test_active_started_plan_requires_explicit_analysis_scope(monkeypatch, tmp_path):
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
        assert {response.json()["detail"]["code"] for response in responses} == {"ANALYSIS_SCOPE_REQUIRED"}
        explicit = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST", "analysis_scope": "EX_ANTE_FROZEN_PLAN"},
        )
        assert explicit.status_code == 201
        assert explicit.json()["status"] == "COMPLETED"
        assert explicit.json()["actual_execution_included"] is False
        assert explicit.json()["current_execution_watermark"] == 1


def test_active_completed_plan_requires_explicit_ex_ante_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-completed.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        start = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "decision-completed-start-001",
            },
        )
        assert start.status_code == 200
        complete = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["finish_time"],
                "expected_revision": 1,
                "idempotency_key": "decision-completed-finish-001",
            },
        )
        assert complete.status_code == 200
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"
        implicit = client.post(endpoint, json={"analysis_type": "COST"})
        assert implicit.status_code == 409
        assert implicit.json()["detail"]["code"] == "ANALYSIS_SCOPE_REQUIRED"
        explicit = client.post(
            endpoint,
            json={"analysis_type": "COST", "analysis_scope": "EX_ANTE_FROZEN_PLAN"},
        )
        assert explicit.status_code == 201
        body = explicit.json()
        assert body["status"] == "COMPLETED"
        assert body["analysis_scope"] == "EX_ANTE_FROZEN_PLAN"
        assert body["current_execution_watermark"] == 2
        assert body["analysis_as_of_time"] == assignment["finish_time"]
        assert body["execution_context_hash"]
        assert body["actual_execution_included"] is False
        assert "不含实际执行" in "".join(body["result"]["assumptions"])
        assert client.get(f"/api/scenarios/main/plan-versions/{version['id']}").json()["number"] == 1
        assert client.get("/api/scenarios/main").json()["revision"] == 2


def test_unimplemented_analysis_scope_returns_stable_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-scope.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        direct = client.get(
            f"/api/scenarios/main/plan-versions/{version['id']}/cost-analysis",
            params={"analysis_scope": "REMAINING_FORECAST"},
        )
        persisted = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST", "analysis_scope": "REMAINING_FORECAST"},
        )
        assert direct.status_code == persisted.status_code == 422
        assert direct.json()["detail"]["code"] == persisted.json()["detail"]["code"] == "ANALYSIS_SCOPE_NOT_SUPPORTED"


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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14


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
