import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend.decision import (
    DecisionAnalysisError,
    analyze_plan_cost,
    build_simulation_scenario_set,
    canonical_decision_input_hash,
    capacity_analysis,
    cost_analysis,
    schedule_signature,
    simulate_plan_risk,
    validate_frozen_plan_integrity,
    verify_counterfactual_schedule,
)
from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import (
    AnalysisHorizon,
    AnalysisIntegrityStatus,
    CapacityAnalysisRequest,
    CapacityReferenceMode,
    CapacityVerificationReport,
    CapacityViolation,
    CostCadence,
    DecisionAnalysisContext,
    DecisionAnalysisScope,
    DecisionCostPolicy,
    EmergencyDispatchPolicy,
    LaborCostMode,
    PlanVersion,
    Point,
    PublicationVerificationArtifact,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    Skill,
    Technician,
    TechnicianArchetype,
    TechnicianUpdate,
    WorkOrderStatus,
)
from backend.provenance import build_plan_manifest_payload, decision_build_sha
from backend.scheduler import baseline_schedule, calculate_kpis
from backend.storage import Store
from backend.travel import EuclideanTravelTimeProvider


def _plan(fixture_id: str = "main") -> PlanVersion:
    scenario = get_fixture(fixture_id)
    schedule = baseline_schedule(scenario, 1)
    report: dict[str, object] = {}
    artifact_payload = {
        "policy_version": "FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        "candidate_snapshot": {},
        "planning_context_snapshot": None,
        "transaction_verification_report": report,
        "verified_schedule_hash": content_hash(schedule),
    }
    verification = PublicationVerificationArtifact(
        **artifact_payload,
        artifact_hash=content_hash(artifact_payload),
    )
    plan = PlanVersion(
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
        published_schedule_hash=content_hash(schedule),
        publication_verification_policy_version="FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
        publication_verification_report_hash=content_hash(report),
        publication_verification_artifact=verification,
        publication_manifest_version="FIELD_SERVICE_PUBLICATION_MANIFEST_V2",
        publication_manifest_hash="pending",
        integrity_status=AnalysisIntegrityStatus.verified,
        self_integrity=AnalysisIntegrityStatus.verified,
        effective_integrity=AnalysisIntegrityStatus.verified,
    )
    plan.publication_manifest_hash = content_hash(build_plan_manifest_payload(plan))
    return plan


def _refresh_plan_attestation(plan: PlanVersion) -> None:
    assert plan.publication_verification_artifact is not None
    plan.published_schedule_hash = content_hash(plan.selected)
    plan.publication_verification_artifact.verified_schedule_hash = plan.published_schedule_hash
    plan.publication_verification_artifact.artifact_hash = content_hash(
        plan.publication_verification_artifact.model_dump(exclude={"artifact_hash"}, mode="json")
    )
    plan.publication_manifest_hash = content_hash(build_plan_manifest_payload(plan))


def test_cost_model_uses_integer_cents_and_reconciles_total():
    plan = _plan()
    assert plan.scenario_snapshot is not None
    scenario = plan.scenario_snapshot
    assert scenario is not None
    breakdown = analyze_plan_cost(scenario, plan.selected)
    components = (
        breakdown.regular_labor_cost_cents,
        breakdown.overtime_base_cost_cents,
        breakdown.overtime_premium_cost_cents,
        breakdown.travel_cost_cents,
        breakdown.sla_penalty_cents,
        breakdown.unserved_revenue_cents,
        breakdown.outsourcing_cost_cents,
    )
    assert all(isinstance(item, int) and item >= 0 for item in components)
    assert breakdown.total_cost_cents == sum(components)
    assert breakdown.cash_operating_cost_cents == sum(components[index] for index in (0, 1, 2, 3, 6))
    assert breakdown.service_failure_loss_cents == components[4] + components[5]
    assert breakdown.total_economic_impact_cents == breakdown.total_cost_cents
    expected_labor = sum(
        kpi.occupied_minutes
        * next(item.cost_per_minute_cents for item in scenario.technicians if item.id == kpi.technician_id)
        for kpi in plan.selected.kpis.technician
    )
    assert breakdown.labor_cost_cents == expected_labor
    assert breakdown.regular_labor_cost_cents == breakdown.labor_cost_cents
    assert breakdown.overtime_premium_cost_cents == breakdown.overtime_cost_cents
    assert breakdown.overtime_base_cost_cents == 0


def test_occupied_minutes_overtime_base_is_already_in_labor():
    plan = _plan()
    result = analyze_plan_cost(
        plan.scenario_snapshot,
        plan.selected,
        DecisionCostPolicy(labor_cost_mode=LaborCostMode.occupied_minutes),
    )
    assert plan.selected.kpis.total_overtime_minutes > 0
    assert result.overtime_base_cost_cents == 0
    assert result.regular_labor_cost_cents == result.labor_cost_cents
    assert result.overtime_premium_cost_cents == result.overtime_cost_cents


def test_paid_shift_includes_overtime_base_wage():
    plan = _plan()
    result = analyze_plan_cost(
        plan.scenario_snapshot,
        plan.selected,
        DecisionCostPolicy(labor_cost_mode=LaborCostMode.paid_shift),
    )
    technicians = {item.id: item for item in plan.scenario_snapshot.technicians}
    expected = sum(
        item.overtime_minutes * technicians[item.technician_id].cost_per_minute_cents
        for item in plan.selected.kpis.technician
    )
    assert expected > 0
    assert result.overtime_base_cost_cents == expected


def test_paid_shift_with_50_percent_premium_pays_150_percent_overtime():
    plan = _plan()
    result = analyze_plan_cost(
        plan.scenario_snapshot,
        plan.selected,
        DecisionCostPolicy(
            labor_cost_mode=LaborCostMode.paid_shift,
            overtime_premium_basis_points=5_000,
        ),
    )
    assert result.overtime_premium_cost_cents == result.overtime_base_cost_cents // 2
    assert result.overtime_base_cost_cents + result.overtime_premium_cost_cents == (
        result.overtime_base_cost_cents * 3 // 2
    )


def test_no_overtime_same_result_for_both_overtime_components():
    plan = _plan()
    without_overtime = plan.selected.model_copy(deep=True)
    without_overtime.kpis.total_overtime_minutes = 0
    for item in without_overtime.kpis.technician:
        item.overtime_minutes = 0
    for mode in (LaborCostMode.occupied_minutes, LaborCostMode.paid_shift):
        result = analyze_plan_cost(
            plan.scenario_snapshot,
            without_overtime,
            DecisionCostPolicy(labor_cost_mode=mode),
        )
        assert result.overtime_base_cost_cents == 0
        assert result.overtime_premium_cost_cents == 0


@pytest.mark.parametrize("mode", [LaborCostMode.occupied_minutes, LaborCostMode.paid_shift])
def test_cash_operating_cost_reconciles_for_all_labor_modes(mode):
    plan = _plan()
    result = analyze_plan_cost(
        plan.scenario_snapshot,
        plan.selected,
        DecisionCostPolicy(labor_cost_mode=mode),
    )
    assert result.cash_operating_cost_cents == (
        result.regular_labor_cost_cents
        + result.overtime_base_cost_cents
        + result.overtime_premium_cost_cents
        + result.travel_cost_cents
        + result.outsourcing_cost_cents
    )


def test_salaried_allocation_remains_explicitly_unsupported():
    plan = _plan()
    with pytest.raises(DecisionAnalysisError) as caught:
        analyze_plan_cost(
            plan.scenario_snapshot,
            plan.selected,
            DecisionCostPolicy(labor_cost_mode=LaborCostMode.salaried_allocation),
        )
    assert caught.value.code == "LABOR_COST_MODE_NOT_SUPPORTED"


def test_capacity_analysis_declares_reference_mode_and_selected_plan_signature():
    plan = _plan("strategy-medium")
    result = capacity_analysis(plan, CapacityAnalysisRequest())
    assert result.reference_mode is CapacityReferenceMode.selected_plan_delta
    assert result.evaluation_method == "ROUTE_ENTRY_TAIL_APPEND_COUNTERFACTUAL_V4"
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
    assert all(
        isinstance(item.marginal_cost_cents, int)
        for item in result.options
        if item.decision_status.value == "INTERNAL_VERIFIED"
    )
    outsource = next(item for item in result.options if item.option_id == "outsource_unserved")
    assert outsource.decision_status.value == "EXTERNAL_CONDITIONAL"
    assert not outsource.feasible
    assert outsource.marginal_cost_cents is None
    assert outsource.conditional_upper_bound_kpis is not None
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
    _refresh_plan_attestation(second)
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
    _refresh_plan_attestation(plan)
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


def test_frozen_plan_integrity_detects_missing_coverage_even_without_legacy_schedule_hash():
    plan = _plan()
    plan.published_schedule_hash = ""
    removed = plan.selected.assignments.pop()
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
    with pytest.raises(DecisionAnalysisError) as caught:
        validate_frozen_plan_integrity(plan)
    assert caught.value.code in {
        "PUBLICATION_VERIFICATION_SCHEDULE_MISMATCH",
        "FROZEN_PLAN_INTEGRITY_FAILED",
    }
    if caught.value.code == "FROZEN_PLAN_INTEGRITY_FAILED":
        assert removed.work_order_id in {
            item["work_order_id"] for item in caught.value.details["violations"] if item["work_order_id"]
        }


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


def test_publication_context_hash_is_part_of_analysis_input():
    plan = _plan()
    provider = EuclideanTravelTimeProvider()
    context = DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.ex_ante_frozen_plan)
    plan.publication_planning_context_hash = "context-one"
    first = canonical_decision_input_hash(plan, "RISK", {"seed": 5}, context, provider)
    plan.publication_planning_context_hash = "context-two"
    second = canonical_decision_input_hash(plan, "RISK", {"seed": 5}, context, provider)
    assert first != second


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


def test_additional_technician_cost_mode_selects_wage_and_fixed_sources_once():
    plan = _plan()
    wage_only = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["add_technician"],
                "capacity_policy": {"add_technician_cost_mode": "WAGE_ONLY"},
            }
        ),
    ).options[0]
    combined = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["add_technician"],
                "capacity_policy": {"add_technician_cost_mode": "WAGE_PLUS_FIXED"},
            }
        ),
    ).options[0]
    fixed_only = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["add_technician"],
                "capacity_policy": {"add_technician_cost_mode": "FIXED_ONLY"},
            }
        ),
    ).options[0]
    assert wage_only.fixed_capacity_cost_cents == 0
    assert combined.fixed_capacity_cost_cents == 60_000
    assert fixed_only.fixed_capacity_cost_cents == 60_000
    assert combined.changed_inputs["cost_mode"] == "WAGE_PLUS_FIXED"
    assert fixed_only.daily_operating_delta_cents is not None
    assert combined.daily_operating_delta_cents is not None
    assert fixed_only.daily_operating_delta_cents <= combined.daily_operating_delta_cents


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
    assert with_fixed_cost.changed_inputs["outsourcing_cost_source"] == "CAPACITY_POLICY"
    assert with_fixed_cost.projected_total_cost_cents is None
    assert with_fixed_cost.diagnostic_metrics["horizon_total_impact_cents"] == (
        base.diagnostic_metrics["horizon_total_impact_cents"]
        - DecisionCostPolicy().outsourcing_cost_per_order_cents * outsourced
        + 4_321 * outsourced
    )


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
    assert first.scope_total_late_minutes_p50 == first.late_minutes_p50
    assert first.scope_total_late_minutes_p90 == first.late_minutes_p90
    assert first.scope_total_late_minutes_p95 == first.late_minutes_p95
    assert first.emergency_dispatch_policy is EmergencyDispatchPolicy.between_visits_only
    assert first.monte_carlo_mean_ci_low == first.sla_rate_ci_low
    assert first.monte_carlo_mean_ci_high == first.sla_rate_ci_high
    assert first.emergency_capacity_disruption_probability == first.emergency_caused_failure_probability
    assert first.emergency_caused_failure_probability <= first.emergency_event_probability
    for probability in (
        first.absence_disruption_probability,
        first.no_show_disruption_probability,
        first.window_failure_probability,
        first.overtime_failure_probability,
        first.emergency_capacity_disruption_probability,
    ):
        assert 0 <= probability <= 1


def test_emergency_event_without_business_harm_is_not_a_disruption():
    plan = _plan()
    for technician in plan.scenario_snapshot.technicians:
        technician.shift_start = 0
        technician.shift_end = 1800
        technician.overtime_limit = 240
        first = min(
            (item for item in plan.selected.assignments if item.technician_id == technician.id),
            key=lambda item: item.sequence,
            default=None,
        )
        if first:
            order = next(item for item in plan.scenario_snapshot.work_orders if item.id == first.work_order_id)
            first.travel_minutes = EuclideanTravelTimeProvider().minutes(
                technician.start_location,
                order.location,
                technician.shift_start,
            )
            first.arrival_time = technician.shift_start + first.travel_minutes
    for order in plan.scenario_snapshot.work_orders:
        order.window_end = 1800
        order.sla_deadline = 2280
    for assignment in plan.selected.assignments:
        assignment.sla_late_minutes = 0
    plan.scenario_snapshot_hash = content_hash(plan.scenario_snapshot)
    plan.selected.scenario_snapshot_hash = plan.scenario_snapshot_hash
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
    _refresh_plan_attestation(plan)
    result = simulate_plan_risk(
        plan,
        RiskSimulationRequest(
            seed=2,
            trials=50,
            travel_delay_max_percent=0,
            service_duration_jitter_percent=0,
            technician_absence_basis_points=0,
            emergency_order_basis_points=10_000,
            customer_no_show_basis_points=0,
        ),
    )
    assert result.emergency_event_probability == 1
    assert result.emergency_caused_failure_probability == 0
    assert result.additional_disruption_probability == 0


def test_two_plans_share_keyed_risk_scenario_set_for_paired_comparison():
    first = _plan()
    second = first.model_copy(deep=True)
    second.id = "PV-risk-second"
    second.number = 2
    second.selected.assignments[0].evidence["comparison_note"] = "different frozen plan"
    _refresh_plan_attestation(second)
    request = RiskSimulationRequest(seed=19, trials=50)
    first_result = simulate_plan_risk(first, request)
    second_result = simulate_plan_risk(second, request)
    assert first_result.simulation_scenario_set_hash == second_result.simulation_scenario_set_hash
    assert first_result.simulation_input_hash != second_result.simulation_input_hash


def test_emergency_demand_is_generated_before_plan_selects_a_responder():
    plan = _plan()
    assert plan.scenario_snapshot is not None
    manifest = build_simulation_scenario_set(
        plan.scenario_snapshot,
        RiskSimulationRequest(
            seed=23,
            trials=50,
            emergency_order_basis_points=10_000,
        ),
        23,
    )
    events = manifest["emergency_events"]
    assert isinstance(events, list) and events
    assert all(item["technician_id"] is None for item in events)
    assert {item["required_skill"] for item in events} <= {
        skill.value for technician in plan.scenario_snapshot.technicians for skill in technician.skills
    }
    manifest = build_simulation_scenario_set(
        plan.scenario_snapshot,
        RiskSimulationRequest(seed=23, trials=50, emergency_order_basis_points=10_000),
        23,
    )
    changed = json.loads(json.dumps(manifest))
    changed["emergency_events"][0]["duration_minutes"] += 1
    assert content_hash(manifest) != content_hash(changed)


def test_legacy_replan_without_publication_context_rejects_route_sensitive_analysis():
    plan = _plan()
    plan.selected.kind = "replan"
    _refresh_plan_attestation(plan)
    cost = cost_analysis(plan)
    assert "LEGACY_REPLAN_CONTEXT_WARNING" in "".join(cost.assumptions)
    with pytest.raises(DecisionAnalysisError, match="路线起点") as risk_error:
        simulate_plan_risk(plan, RiskSimulationRequest(seed=3, trials=50))
    assert risk_error.value.code == "REPLAN_ANALYSIS_CONTEXT_NOT_AVAILABLE"
    with pytest.raises(DecisionAnalysisError) as capacity_error:
        capacity_analysis(plan, CapacityAnalysisRequest(option_ids=["extend_shift"]))
    assert capacity_error.value.code == "REPLAN_ANALYSIS_CONTEXT_NOT_AVAILABLE"


def test_capacity_per_shift_cost_multiplies_affected_technicians_and_days():
    plan = _plan()
    result = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["extend_shift"],
                "analysis_horizon": {"days": 5},
                "capacity_policy": {
                    "extend_shift_fixed_cost_cents": 1_000,
                    "extend_shift_cost_cadence": "PER_SHIFT",
                },
            }
        ),
    ).options[0]
    assert result.cost_unit_type.value == "TECHNICIAN_SHIFT"
    assert result.cost_units_per_day == len(result.affected_entity_ids)
    assert result.cost_units_per_day > 1
    assert result.horizon_total_impact_cents == (
        result.daily_operating_delta_cents * 5 + 1_000 * result.cost_units_per_day * 5
    )


def test_capacity_per_order_cost_uses_targeted_work_orders():
    plan = _plan()
    result = capacity_analysis(
        plan,
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["outsource_unserved"],
                "analysis_horizon": {"days": 3},
                "capacity_policy": {
                    "outsource_unserved_fixed_cost_cents": 2_000,
                    "outsource_unserved_cost_cadence": "PER_ORDER",
                },
            }
        ),
    ).options[0]
    assert result.cost_unit_type.value == "WORK_ORDER"
    assert result.cost_units_per_day == len(result.affected_entity_ids)
    assert result.horizon_total_impact_cents is None
    assert result.diagnostic_metrics["horizon_total_impact_cents"] == (
        result.diagnostic_metrics["daily_operating_delta_cents"] * 3 + 2_000 * result.cost_units_per_day * 3
    )


def test_capacity_rejects_cadence_without_defined_units():
    with pytest.raises(ValueError, match="计量单位"):
        CapacityAnalysisRequest.model_validate(
            {
                "option_ids": ["add_skill"],
                "capacity_policy": {"add_skill_cost_cadence": "PER_SHIFT"},
            }
        )


def test_infeasible_capacity_option_has_only_diagnostic_metrics(monkeypatch):
    report = CapacityVerificationReport(
        valid=False,
        violations=[CapacityViolation(code="FORCED_INVALID", message="强制验证失败")],
    )
    original = verify_counterfactual_schedule

    def forced_invalid(*args, **kwargs):
        if kwargs.get("allow_started_first"):
            return original(*args, **kwargs)
        return report

    monkeypatch.setattr("backend.decision.verify_counterfactual_schedule", forced_invalid)
    result = capacity_analysis(_plan(), CapacityAnalysisRequest(option_ids=["extend_shift"])).options[0]
    assert result.feasible is False
    assert result.completion_rate is None
    assert result.daily_operating_delta_cents is None
    assert result.horizon_total_impact_cents is None
    assert result.economic_impact_offset_days is None
    assert result.cash_payback_days is None
    assert result.marginal_cost_cents is None
    assert result.diagnostic_metrics["completion_rate"] >= 0
    assert result.verification_report == report


def test_cost_analysis_input_hash_changes_with_labor_mode():
    occupied = cost_analysis(
        _plan(),
        policy=DecisionCostPolicy(labor_cost_mode=LaborCostMode.occupied_minutes),
    )
    paid = cost_analysis(
        _plan(),
        policy=DecisionCostPolicy(labor_cost_mode=LaborCostMode.paid_shift),
    )
    assert occupied.analysis_input_hash != paid.analysis_input_hash


def test_remaining_paid_shift_excludes_idle_technicians_and_separates_committed_cost():
    plan = _plan()
    assert plan.scenario_snapshot is not None
    scenario = plan.scenario_snapshot.model_copy(deep=True)
    template = scenario.technicians[0]
    idle = template.model_copy(update={"id": "TECH-IDLE", "name": "无剩余任务"}, deep=True)
    scenario.technicians.append(idle)
    schedule = plan.selected.model_copy(deep=True)
    schedule.kpis = calculate_kpis(scenario, schedule.assignments, schedule.unassigned)
    paid = analyze_plan_cost(
        scenario,
        schedule,
        DecisionCostPolicy(labor_cost_mode=LaborCostMode.paid_shift),
        paid_shift_default_start=720,
        paid_shift_only_if_scheduled=True,
    )
    assert paid.technician_cost_cents[idle.id] == 0
    assert paid.remaining_incremental_labor_cost_cents == paid.regular_labor_cost_cents
    assert paid.full_day_committed_labor_cost_cents > paid.remaining_incremental_labor_cost_cents


def test_emergency_demand_is_included_in_all_demand_sla_population():
    result = simulate_plan_risk(
        _plan(),
        RiskSimulationRequest(
            seed=37,
            trials=50,
            emergency_order_basis_points=10_000,
            technician_absence_basis_points=0,
            customer_no_show_basis_points=0,
            travel_delay_max_percent=0,
            service_duration_jitter_percent=0,
        ),
    )
    assert result.emergency_event_probability == 1
    assert result.emergency_completion_rate + result.emergency_unserved_probability == pytest.approx(1)
    assert all(metric.emergency_completed or not metric.emergency_on_time for metric in result.trial_metrics)
    assert any(metric.all_demand_sla_rate != metric.published_commitment_sla_rate for metric in result.trial_metrics)


def test_emergency_dispatch_uses_realized_travel_checkpoint_for_selection_and_execution():
    plan = _plan()
    assert plan.scenario_snapshot is not None
    for technician in plan.scenario_snapshot.technicians:
        technician.shift_end = 1600
        technician.overtime_limit = 240
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
    plan.scenario_snapshot_hash = content_hash(plan.scenario_snapshot)
    plan.selected.scenario_snapshot_hash = plan.scenario_snapshot_hash
    _refresh_plan_attestation(plan)
    request = RiskSimulationRequest(
        seed=23,
        trials=50,
        emergency_order_basis_points=10_000,
        technician_absence_basis_points=0,
        customer_no_show_basis_points=0,
        travel_delay_max_percent=100,
        service_duration_jitter_percent=25,
    )
    manifest = build_simulation_scenario_set(plan.scenario_snapshot, request, 23)
    event = next(item for item in manifest["emergency_events"] if item["trial"] == 10)
    metric = simulate_plan_risk(plan, request).trial_metrics[10]
    first_order = next(item for item in plan.scenario_snapshot.work_orders if item.id == "WO-1024")

    assert event["event_time"] == 553
    assert metric.emergency_technician_id == "TECH-04"
    assert metric.emergency_dispatch_time == 561
    assert metric.emergency_dispatch_time > event["event_time"]
    assert metric.emergency_dispatch_location == first_order.location
    assert metric.emergency_finish_time is not None
    assert metric.emergency_finish_time > metric.emergency_dispatch_time


def test_risk_simulation_respects_published_start_time_and_explicit_earliest_mode():
    plan = _plan()
    assignment = next(item for item in plan.selected.assignments if item.work_order_id == "WO-1035")
    assignment.start_time += 20
    assignment.finish_time += 20
    order = next(item for item in plan.scenario_snapshot.work_orders if item.id == assignment.work_order_id)
    assignment.sla_late_minutes = max(0, assignment.finish_time - order.sla_deadline)
    technician = next(item for item in plan.scenario_snapshot.technicians if item.id == assignment.technician_id)
    route = sorted(
        (item for item in plan.selected.assignments if item.technician_id == technician.id),
        key=lambda item: item.sequence,
    )
    orders = {item.id: item for item in plan.scenario_snapshot.work_orders}
    for previous, current in zip(route[route.index(assignment) :], route[route.index(assignment) + 1 :], strict=False):
        current_order = orders[current.work_order_id]
        previous_order = orders[previous.work_order_id]
        current.travel_minutes = EuclideanTravelTimeProvider().minutes(
            previous_order.location,
            current_order.location,
            previous.finish_time,
        )
        current.arrival_time = previous.finish_time + current.travel_minutes
        current.start_time = max(current.start_time, current.arrival_time, current_order.window_start)
        current.finish_time = current.start_time + current_order.service_duration
        current.sla_late_minutes = max(0, current.finish_time - current_order.sla_deadline)
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
    _refresh_plan_attestation(plan)
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
    assert result.analysis_scope.value == "FROZEN_FULL_PLAN"
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
        assert [
            item["number"]
            for item in client.get(f"/api/scenarios/main/plan-versions/{version_id}/analysis-runs").json()
        ] == [1, 2, 3]


def test_published_plan_integrity_is_attested_and_checked_by_analysis_report_and_activation(monkeypatch, tmp_path):
    database = tmp_path / "published-integrity.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        assert plan["published_schedule_hash"]
        assert plan["publication_verification_policy_version"] == "FIELD_SERVICE_PUBLICATION_VERIFICATION_V2"
        assert plan["publication_verification_report_hash"]
        assert plan["publication_verification_artifact"]["artifact_hash"]
        assert plan["publication_manifest_hash"]
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (plan["id"],)).fetchone()[0]
            )
            payload["selected"]["assignments"][0]["evidence"]["tampered_note"] = "changed after publication"
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), plan["id"]),
            )
        cost = client.get(f"/api/scenarios/main/plan-versions/{plan['id']}/cost-analysis")
        report = client.get(f"/api/scenarios/main/plan-versions/{plan['id']}/report")
        activation = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/activate",
            json={"expected_revision": 0, "idempotency_key": "integrity-activation-001"},
        )
        assert cost.status_code == report.status_code == activation.status_code == 409
        assert {response.json()["detail"]["code"] for response in (cost, report, activation)} == {
            "PLAN_INTEGRITY_FAILED"
        }


def test_plan_manifest_v2_detects_lineage_and_baseline_artifact_tampering(monkeypatch, tmp_path):
    database = tmp_path / "plan-manifest-v2.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1})
        baseline, optimized = client.get("/api/scenarios/main/plan-versions").json()
        with closing(sqlite3.connect(database)) as connection, connection:
            baseline_payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (baseline["id"],)).fetchone()[0]
            )
            baseline_payload["artifacts"][0]["schedule"]["objective"] += 1
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(baseline_payload, ensure_ascii=False), baseline["id"]),
            )
            optimized_payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (optimized["id"],)).fetchone()[0]
            )
            optimized_payload["lineage_source_version_id"] = "PV-FORGED"
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(optimized_payload, ensure_ascii=False), optimized["id"]),
            )
        assert (
            client.get(f"/api/scenarios/main/plan-versions/{baseline['id']}").json()["effective_integrity"] == "FAILED"
        )
        assert (
            client.get(f"/api/scenarios/main/plan-versions/{optimized['id']}").json()["effective_integrity"] == "FAILED"
        )


def test_generic_report_rejects_schedule_without_attested_plan(monkeypatch, tmp_path):
    database = tmp_path / "unattested-report.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        schedule = client.post("/api/scenarios/main/baseline").json()
        schedule["id"] = "SCH-UNATTESTED-TEST"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "INSERT INTO schedules(id, scenario_id, kind, version, payload) VALUES (?, ?, ?, ?, ?)",
                (schedule["id"], "main", schedule["kind"], 99, json.dumps(schedule, ensure_ascii=False)),
            )
        response = client.get(f"/api/scenarios/main/report?schedule_id={schedule['id']}")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "UNATTESTED_SCHEDULE"


def test_idempotent_publication_replay_rejects_failed_result_plan(monkeypatch, tmp_path):
    database = tmp_path / "failed-publication-replay.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        source = client.get("/api/scenarios/main/plan-versions").json()[0]
        request = {"expected_revision": 0, "idempotency_key": "failed-replay-activate-001"}
        first = client.post(f"/api/scenarios/main/plan-versions/{source['id']}/activate", json=request)
        assert first.status_code == 200
        published = first.json()
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (published["id"],)).fetchone()[0]
            )
            payload["selected"]["objective"] += 1
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), published["id"]),
            )
        replay = client.post(f"/api/scenarios/main/plan-versions/{source['id']}/activate", json=request)
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "PLAN_INTEGRITY_FAILED"
        assert len(client.get("/api/scenarios/main/plan-versions").json()) == 2


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
            json={"analysis_type": "RISK", "request": {"seed": 7, "trials": 50}},
        )
        selected_capacity = client.post(endpoint, json={"analysis_type": "CAPACITY"})
        controlled_capacity = client.post(
            endpoint,
            json={
                "analysis_type": "CAPACITY",
                "request": {"reference_mode": "CONTROLLED_REOPTIMIZATION"},
            },
        )

        assert (
            cost.status_code
            == risk.status_code
            == selected_capacity.status_code
            == controlled_capacity.status_code
            == 201
        )
        assert cost_replay.status_code == 200
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


def test_failed_plan_trust_check_does_not_consume_an_analysis_number(monkeypatch, tmp_path):
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
        assert first.status_code == replay.status_code == 409
        assert first.json()["detail"]["code"] == "PLAN_INTEGRITY_FAILED"
        assert replay.json()["detail"]["code"] == "PLAN_INTEGRITY_FAILED"
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1]
        assert client.get("/api/scenarios/main").json()["revision"] == 0
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM decision_analysis_runs").fetchone()[0] == 0


def test_analysis_finalization_reloads_parent_plan_and_rejects_mid_run_tampering(monkeypatch, tmp_path):
    database = tmp_path / "analysis-parent-finalization-race.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    original = main_module.cost_analysis

    def tamper_parent_after_computation(*args, **kwargs):
        result = original(*args, **kwargs)
        with closing(sqlite3.connect(database)) as connection, connection:
            plan_id, payload = connection.execute("SELECT id, payload FROM plan_versions LIMIT 1").fetchone()
            changed = json.loads(payload)
            changed["selected"]["solver_note"] = "tampered while A was running"
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(changed, ensure_ascii=False), plan_id),
            )
        return result

    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        monkeypatch.setattr(main_module, "cost_analysis", tamper_parent_after_computation)
        response = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        )
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["status"] == "FAILED"
        assert run["result"] is None
        assert run["error"]["code"] == "PARENT_PLAN_CHANGED_DURING_ANALYSIS"
        assert run["parent_plan_integrity"] == "FAILED"
        assert run["effective_integrity"] == "FAILED"
        assert run["artifact_manifest"] == []


def test_legacy_plan_is_view_only_until_reattestation_creates_a_new_version(monkeypatch, tmp_path):
    database = tmp_path / "legacy-reattestation.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        legacy = client.get("/api/scenarios/main/plan-versions").json()[0]
        assignment = baseline["assignments"][0]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER prevent_plan_attestation_change")
            connection.execute(
                "UPDATE plan_versions SET attestation_requirement='LEGACY_MIGRATED' WHERE id=?",
                (legacy["id"],),
            )
            payload_before = connection.execute(
                "SELECT payload FROM plan_versions WHERE id=?", (legacy["id"],)
            ).fetchone()[0]
            revision_rows_before = connection.execute("SELECT COUNT(*) FROM scenario_revisions").fetchone()[0]

        detail = client.get(f"/api/scenarios/main/plan-versions/{legacy['id']}").json()
        assert detail["effective_integrity"] == "LEGACY_UNATTESTED"
        assert client.get("/api/scenarios/main/schedules").json() == []
        analysis = client.post(
            f"/api/scenarios/main/plan-versions/{legacy['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        )
        execution = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "legacy-execution-blocked-001",
            },
        )
        assert analysis.status_code == execution.status_code == 409
        assert analysis.json()["detail"]["code"] == "PLAN_REATTESTATION_REQUIRED"
        assert execution.json()["detail"]["code"] == "PLAN_REATTESTATION_REQUIRED"
        assert client.get("/api/scenarios/main").json()["revision"] == 0

        reattested = client.post(
            f"/api/scenarios/main/plan-versions/{legacy['id']}/reattest",
            json={"expected_revision": 0, "idempotency_key": "legacy-reattest-001"},
        )
        assert reattested.status_code == 200, reattested.text
        new_plan = reattested.json()
        assert new_plan["number"] == 2
        assert new_plan["relation"] == "reattested_from"
        assert new_plan["source_version_id"] == legacy["id"]
        assert new_plan["effective_integrity"] == "VERIFIED"
        assert new_plan["schedule_integrity"] == "VERIFIED"
        assert new_plan["source_solver_provenance"] == legacy["selected"]["solver_name"]
        assert new_plan["inherited_source_solver_policy"] is not None
        assert new_plan["replay_validation_policy"] == "FIELD_SERVICE_REATTESTATION_V1"
        assert new_plan["reattestation_mode"] == "EXACT_SNAPSHOT"
        assert new_plan["publication_manifest_version"] == "FIELD_SERVICE_PUBLICATION_MANIFEST_V2"
        with closing(sqlite3.connect(database)) as connection:
            assert (
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (legacy["id"],)).fetchone()[0]
                == payload_before
            )
            assert connection.execute("SELECT COUNT(*) FROM decision_analysis_runs").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM work_order_execution_events").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM scenario_revisions").fetchone()[0] == revision_rows_before


def test_planning_equivalent_reattestation_ignores_metadata_but_exact_mode_does_not(monkeypatch, tmp_path):
    database = tmp_path / "planning-equivalent-reattestation.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        legacy = client.get("/api/scenarios/main/plan-versions").json()[0]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER prevent_plan_attestation_change")
            connection.execute(
                "UPDATE plan_versions SET attestation_requirement='LEGACY_MIGRATED' WHERE id=?",
                (legacy["id"],),
            )
        edited = client.put(
            "/api/scenarios/main/work-orders/WO-1021",
            json={"title": "只修改客户可见标题"},
        )
        assert edited.status_code == 200
        exact = client.post(
            f"/api/scenarios/main/plan-versions/{legacy['id']}/reattest",
            json={
                "expected_revision": 1,
                "idempotency_key": "reattest-exact-metadata-001",
                "mode": "EXACT_SNAPSHOT",
            },
        )
        assert exact.status_code == 409
        assert exact.json()["detail"]["code"] == "REATTESTATION_SNAPSHOT_MISMATCH"
        equivalent = client.post(
            f"/api/scenarios/main/plan-versions/{legacy['id']}/reattest",
            json={
                "expected_revision": 1,
                "idempotency_key": "reattest-equivalent-001",
                "mode": "PLANNING_EQUIVALENT",
            },
        )
        assert equivalent.status_code == 200, equivalent.text
        assert equivalent.json()["reattestation_mode"] == "PLANNING_EQUIVALENT"
        assert equivalent.json()["scenario_snapshot"]["work_orders"][0]["title"] == "只修改客户可见标题"


def test_analysis_run_request_is_discriminated_and_rejects_silent_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-discriminator.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"
        invalid = client.post(
            endpoint,
            json={
                "analysis_type": "COST",
                "request": {"analysis_horizon": {"days": 2}},
                "risk_request": {"trials": 50},
            },
        )
        assert invalid.status_code == 422
        valid = client.post(
            endpoint,
            json={"analysis_type": "COST", "request": {"analysis_horizon": {"days": 2}}},
        )
        assert valid.status_code == 201
        assert valid.json()["result"]["analysis_horizon"]["days"] == 2


def test_exact_retry_rejects_runtime_drift_and_points_to_current_rerun(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-retry-drift.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    original_cost_analysis = main_module.cost_analysis

    def fail_cost(*args, **kwargs):
        raise RuntimeError("injected failure")

    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        monkeypatch.setattr(main_module, "cost_analysis", fail_cost)
        failed = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        )
        assert failed.status_code == 201
        assert failed.json()["status"] == "FAILED"
        monkeypatch.setattr(main_module, "cost_analysis", original_cost_analysis)
        monkeypatch.setattr(main_module, "decision_build_sha", lambda: "different-build")
        retried = client.post(
            f"/api/scenarios/main/analysis-runs/{failed.json()['id']}/retry",
            headers={"Idempotency-Key": "retry-runtime-drift-001"},
        )
        assert retried.status_code == 409
        assert retried.json()["detail"]["code"] == "ANALYSIS_EXACT_RETRY_CONTEXT_CHANGED"
        assert retried.json()["detail"]["rerun_current_endpoint"].endswith("/rerun-current")


def test_concurrent_exact_retry_with_one_key_creates_one_attempt(monkeypatch, tmp_path):
    database = tmp_path / "decision-retry-concurrent.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)

    def fail_cost(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(main_module, "cost_analysis", fail_cost)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        failed = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        ).json()
        endpoint = f"/api/scenarios/main/analysis-runs/{failed['id']}/retry"

        def retry_once():
            return client.post(endpoint, headers={"Idempotency-Key": "concurrent-analysis-retry-001"})

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [future.result() for future in [executor.submit(retry_once), executor.submit(retry_once)]]
        successful = [response.json() for response in responses if response.status_code in {200, 201}]
        assert successful
        assert len({item["id"] for item in successful}) == 1
        with closing(sqlite3.connect(database)) as connection:
            attempts = connection.execute(
                "SELECT attempt_number FROM decision_analysis_attempts WHERE logical_analysis_id=? ORDER BY attempt_number",
                (failed["logical_analysis_id"],),
            ).fetchall()
        assert [item[0] for item in attempts] == [1, 2]


def test_terminal_analysis_payload_cannot_be_downgraded_to_running(monkeypatch, tmp_path):
    database = tmp_path / "decision-status.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"
        completed = client.post(endpoint, json={"analysis_type": "COST"})
        assert completed.status_code == 201
        assert client.post(endpoint, json={"analysis_type": "COST"}).status_code == 200
        with (
            closing(sqlite3.connect(database)) as connection,
            pytest.raises(
                sqlite3.IntegrityError,
                match="terminal analysis status is immutable",
            ),
        ):
            connection.execute(
                "UPDATE decision_analysis_runs SET status='RUNNING' WHERE id=?",
                (completed.json()["id"],),
            )
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE decision_analysis_runs SET started_at='forged' WHERE id=?",
                (completed.json()["id"],),
            )
        relational_tamper = client.get(f"/api/scenarios/main/analysis-runs/{completed.json()['id']}").json()
        assert relational_tamper["effective_integrity"] == "FAILED"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE decision_analysis_runs SET started_at=created_at WHERE id=?",
                (completed.json()["id"],),
            )
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = completed.json()
            payload["status"] = "RUNNING"
            payload["result"] = None
            payload["finished_at"] = None
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), payload["id"]),
            )
        replay = client.post(endpoint, json={"analysis_type": "COST"})
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "ANALYSIS_INTEGRITY_FAILED"


def test_capacity_analysis_run_persists_full_counterfactual_artifacts(monkeypatch, tmp_path):
    database = tmp_path / "decision-artifacts.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "CAPACITY"},
        )
        assert run.status_code == 201
        body = run.json()
        assert body["status"] == "COMPLETED"
        assert all(option["artifact_id"] for option in body["result"]["options"])
        assert all(option["diagnostic_schedule"] is None for option in body["result"]["options"])
        artifacts = client.get(f"/api/scenarios/main/analysis-runs/{body['id']}/artifacts")
        assert artifacts.status_code == 200
        assert len(artifacts.json()) == 6
        artifact = artifacts.json()[0]
        assert artifact["schedule"]["assignments"] is not None
        assert artifact["verification_report"]["violations"] is not None
        detail = client.get(f"/api/scenarios/main/analysis-runs/{body['id']}/artifacts/{artifact['id']}")
        assert detail.status_code == 200
        assert detail.json() == artifact
        assert artifact["integrity_status"] == "VERIFIED"
        outsourced = next(item for item in artifacts.json() if item["option_id"] == "outsource_unserved")
        option = next(item for item in body["result"]["options"] if item["option_id"] == "outsource_unserved")
        assert option["artifact_hash"] == outsourced["artifact_hash"]
        assert len(outsourced["external_assignments"]) == outsourced["counterfactual_kpis"]["external_assignment_count"]
        assert option["decision_status"] == "EXTERNAL_CONDITIONAL"
        assert option["completion_rate"] is None
        assert outsourced["decision_status"] == "EXTERNAL_CONDITIONAL"
        assert outsourced["formal_result_available"] is False
        assert outsourced["structural_verification"] == outsourced["verification_report"]
        assert outsourced["commercial_verification_status"] == "UNVERIFIED"
        assert outsourced["conditional_assumptions"]
        assert outsourced["conditional_upper_bound_kpis"] == option["conditional_upper_bound_kpis"]
        assert (
            outsourced["counterfactual_kpis"]["completion_rate"]
            == option["conditional_upper_bound_kpis"]["completion_rate"]
        )
        assert (
            len(outsourced["work_order_dispositions"]) == outsourced["counterfactual_kpis"]["active_work_order_count"]
        )
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload FROM decision_analysis_artifacts WHERE id=?", (outsourced["id"],)
                ).fetchone()[0]
            )
            payload["external_assignments"][0]["assumed_on_time"] = False
            connection.execute(
                "UPDATE decision_analysis_artifacts SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), outsourced["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{body['id']}").json()
        assert checked["integrity_status"] == "FAILED"
        checked_artifact = client.get(
            f"/api/scenarios/main/analysis-runs/{body['id']}/artifacts/{outsourced['id']}"
        ).json()
        assert checked_artifact["integrity_status"] == "FAILED"


def test_analysis_result_tampering_and_required_attestation_downgrade_fail_closed(monkeypatch, tmp_path):
    database = tmp_path / "analysis-result-integrity.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        first = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        ).json()
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (first["id"],)).fetchone()[
                    0
                ]
            )
            payload["result"]["breakdown"]["travel_cost_cents"] += 1
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), first["id"]),
            )
        assert client.get(f"/api/scenarios/main/analysis-runs/{first['id']}").json()["integrity_status"] == "FAILED"

        second = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST", "request": {"analysis_horizon": {"days": 2}}},
        ).json()
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (second["id"],)).fetchone()[
                    0
                ]
            )
            payload["result_hash"] = None
            payload["artifact_manifest"] = []
            payload["analysis_manifest_hash"] = None
            payload["integrity_status"] = "LEGACY_UNATTESTED"
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), second["id"]),
            )
        assert client.get(f"/api/scenarios/main/analysis-runs/{second['id']}").json()["integrity_status"] == "FAILED"


def test_analysis_effective_trust_fails_when_parent_plan_is_tampered(monkeypatch, tmp_path):
    database = tmp_path / "analysis-parent-plan.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        ).json()
        assert run["effective_integrity"] == "VERIFIED"
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (plan["id"],)).fetchone()[0]
            )
            payload["selected"]["objective"] += 1
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), plan["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}").json()
        assert checked["parent_plan_integrity"] == "FAILED"
        assert checked["effective_integrity"] == "FAILED"
        assert checked["result"] is None
        replay = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "PLAN_INTEGRITY_FAILED"


def test_artifact_effective_trust_is_bounded_by_parent_analysis(monkeypatch, tmp_path):
    database = tmp_path / "artifact-parent-analysis.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "CAPACITY", "request": {"option_ids": ["extend_shift"]}},
        ).json()
        artifact = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}/artifacts").json()[0]
        assert artifact["self_integrity"] == artifact["effective_integrity"] == "VERIFIED"
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (run["id"],)).fetchone()[0]
            )
            payload["result"]["options"][0]["name"] = "被篡改的名称"
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), run["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}/artifacts/{artifact['id']}").json()
        assert checked["self_integrity"] == "VERIFIED"
        assert checked["parent_analysis_integrity"] == "FAILED"
        assert checked["effective_integrity"] == "FAILED"


def test_direct_decision_endpoints_are_deprecated_in_openapi(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "decision-openapi.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/cost-analysis"]["get"]["deprecated"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/capacity-analysis"]["post"]["deprecated"]
        assert paths["/api/scenarios/{scenario_id}/plan-versions/{version_id}/risk-simulation"]["post"]["deprecated"]


def test_paired_risk_comparison_persists_two_runs_with_one_scenario_set(monkeypatch, tmp_path):
    database = tmp_path / "paired-risk.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        assert (
            client.post(
                "/api/scenarios/main/optimize",
                json={"strategy": "balanced", "time_limit_seconds": 1},
            ).status_code
            == 200
        )
        before, after = client.get("/api/scenarios/main/plan-versions").json()
        compared = client.post(
            f"/api/scenarios/main/risk-comparison?before={before['id']}&after={after['id']}",
            headers={"Idempotency-Key": "paired-risk-comparison-001"},
            json={"analysis_scope": "EX_ANTE_FROZEN_PLAN", "seed": 29, "trials": 50},
        )
        assert compared.status_code == 200, compared.text
        body = compared.json()
        assert body["scenario_set_hash"]
        assert body["before_analysis_id"] != body["after_analysis_id"]
        assert set(body["delta"]) == {
            "expected_sla_on_time_rate",
            "expected_overtime_minutes",
            "additional_disruption_probability",
            "expected_total_unserved_orders",
        }
        assert body["comparison_hash"] and body["integrity_status"] == "VERIFIED"
        for field in (
            "paired_sla_delta",
            "paired_all_demand_sla_delta",
            "paired_emergency_completion_delta",
            "paired_emergency_on_time_delta",
            "paired_overtime_delta",
            "paired_unserved_delta",
            "paired_disruption_delta",
        ):
            summary = body[field]
            assert summary["win_count"] + summary["tie_count"] + summary["loss_count"] == 50
            assert summary["ci_low"] <= summary["mean_delta"] <= summary["ci_high"]
        assert body["result"]["paired_published_sla_delta"] == body["paired_sla_delta"]
        stored = client.get(f"/api/scenarios/main/risk-comparisons/{body['id']}")
        assert stored.status_code == 200
        assert stored.json()["comparison_hash"] == body["comparison_hash"]
        replay = client.post(
            f"/api/scenarios/main/risk-comparison?before={before['id']}&after={after['id']}",
            headers={"Idempotency-Key": "paired-risk-comparison-001"},
            json={"analysis_scope": "EX_ANTE_FROZEN_PLAN", "seed": 29, "trials": 50},
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == body["id"]
        conflict = client.post(
            f"/api/scenarios/main/risk-comparison?before={before['id']}&after={after['id']}",
            headers={"Idempotency-Key": "paired-risk-comparison-001"},
            json={"analysis_scope": "EX_ANTE_FROZEN_PLAN", "seed": 30, "trials": 50},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        with closing(sqlite3.connect(database)) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="attestation requirement is immutable"):
                connection.execute(
                    "UPDATE risk_comparison_runs SET attestation_requirement='LEGACY_MIGRATED' WHERE id=?",
                    (body["id"],),
                )
            with pytest.raises(sqlite3.IntegrityError, match="LEGACY_MIGRATED is migration-only"):
                connection.execute(
                    """
                    INSERT INTO risk_comparison_runs(
                        id, scenario_id, number, comparison_hash, comparison_input_hash,
                        attestation_requirement, payload, created_at
                    )
                    SELECT 'RC-forged-legacy', scenario_id, number + 100, comparison_hash || '-legacy',
                           comparison_input_hash, 'LEGACY_MIGRATED', payload, created_at
                    FROM risk_comparison_runs WHERE id=?
                    """,
                    (body["id"],),
                )

        with closing(sqlite3.connect(database)) as connection, connection:
            analysis_payload = connection.execute(
                "SELECT payload FROM decision_analysis_runs WHERE id=?", (body["before_analysis_id"],)
            ).fetchone()[0]
            tampered_analysis = json.loads(analysis_payload)
            tampered_analysis["result"]["expected_sla_on_time_rate"] = 0
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(tampered_analysis, ensure_ascii=False), body["before_analysis_id"]),
            )
        invalid_parent = client.get(f"/api/scenarios/main/risk-comparisons/{body['id']}").json()
        assert invalid_parent["effective_integrity"] == "FAILED"
        assert invalid_parent["business_result_available"] is False
        assert invalid_parent["result"] is None
        assert invalid_parent["paired_sla_delta"] is None
        assert invalid_parent["delta"] == {}

        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (analysis_payload, body["before_analysis_id"]),
            )
            trial_payload = connection.execute(
                "SELECT payload FROM decision_analysis_artifacts WHERE id=?",
                (body["before_trial_artifact_id"],),
            ).fetchone()[0]
            tampered_trial = json.loads(trial_payload)
            tampered_trial["metrics"][0]["total_unserved_orders"] += 1
            connection.execute(
                "UPDATE decision_analysis_artifacts SET payload=? WHERE id=?",
                (json.dumps(tampered_trial, ensure_ascii=False), body["before_trial_artifact_id"]),
            )
        invalid_trial = client.get(f"/api/scenarios/main/risk-comparisons/{body['id']}").json()
        assert invalid_trial["effective_integrity"] == "FAILED"
        assert invalid_trial["business_result_available"] is False
        assert invalid_trial["result"] is None
        assert invalid_trial["paired_unserved_delta"] is None


def test_replan_analysis_detects_publication_route_entry_tampering(monkeypatch, tmp_path):
    database = tmp_path / "route-entry-tamper.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        assert (
            client.post("/api/scenarios/main/replan", json={"planning_time": 600, "time_limit_seconds": 1}).status_code
            == 200
        )
        plan = client.get("/api/scenarios/main/plan-versions").json()[-1]
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (plan["id"],)).fetchone()[0]
            )
            payload["publication_planning_context"]["route_entries"][0]["location"]["x"] += 1
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), plan["id"]),
            )
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={
                "analysis_type": "RISK",
                "analysis_scope": "EX_ANTE_FROZEN_PLAN",
                "request": {"seed": 31, "trials": 50},
            },
        )
        assert run.status_code == 409
        assert run.json()["detail"]["code"] == "PLAN_INTEGRITY_FAILED"
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM decision_analysis_runs").fetchone()[0] == 0


def test_started_execution_does_not_change_historical_full_plan_scope(monkeypatch, tmp_path):
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
        assert all(response.status_code == 200 for response in responses)
        explicit = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST", "analysis_scope": "EX_ANTE_FROZEN_PLAN"},
        )
        assert explicit.status_code == 201
        assert explicit.json()["status"] == "COMPLETED"
        assert explicit.json()["analysis_scope"] == "FROZEN_FULL_PLAN"
        assert explicit.json()["actual_execution_included"] is False
        assert explicit.json()["current_execution_watermark"] == 0


def test_completed_execution_does_not_change_historical_full_plan_scope(monkeypatch, tmp_path):
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
        assert implicit.status_code == 201
        explicit = client.post(
            endpoint,
            json={"analysis_type": "COST", "analysis_scope": "EX_ANTE_FROZEN_PLAN"},
        )
        assert explicit.status_code == 201
        body = explicit.json()
        assert body["status"] == "COMPLETED"
        assert body["analysis_scope"] == "FROZEN_FULL_PLAN"
        assert body["current_execution_watermark"] == 0
        assert body["analysis_as_of_time"] is None
        assert body["execution_context_hash"] is None
        assert body["actual_execution_included"] is False
        assert "完整冻结计划" in "".join(body["result"]["assumptions"])
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
        assert direct.json()["detail"]["code"] == persisted.json()["detail"]["code"] == "ANALYSIS_SCOPE_MISMATCH"


def test_tampered_analysis_cannot_be_replayed_or_consumed(monkeypatch, tmp_path):
    database = tmp_path / "analysis-replay-fail-closed.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        endpoint = f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs"
        run = client.post(endpoint, json={"analysis_type": "COST"}).json()
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (run["id"],)).fetchone()[0]
            )
            payload["result"]["breakdown"]["travel_cost_cents"] += 99
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), run["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}").json()
        listed = client.get(endpoint).json()[0]
        replay = client.post(endpoint, json={"analysis_type": "COST"})
        direct = client.get(f"/api/scenarios/main/plan-versions/{version['id']}/cost-analysis")
        assert checked["integrity_status"] == listed["integrity_status"] == "FAILED"
        assert checked["result"] is None and listed["result"] is None
        assert replay.status_code == direct.status_code == 409
        assert replay.json()["detail"]["code"] == direct.json()["detail"]["code"] == "ANALYSIS_INTEGRITY_FAILED"


@pytest.mark.parametrize("target", ["request", "policy", "context"])
def test_analysis_input_manifest_detects_snapshot_tampering(monkeypatch, tmp_path, target):
    database = tmp_path / f"analysis-input-{target}.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        version = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{version['id']}/analysis-runs",
            json={"analysis_type": "COST", "request": {"analysis_horizon": {"days": 3}}},
        ).json()
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (run["id"],)).fetchone()[0]
            )
            if target == "request":
                payload["request_snapshot"]["request"]["analysis_horizon"]["days"] = 4
            elif target == "policy":
                payload["policy_snapshot"]["analysis_horizon"]["days"] = 4
            else:
                payload["current_execution_watermark"] = 1
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), run["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}").json()
        assert checked["integrity_status"] == "FAILED"
        assert checked["result"] is None


def test_required_plan_and_artifact_cannot_downgrade_to_legacy(monkeypatch, tmp_path):
    database = tmp_path / "required-attestation.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        capacity = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "CAPACITY"},
        ).json()
        artifact = client.get(f"/api/scenarios/main/analysis-runs/{capacity['id']}/artifacts").json()[0]
        with closing(sqlite3.connect(database)) as connection, connection:
            plan_payload = json.loads(
                connection.execute("SELECT payload FROM plan_versions WHERE id=?", (plan["id"],)).fetchone()[0]
            )
            plan_payload["published_schedule_hash"] = ""
            plan_payload["publication_verification_artifact"] = None
            plan_payload["publication_manifest_hash"] = ""
            connection.execute(
                "UPDATE plan_versions SET payload=? WHERE id=?",
                (json.dumps(plan_payload, ensure_ascii=False), plan["id"]),
            )
            artifact_payload = json.loads(
                connection.execute(
                    "SELECT payload FROM decision_analysis_artifacts WHERE id=?", (artifact["id"],)
                ).fetchone()[0]
            )
            artifact_payload["artifact_hash"] = ""
            artifact_payload["integrity_status"] = "LEGACY_UNATTESTED"
            connection.execute(
                "UPDATE decision_analysis_artifacts SET payload=? WHERE id=?",
                (json.dumps(artifact_payload, ensure_ascii=False), artifact["id"]),
            )
        checked_plan = client.get(f"/api/scenarios/main/plan-versions/{plan['id']}").json()
        checked_artifact = client.get(
            f"/api/scenarios/main/analysis-runs/{capacity['id']}/artifacts/{artifact['id']}"
        ).json()
        checked_run = client.get(f"/api/scenarios/main/analysis-runs/{capacity['id']}").json()
        assert checked_plan["attestation_requirement"] == "REQUIRED"
        assert checked_plan["integrity_status"] == "FAILED"
        assert checked_artifact["attestation_requirement"] == "REQUIRED"
        assert checked_artifact["integrity_status"] == "FAILED"
        assert checked_run["integrity_status"] == "FAILED" and checked_run["result"] is None


def test_v17_records_are_migrated_as_explicit_legacy_attestation(monkeypatch, tmp_path):
    database = tmp_path / "legacy-attestation-migration.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        ).json()
    with closing(sqlite3.connect(database)) as connection, connection:
        for trigger in (
            "prevent_plan_attestation_change",
            "prevent_analysis_attestation_change",
            "prevent_artifact_attestation_change",
            "prevent_legacy_plan_insert",
            "prevent_legacy_analysis_insert",
            "prevent_legacy_artifact_insert",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("PRAGMA user_version=17")
    migrated = Store(database)
    legacy_plan = migrated.get_plan_version("main", plan["id"])
    legacy_run = migrated.get_decision_analysis_run("main", run["id"])
    assert legacy_plan is not None and legacy_plan.attestation_requirement.value == "LEGACY_MIGRATED"
    assert legacy_plan.integrity_status.value == "LEGACY_UNATTESTED"
    assert legacy_run is not None and legacy_run.attestation_requirement.value == "LEGACY_MIGRATED"
    assert legacy_run.integrity_status.value == "LEGACY_UNATTESTED"


def test_malformed_artifact_is_structured_and_invalidates_parent(monkeypatch, tmp_path):
    database = tmp_path / "malformed-artifact.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "CAPACITY"},
        ).json()
        artifact = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}/artifacts").json()[0]
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "UPDATE decision_analysis_artifacts SET payload=? WHERE id=?",
                ("{", artifact["id"]),
            )
        parent = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}").json()
        detail = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}/artifacts/{artifact['id']}")
        assert parent["integrity_status"] == "FAILED" and parent["result"] is None
        assert detail.status_code == 409
        assert detail.json()["detail"]["code"] == "MALFORMED_ATTESTED_RECORD"


def test_failed_error_payload_is_attested(monkeypatch, tmp_path):
    database = tmp_path / "failed-error-attestation.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={
                "analysis_type": "COST",
                "request": {"cost_policy": {"labor_cost_mode": "SALARIED_ALLOCATION"}},
            },
        ).json()
        assert run["status"] == "FAILED" and run["integrity_status"] == "VERIFIED"
        with closing(sqlite3.connect(database)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM decision_analysis_runs WHERE id=?", (run["id"],)).fetchone()[0]
            )
            payload["error"]["message"] = "tampered"
            connection.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), run["id"]),
            )
        checked = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}").json()
        assert checked["integrity_status"] == "FAILED"


def test_tail_append_uses_route_entry_when_future_route_is_empty():
    import backend.decision as decision_module

    scenario = get_fixture("main")
    schedule = baseline_schedule(scenario, 1)
    technician = scenario.technicians[0]
    order = next(item for item in scenario.work_orders if set(item.required_skills).issubset(set(technician.skills)))
    schedule.assignments = []
    schedule.unassigned = [
        next(item for item in baseline_schedule(scenario, 1).unassigned if item.work_order_id == order.id)
        if any(item.work_order_id == order.id for item in baseline_schedule(scenario, 1).unassigned)
        else decision_module.UnassignedWorkOrder(
            work_order_id=order.id,
            reason="DROPPED_BY_OBJECTIVE",
            detail="test",
        )
    ]
    entry = decision_module.RouteEntryContext(
        technician_id=technician.id,
        location=Point(x=order.location.x, y=order.location.y),
        available_at=max(technician.shift_start, order.window_start),
        return_location=technician.start_location,
        first_future_work_order_id=None,
    )
    result = decision_module._tail_append_counterfactual(
        scenario,
        schedule,
        "extend_shift",
        EuclideanTravelTimeProvider(),
        route_entries=[entry],
    )
    assignment = next(item for item in result.assignments if item.work_order_id == order.id)
    assert assignment.arrival_time == entry.available_at
    assert assignment.evidence["route_entry_origin"] == entry.location.model_dump(mode="json")


def test_unused_technician_absence_is_an_event_but_not_plan_harm(monkeypatch):
    import backend.decision as decision_module

    plan = _plan()
    assert plan.scenario_snapshot is not None
    used = {item.technician_id for item in plan.selected.assignments}
    template = plan.scenario_snapshot.technicians[0]
    unused = template.model_copy(update={"id": "TECH-UNUSED", "name": "未参与计划"}, deep=True)
    plan.scenario_snapshot.technicians.append(unused)
    plan.scenario_snapshot_hash = content_hash(plan.scenario_snapshot)
    plan.selected.scenario_snapshot_hash = plan.scenario_snapshot_hash
    plan.selected.kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
    )
    _refresh_plan_attestation(plan)
    original = decision_module._keyed_draw

    def controlled(seed, trial, event_type, *entity_ids, modulo):
        if event_type == "absence":
            return 0 if entity_ids and entity_ids[0] == unused.id else modulo - 1
        if event_type in {"emergency_event", "no_show"}:
            return modulo - 1
        return original(seed, trial, event_type, *entity_ids, modulo=modulo)

    monkeypatch.setattr(decision_module, "_keyed_draw", controlled)
    result = simulate_plan_risk(
        plan,
        RiskSimulationRequest(
            seed=41,
            trials=50,
            technician_absence_basis_points=500,
            emergency_order_basis_points=0,
            customer_no_show_basis_points=0,
            travel_delay_max_percent=0,
            service_duration_jitter_percent=0,
        ),
    )
    assert unused.id not in used
    assert result.technician_absence_event_probability == 1
    assert result.absence_caused_failure_probability == 0
    assert result.absence_disruption_probability == 0


def test_replan_future_events_start_at_publication_time():
    scenario = get_fixture("main")
    request = RiskSimulationRequest(seed=51, trials=50, emergency_order_basis_points=10_000)
    manifest = build_simulation_scenario_set(scenario, request, 51, analysis_as_of_time=720)
    events = manifest["emergency_events"]
    assert isinstance(events, list) and events
    assert all(item["event_time"] >= 720 for item in events)


def test_replan_cost_capacity_and_risk_share_publication_remaining_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "replan-analysis-scope.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        assignment = next(item for item in baseline["assignments"] if item["sequence"] == 1)
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": 0,
                "idempotency_key": "replan-analysis-start-001",
            },
        )
        assert started.status_code == 200
        replanned = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": assignment["start_time"], "time_limit_seconds": 1},
        )
        assert replanned.status_code == 200, replanned.text
        plan = next(item for item in client.get("/api/scenarios/main/plan-versions").json() if item["active"])
        endpoint = f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs"
        runs = [
            client.post(endpoint, json={"analysis_type": "COST"}).json(),
            client.post(endpoint, json={"analysis_type": "CAPACITY"}).json(),
            client.post(
                endpoint,
                json={"analysis_type": "RISK", "request": {"seed": 61, "trials": 50}},
            ).json(),
        ]
        assert all(item["status"] == "COMPLETED" for item in runs)
        assert {item["analysis_scope"] for item in runs} == {"PUBLICATION_REMAINING_PLAN"}
        signatures = {
            runs[0]["result"]["schedule_signature"],
            runs[1]["result"]["selected_plan_signature"],
            runs[2]["result"]["schedule_signature"],
        }
        assert len(signatures) == 1
        paid_shift = client.post(
            endpoint,
            json={
                "analysis_type": "COST",
                "request": {"cost_policy": {"labor_cost_mode": "PAID_SHIFT"}},
            },
        ).json()
        assert paid_shift["status"] == "COMPLETED"
        assert (
            paid_shift["result"]["breakdown"]["full_day_committed_labor_cost_cents"]
            >= paid_shift["result"]["breakdown"]["remaining_incremental_labor_cost_cents"]
        )
        for analysis_type in ("COST", "CAPACITY"):
            multi_day = client.post(
                endpoint,
                json={
                    "analysis_type": analysis_type,
                    "request": {"analysis_horizon": {"days": 2}},
                },
            ).json()
            assert multi_day["status"] == "FAILED"
            assert multi_day["error"]["code"] == "REMAINING_PLAN_HORIZON_MUST_BE_ONE"


def test_retry_command_recovers_after_crash_before_analysis_reservation(monkeypatch, tmp_path):
    database = tmp_path / "retry-crash-recovery.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        failed = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={
                "analysis_type": "COST",
                "request": {"cost_policy": {"labor_cost_mode": "SALARIED_ALLOCATION"}},
            },
        ).json()
        assert main_module.store is not None
        original_reserve = main_module.store.reserve_decision_analysis_run

        def crash_once(*args, **kwargs):
            raise RuntimeError("simulated crash before A reserve")

        monkeypatch.setattr(main_module.store, "reserve_decision_analysis_run", crash_once)
        first = client.post(
            f"/api/scenarios/main/analysis-runs/{failed['id']}/retry",
            headers={"Idempotency-Key": "retry-crash-key-001"},
        )
        assert first.status_code == 500
        monkeypatch.setattr(main_module.store, "reserve_decision_analysis_run", original_reserve)
        recovered = client.post(
            f"/api/scenarios/main/analysis-runs/{failed['id']}/retry",
            headers={"Idempotency-Key": "retry-crash-key-001"},
        )
        assert recovered.status_code == 201
        assert recovered.json()["attempt_number"] == 2


def test_rerun_current_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "rerun-idempotent.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        original = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "COST"},
        ).json()
        endpoint = f"/api/scenarios/main/analysis-runs/{original['id']}/rerun-current"
        first = client.post(endpoint, headers={"Idempotency-Key": "rerun-same-key-001"})
        replay = client.post(endpoint, headers={"Idempotency-Key": "rerun-same-key-001"})
        second = client.post(endpoint, headers={"Idempotency-Key": "rerun-other-key-001"})
        assert first.status_code == 201 and replay.status_code == 200 and second.status_code == 201
        assert first.json()["id"] == replay.json()["id"] != second.json()["id"]
        assert first.json()["logical_analysis_id"] != original["logical_analysis_id"]


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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20


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


def test_v14_analysis_unique_constraint_migrates_to_retryable_attempt_schema(tmp_path):
    database = tmp_path / "analysis-attempt-migration.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE decision_analysis_artifacts")
        connection.execute("DROP TABLE decision_analysis_runs")
        connection.execute(
            """
            CREATE TABLE decision_analysis_runs (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                plan_version_id TEXT NOT NULL,
                analysis_type TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scenario_id, number),
                UNIQUE(plan_version_id, analysis_type, input_hash)
            )
            """
        )
        connection.execute("PRAGMA user_version=14")

    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_analysis_runs'"
        ).fetchone()[0]
        assert "UNIQUE(plan_version_id, analysis_type, input_hash)" not in table_sql
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_analysis_artifacts'"
        ).fetchone()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20


def test_v14_retry_schema_migration_preserves_analysis_artifacts(monkeypatch, tmp_path):
    database = tmp_path / "analysis-artifact-preservation.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        run = client.post(
            f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs",
            json={"analysis_type": "CAPACITY"},
        ).json()
        artifacts = client.get(f"/api/scenarios/main/analysis-runs/{run['id']}/artifacts").json()
    artifact_hashes = {item["id"]: item["artifact_hash"] for item in artifacts}
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER IF EXISTS prevent_analysis_attestation_change")
        connection.execute("DROP TRIGGER IF EXISTS prevent_legacy_analysis_insert")
        connection.execute("ALTER TABLE decision_analysis_runs RENAME TO decision_analysis_runs_v18")
        connection.execute(
            """
            CREATE TABLE decision_analysis_runs (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                plan_version_id TEXT NOT NULL,
                analysis_type TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scenario_id, number),
                UNIQUE(plan_version_id, analysis_type, input_hash)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO decision_analysis_runs(
                id, scenario_id, number, plan_version_id, analysis_type, input_hash, payload, created_at
            )
            SELECT id, scenario_id, number, plan_version_id, analysis_type, input_hash, payload, created_at
            FROM decision_analysis_runs_v18
            """
        )
        connection.execute("DROP TABLE decision_analysis_runs_v18")
        connection.execute("PRAGMA user_version=14")
    Store(database)
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT id, payload FROM decision_analysis_artifacts ORDER BY id").fetchall()
        assert len(rows) == len(artifact_hashes)
        assert {row[0]: json.loads(row[1])["artifact_hash"] for row in rows} == artifact_hashes
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
