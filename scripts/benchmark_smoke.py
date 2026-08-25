from __future__ import annotations

import json

from backend._version import __version__
from backend.decision import (
    DecisionAnalysisError,
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
    DecisionAnalysisContext,
    DecisionAnalysisScope,
    PlanVersion,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    Skill,
)
from backend.scheduler import baseline_schedule, calculate_kpis
from backend.travel import EuclideanTravelTimeProvider
from backend.verification import verify_schedule


def plan_for(scenario_id: str):
    scenario = get_fixture(scenario_id)
    schedule = baseline_schedule(scenario, 1)
    report = verify_schedule(scenario, schedule)
    assert report.publishable, [item.code for item in report.errors]
    return scenario, PlanVersion(
        id=f"BENCH-{scenario.id}",
        scenario_id=scenario.id,
        number=1,
        action="baseline",
        label="Benchmark baseline",
        data_revision=scenario.revision,
        created_at=schedule.created_at,
        scenario_snapshot=scenario,
        selected=schedule,
        scenario_snapshot_hash=content_hash(scenario),
    )


rows: list[dict[str, object]] = []

for name, fixture_id in (
    ("small", "main"),
    ("medium", "strategy-medium"),
    ("skill-shortage", "skill-shortage"),
    ("emergency", "emergency"),
):
    scenario, plan = plan_for(fixture_id)
    rows.append(
        {
            "name": name,
            "status": "verified",
            "orders": len(scenario.work_orders),
            "technicians": len(scenario.technicians),
            "signature": content_hash(plan.selected.assignments)[:12],
        }
    )

tight = get_fixture("main")
for order in tight.work_orders:
    order.window_end = min(order.window_end, order.window_start + 15)
tight_schedule = baseline_schedule(tight, 1)
assert verify_schedule(tight, tight_schedule).publishable
rows.append({"name": "tight-window", "status": "verified", "unassigned": len(tight_schedule.unassigned)})

infeasible = get_fixture("main")
infeasible.locked_assignments = []
for technician in infeasible.technicians:
    technician.skills = [Skill.electrical]
for order in infeasible.work_orders:
    order.required_skills = [Skill.hvac]
infeasible_schedule = baseline_schedule(infeasible, 1)
infeasible_report = verify_schedule(infeasible, infeasible_schedule)
assert not infeasible_report.publishable
assert "EMPTY_CANDIDATE" in {item.code for item in infeasible_report.errors}
assert len(infeasible_schedule.unassigned) == len(infeasible.work_orders)
rows.append(
    {
        "name": "infeasible",
        "status": "correctly-rejected",
        "reason": "EMPTY_CANDIDATE",
        "unassigned": len(infeasible_schedule.unassigned),
    }
)

scenario, plan = plan_for("main")
cost = cost_analysis(plan)
capacity = capacity_analysis(plan, CapacityAnalysisRequest())
risk_request = RiskSimulationRequest(seed=20260824, trials=50)
risk = simulate_plan_risk(plan, risk_request)
controlled_capacity = capacity_analysis(
    plan,
    CapacityAnalysisRequest(reference_mode=CapacityReferenceMode.controlled_reoptimization),
)
assert cost.breakdown.total_economic_impact_cents > 0
assert cost.breakdown.total_economic_impact_cents == (
    cost.breakdown.cash_operating_cost_cents + cost.breakdown.service_failure_loss_cents
)
assert len(capacity.options) == 6
assert capacity.selected_plan_signature == schedule_signature(plan.selected)
assert capacity.reference_schedule_signature == capacity.selected_plan_signature
assert capacity.reference_mode is CapacityReferenceMode.selected_plan_delta
assert controlled_capacity.reference_mode is CapacityReferenceMode.controlled_reoptimization
assert controlled_capacity.reference_solver_policy_fingerprint
assert all(item.option_id != "add_service_depot" for item in capacity.options)
assert all(not item.violations for item in capacity.options if item.feasible)
assert risk.late_minutes_p50 <= risk.late_minutes_p90 <= risk.late_minutes_p95
assert risk.full_day_total_late_minutes_p95 == risk.late_minutes_p95
assert risk.execution_policy is RiskExecutionPolicy.follow_published_schedule
assert risk.sla_rate_ci_low <= risk.expected_sla_on_time_rate <= risk.sla_rate_ci_high

delayed_plan = plan.model_copy(deep=True)
assert delayed_plan.scenario_snapshot is not None
delayed_assignment = next(item for item in delayed_plan.selected.assignments if item.work_order_id == "WO-1035")
delayed_order = next(
    item for item in delayed_plan.scenario_snapshot.work_orders if item.id == delayed_assignment.work_order_id
)
delayed_assignment.start_time += 20
delayed_assignment.finish_time += 20
delayed_assignment.sla_late_minutes = max(0, delayed_assignment.finish_time - delayed_order.sla_deadline)
delayed_technician = next(
    item for item in delayed_plan.scenario_snapshot.technicians if item.id == delayed_assignment.technician_id
)
delayed_route = sorted(
    (item for item in delayed_plan.selected.assignments if item.technician_id == delayed_technician.id),
    key=lambda item: item.sequence,
)
delayed_orders = {item.id: item for item in delayed_plan.scenario_snapshot.work_orders}
provider = EuclideanTravelTimeProvider()
start_index = delayed_route.index(delayed_assignment)
for previous, current in zip(delayed_route[start_index:], delayed_route[start_index + 1 :], strict=False):
    previous_order = delayed_orders[previous.work_order_id]
    current_order = delayed_orders[current.work_order_id]
    current.travel_minutes = provider.minutes(previous_order.location, current_order.location, previous.finish_time)
    current.arrival_time = previous.finish_time + current.travel_minutes
    current.start_time = max(current.start_time, current.arrival_time, current_order.window_start)
    current.finish_time = current.start_time + current_order.service_duration
    current.sla_late_minutes = max(0, current.finish_time - current_order.sla_deadline)
delayed_plan.selected.kpis = calculate_kpis(
    delayed_plan.scenario_snapshot,
    delayed_plan.selected.assignments,
    delayed_plan.selected.unassigned,
)
zero_noise = {
    "seed": 7,
    "trials": 50,
    "travel_delay_max_percent": 0,
    "service_duration_jitter_percent": 0,
    "technician_absence_basis_points": 0,
    "emergency_order_basis_points": 0,
    "customer_no_show_basis_points": 0,
}
follow = simulate_plan_risk(
    delayed_plan,
    RiskSimulationRequest.model_validate(
        {**zero_noise, "execution_policy": RiskExecutionPolicy.follow_published_schedule}
    ),
)
earliest = simulate_plan_risk(
    delayed_plan,
    RiskSimulationRequest.model_validate(
        {**zero_noise, "execution_policy": RiskExecutionPolicy.earliest_feasible_execution}
    ),
)
assert follow.late_minutes_p50 > earliest.late_minutes_p50

try:
    capacity_analysis(
        plan,
        CapacityAnalysisRequest(),
        EuclideanTravelTimeProvider(minutes_per_grid_unit=0.72),
    )
except DecisionAnalysisError as error:
    assert error.code == "TRAVEL_MODEL_NOT_AVAILABLE"
else:
    raise AssertionError("capacity analysis accepted a mismatched travel model")

try:
    cost_analysis(
        plan,
        context=DecisionAnalysisContext(analysis_scope=DecisionAnalysisScope.remaining_forecast),
    )
except DecisionAnalysisError as error:
    assert error.code == "ANALYSIS_SCOPE_MISMATCH"
else:
    raise AssertionError("decision analysis accepted an unimplemented remaining forecast")

rows.extend(
    [
        {"name": "replan", "status": "covered-by-regression", "test": "complete/start replan suite"},
        {
            "name": "complete-replan-replan",
            "status": "covered-by-regression",
            "test": "test_completed_work_can_be_replanned_multiple_times",
        },
        {
            "name": "started-nonfirst",
            "status": "covered-by-regression",
            "test": "test_started_nonfirst_assignment_becomes_future_sequence_one",
        },
        {
            "name": "active-overrun",
            "status": "covered-by-regression",
            "test": "test_active_service_overrun_is_not_silently_available",
        },
        {
            "name": "multi-depot",
            "status": "partial",
            "note": "per-technician multi-origin supported; depot inventory is not",
        },
        {
            "name": "crew",
            "status": "unsupported",
            "note": "simultaneous multi-technician visits are outside the current domain",
        },
        {"name": "cross-day", "status": "unsupported", "note": "single planning-day contract"},
        {"name": "parts-shortage", "status": "unsupported", "note": "parts inventory is outside the current domain"},
    ]
)

print(
    json.dumps(
        {
            "benchmark_version": "FIELD_SERVICE_BENCHMARK_V1",
            "fieldflow_version": __version__,
            "decision_checks": {
                "cost_total_economic_impact_cents": cost.breakdown.total_economic_impact_cents,
                "capacity_options": len(capacity.options),
                "capacity_reference_mode": capacity.reference_mode.value,
                "controlled_reference_mode": controlled_capacity.reference_mode.value,
                "risk_execution_policy": risk.execution_policy.value,
                "risk_input_hash": risk.simulation_input_hash,
            },
            "scenarios": rows,
        },
        ensure_ascii=False,
        indent=2,
    )
)
