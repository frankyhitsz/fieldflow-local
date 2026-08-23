from backend.fixtures import get_fixture
from backend.models import LockedAssignment, Point, ScheduleAssignment, WorkOrder, WorkOrderStatus
from backend.scheduler import (
    baseline_schedule,
    optimized_schedule,
    replan_schedule,
    scenario_for_profile,
    validate_schedule,
)
from backend.storage import BUILTIN_PROFILES


def test_baseline_is_valid_and_deterministic():
    scenario = get_fixture("main")
    first = baseline_schedule(scenario, 1)
    second = baseline_schedule(scenario, 1)
    assert validate_schedule(scenario, first) == []
    assert [(a.work_order_id, a.technician_id, a.start_time) for a in first.assignments] == [
        (a.work_order_id, a.technician_id, a.start_time) for a in second.assignments
    ]
    assert all(item.reason.value for item in first.unassigned)
    assert first.kpis.sla_late_count == 4
    assert first.kpis.total_travel_minutes == 310
    assert first.kpis.total_overtime_minutes == 70
    assert first.kpis.unassigned_count == 2


def test_optimizer_improves_main_weighted_objective():
    scenario = get_fixture("main")
    baseline = baseline_schedule(scenario, 1)
    optimized = optimized_schedule(scenario, 2, baseline, time_limit_seconds=1)
    assert optimized.solver_status.value == "FEASIBLE"
    assert validate_schedule(scenario, optimized) == []
    assert optimized.objective < baseline.objective
    assert optimized.kpis.total_travel_minutes < baseline.kpis.total_travel_minutes
    assert optimized.kpis.total_overtime_minutes <= baseline.kpis.total_overtime_minutes
    assert "尚未完成全局最优性证明" in optimized.solver_note
    repeated = optimized_schedule(get_fixture("main"), 2, baseline, time_limit_seconds=1)
    assert repeated.solver_status.value in {"OPTIMAL", "FEASIBLE", "TIME_LIMIT_FEASIBLE"}
    assert [(a.work_order_id, a.technician_id, a.sequence, a.start_time) for a in repeated.assignments] == [
        (a.work_order_id, a.technician_id, a.sequence, a.start_time) for a in optimized.assignments
    ]


def test_skill_shortage_has_specific_reason_code():
    scenario = get_fixture("skill-shortage")
    result = baseline_schedule(scenario, 1)
    reason_by_id = {u.work_order_id: u.reason.value for u in result.unassigned}
    assert reason_by_id["WO-SKILL-01"] == "NO_ELIGIBLE_TECHNICIAN"
    optimized = optimized_schedule(scenario, 2, result, time_limit_seconds=1)
    assert not any(a.work_order_id == "WO-SKILL-01" for a in optimized.assignments)
    optimized_reasons = {u.work_order_id: u.reason.value for u in optimized.unassigned}
    assert optimized_reasons["WO-SKILL-01"] == "NO_ELIGIBLE_TECHNICIAN"


def test_locked_assignment_is_not_overridden():
    scenario = get_fixture("main")
    scenario.locked_assignments.append(LockedAssignment(work_order_id="WO-1021", technician_id="TECH-01"))
    result = optimized_schedule(scenario, 1, time_limit_seconds=1)
    assignment = next(a for a in result.assignments if a.work_order_id == "WO-1021")
    assert assignment.technician_id == "TECH-01"
    assert assignment.locked


def test_started_work_is_stable_during_replan():
    scenario = get_fixture("emergency")
    before = optimized_schedule(scenario, 1, time_limit_seconds=1)
    fixed = min(before.assignments, key=lambda a: a.start_time)
    next(o for o in scenario.work_orders if o.id == fixed.work_order_id).status = WorkOrderStatus.started
    after = replan_schedule(scenario, 2, before, current_time=fixed.start_time + 1, time_limit_seconds=1)
    preserved = next(a for a in after.assignments if a.work_order_id == fixed.work_order_id)
    assert (preserved.technician_id, preserved.sequence, preserved.start_time, preserved.finish_time) == (
        fixed.technician_id, fixed.sequence, fixed.start_time, fixed.finish_time
    )


def test_strategy_fixture_exposes_visible_business_tradeoffs():
    scenario = get_fixture("strategy-medium")
    results = {}
    signatures = set()
    for profile in BUILTIN_PROFILES:
        if profile.id == "stable":
            continue
        effective = scenario_for_profile(scenario, profile)
        baseline = baseline_schedule(effective, 0, profile.id)
        result = optimized_schedule(effective, 0, baseline, time_limit_seconds=1, strategy=profile.id)
        assert validate_schedule(effective, result) == []
        results[profile.id] = result
        signatures.add(tuple((item.work_order_id, item.technician_id, item.sequence) for item in result.assignments))
    assert len(signatures) >= 3
    assert results["completion"].kpis.completion_rate >= results["balanced"].kpis.completion_rate
    assert results["punctuality"].kpis.sla_on_time_rate >= results["balanced"].kpis.sla_on_time_rate
    assert results["low_travel"].kpis.total_travel_minutes <= results["balanced"].kpis.total_travel_minutes
    assert results["low_overtime"].kpis.total_overtime_minutes <= results["balanced"].kpis.total_overtime_minutes
    assert results["fair_workload"].kpis.normalized_workload_range <= results["balanced"].kpis.normalized_workload_range


def test_optimizer_allows_waits_longer_than_six_hours_and_records_real_arrival():
    scenario = get_fixture("main")
    scenario.work_orders = [WorkOrder(
        id="WO-LATE", customer_name="夜间客户", title="晚间维护",
        required_skills=[scenario.technicians[0].skills[0]], location=Point(x=50, y=52),
        service_duration=30, window_start=1200, window_end=1260, sla_deadline=1260,
    )]
    scenario.technicians = [scenario.technicians[0].model_copy(update={"shift_end": 1320})]
    result = optimized_schedule(scenario, 1, time_limit_seconds=1)
    assert validate_schedule(scenario, result) == []
    assignment = result.assignments[0]
    assert assignment.start_time >= 1200
    assert assignment.arrival_time < assignment.start_time


def test_assignment_explanation_uses_route_local_insertion_evidence():
    scenario = get_fixture("main")
    result = optimized_schedule(scenario, 1, time_limit_seconds=1)
    assignment = result.assignments[0]
    assert isinstance(assignment.evidence["route_insertion_travel_delta_minutes"], int)
    assert assignment.evidence["alternative_delta_scope"] == "travel_only_without_rescheduling"
    assert any("行程净增" in line for line in assignment.explanation)
    assert not any("至少减少" in line or "全局最优" in line for line in assignment.explanation)


def test_completed_prefix_does_not_push_completion_rate_above_one():
    scenario = get_fixture("emergency")
    before = optimized_schedule(scenario, 1, time_limit_seconds=1)
    completed = min(before.assignments, key=lambda item: item.start_time)
    next(order for order in scenario.work_orders if order.id == completed.work_order_id).status = WorkOrderStatus.completed
    after = replan_schedule(scenario, 2, before, current_time=completed.start_time - 1, time_limit_seconds=1)
    assert after.kpis.completion_rate <= 1
    assert next(item for item in after.assignments if item.work_order_id == completed.work_order_id).start_time == completed.start_time


def test_validator_reports_unknown_references_instead_of_crashing():
    scenario = get_fixture("main")
    result = baseline_schedule(scenario, 1)
    result.assignments.append(ScheduleAssignment(
        work_order_id="WO-UNKNOWN", technician_id="TECH-UNKNOWN", sequence=99,
        arrival_time=600, start_time=600, finish_time=630, travel_minutes=0,
        sla_late_minutes=0, explanation=[],
    ))
    assert "work order does not exist" in " ".join(validate_schedule(scenario, result))
