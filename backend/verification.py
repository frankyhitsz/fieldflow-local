from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from .hashing import content_hash
from .models import (
    CoverageSummary,
    ScheduleAssignment,
    ScheduleResult,
    ScheduleScenario,
    ScheduleVerificationReport,
    SolverStatus,
    VerificationIssue,
)
from .scheduler import calculate_kpis, objective_breakdown
from .timeutils import travel_minutes

PUBLISHABLE_STATUSES = {
    SolverStatus.optimal,
    SolverStatus.feasible,
    SolverStatus.time_limit_feasible,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def verify_schedule(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    source: ScheduleResult | None = None,
) -> ScheduleVerificationReport:
    errors: list[VerificationIssue] = []
    warnings: list[VerificationIssue] = []

    def error(code: str, message: str, work_order_id: str | None = None, technician_id: str | None = None) -> None:
        errors.append(VerificationIssue(code=code, message=message, work_order_id=work_order_id, technician_id=technician_id))

    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    active_ids = {item.id for item in scenario.work_orders if item.status.value != "completed"}
    assignment_counts = Counter(item.work_order_id for item in result.assignments)
    unassigned_counts = Counter(item.work_order_id for item in result.unassigned)
    assignment_ids = set(assignment_counts)
    unassigned_ids = set(unassigned_counts)
    duplicate_assignments = sorted(item for item, count in assignment_counts.items() if count > 1)
    duplicate_unassigned = sorted(item for item, count in unassigned_counts.items() if count > 1)
    overlapping = sorted(assignment_ids & unassigned_ids)
    covered_active = (assignment_ids | unassigned_ids) & active_ids
    missing = sorted(active_ids - covered_active)

    for item in duplicate_assignments:
        error("DUPLICATE_ASSIGNMENT", f"{item}: assigned more than once", item)
    for item in duplicate_unassigned:
        error("DUPLICATE_UNASSIGNED", f"{item}: listed as unassigned more than once", item)
    for item in overlapping:
        error("ASSIGNED_AND_UNASSIGNED", f"{item}: appears in assignments and unassigned", item)
    for item in missing:
        error("MISSING_WORK_ORDER", f"{item}: active work order is missing from the candidate", item)
    for item in sorted((assignment_ids | unassigned_ids) - set(orders)):
        error("UNKNOWN_WORK_ORDER", f"{item}: work order does not exist", item)
    for item in sorted(unassigned_ids - active_ids):
        if item in orders:
            error("INACTIVE_WORK_ORDER_UNASSIGNED", f"{item}: completed work order cannot be unassigned", item)

    coverage = CoverageSummary(
        active_work_orders=len(active_ids),
        assigned_work_orders=len(assignment_ids & active_ids),
        unassigned_work_orders=len(unassigned_ids & active_ids),
        missing_work_orders=missing,
        duplicate_assignments=duplicate_assignments,
        duplicate_unassigned=duplicate_unassigned,
        overlapping_work_orders=overlapping,
    )

    if active_ids and not result.assignments:
        error("EMPTY_CANDIDATE", "candidate has no assignments")
    if result.scenario_id != scenario.id:
        error("SCENARIO_ID_MISMATCH", f"candidate scenario {result.scenario_id} does not match {scenario.id}")
    if result.scenario_revision != scenario.revision:
        error("SCENARIO_REVISION_MISMATCH", f"candidate D{result.scenario_revision:03d} does not match current D{scenario.revision:03d}")
    expected_snapshot_hash = content_hash(scenario)
    if result.scenario_snapshot_hash != expected_snapshot_hash:
        error("SCENARIO_HASH_MISMATCH", "candidate scenario snapshot hash does not match current data")
    if result.solver_status not in PUBLISHABLE_STATUSES or not result.solution_found:
        error("SOLVER_STATUS_NOT_PUBLISHABLE", f"solver status {result.solver_status.value} cannot be published")

    locked = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in result.assignments:
        order = orders.get(assignment.work_order_id)
        technician = technicians.get(assignment.technician_id)
        if order is None:
            continue
        if technician is None:
            error("UNKNOWN_TECHNICIAN", f"{assignment.work_order_id}: technician {assignment.technician_id} does not exist", assignment.work_order_id, assignment.technician_id)
            continue
        if not set(order.required_skills).issubset(set(technician.skills)):
            error("SKILL_MISMATCH", f"{order.id}: technician lacks required skill", order.id, technician.id)
        if not order.window_start <= assignment.start_time <= order.window_end:
            error("TIME_WINDOW_VIOLATION", f"{order.id}: start outside time window", order.id, technician.id)
        if assignment.arrival_time > assignment.start_time:
            error("ARRIVAL_AFTER_START", f"{order.id}: arrival occurs after service start", order.id, technician.id)
        if assignment.finish_time != assignment.start_time + order.service_duration:
            error("SERVICE_DURATION_MISMATCH", f"{order.id}: finish time does not match service duration", order.id, technician.id)
        if assignment.sla_late_minutes != max(0, assignment.finish_time - order.sla_deadline):
            error("SLA_LATENESS_MISMATCH", f"{order.id}: SLA lateness is inconsistent", order.id, technician.id)
        if assignment.start_time < technician.shift_start:
            error("BEFORE_SHIFT", f"{order.id}: starts before technician shift", order.id, technician.id)
        if assignment.finish_time > technician.shift_end + technician.overtime_limit:
            error("OVERTIME_LIMIT_EXCEEDED", f"{order.id}: exceeds overtime limit", order.id, technician.id)
        if locked.get(order.id) and assignment.technician_id != locked[order.id]:
            error("LOCKED_TECHNICIAN_CHANGED", f"{order.id}: locked technician changed", order.id, technician.id)
        grouped[technician.id].append(assignment)

    for technician_id, route in grouped.items():
        ordered = sorted(route, key=lambda item: item.sequence)
        if [item.sequence for item in ordered] != list(range(1, len(ordered) + 1)):
            error("NONCONTIGUOUS_SEQUENCE", f"{technician_id}: route sequence is not contiguous", technician_id=technician_id)
        for before, after in zip(ordered, ordered[1:], strict=False):
            travel = travel_minutes(orders[before.work_order_id].location, orders[after.work_order_id].location)
            if after.travel_minutes != travel:
                error("TRAVEL_TIME_MISMATCH", f"{after.work_order_id}: travel minutes do not match route", after.work_order_id, technician_id)
            if after.arrival_time < before.finish_time + travel:
                error("IMPOSSIBLE_ARRIVAL", f"{after.work_order_id}: arrival precedes prior service and travel", after.work_order_id, technician_id)
            if before.finish_time + travel > after.start_time:
                error("ROUTE_OVERLAP", f"{technician_id}: {before.work_order_id} overlaps travel to {after.work_order_id}", technician_id=technician_id)
        if ordered:
            technician = technicians[technician_id]
            first = ordered[0]
            first_travel = travel_minutes(technician.start_location, orders[first.work_order_id].location)
            if first.travel_minutes != first_travel:
                error("FIRST_LEG_TRAVEL_MISMATCH", f"{first.work_order_id}: first-leg travel minutes do not match depot", first.work_order_id, technician_id)
            if first.arrival_time < technician.shift_start + first_travel:
                error("IMPOSSIBLE_DEPOT_DEPARTURE", f"{first.work_order_id}: arrival precedes possible depot departure", first.work_order_id, technician_id)
            last = ordered[-1]
            return_travel = travel_minutes(orders[last.work_order_id].location, technician.start_location)
            if last.finish_time + return_travel > technician.shift_end + technician.overtime_limit:
                error("RETURN_EXCEEDS_OVERTIME", f"{technician_id}: route return exceeds overtime limit", technician_id=technician_id)

    if source is not None:
        source_by_id = {item.work_order_id: item for item in source.assignments}
        result_by_id = {item.work_order_id: item for item in result.assignments}
        for order in scenario.work_orders:
            if order.status.value not in {"started", "completed"}:
                continue
            old = source_by_id.get(order.id)
            new = result_by_id.get(order.id)
            if old is None:
                error("IMMUTABLE_SOURCE_MISSING", f"{order.id}: source plan has no immutable assignment", order.id)
            elif new is None or (new.technician_id, new.sequence, new.start_time, new.finish_time) != (old.technician_id, old.sequence, old.start_time, old.finish_time):
                error("IMMUTABLE_ASSIGNMENT_CHANGED", f"{order.id}: started or completed assignment changed", order.id)

    recomputed_kpis = None
    if not any(issue.code in {"UNKNOWN_WORK_ORDER", "UNKNOWN_TECHNICIAN", "DUPLICATE_ASSIGNMENT"} for issue in errors):
        recomputed_kpis = calculate_kpis(scenario, result.assignments, result.unassigned, source if result.kind == "replan" else None)
        if recomputed_kpis.model_dump() != result.kpis.model_dump():
            error("KPI_MISMATCH", "candidate KPI values do not match recomputed metrics")
        changes = sum(1 for item in result.assignments if item.changed) if result.kind == "replan" else 0
        recomputed_breakdown = objective_breakdown(scenario, recomputed_kpis, result.unassigned, result.assignments, changes)
        recomputed_score = round(sum(recomputed_breakdown.values()), 2)
        if recomputed_breakdown != result.objective_breakdown or result.objective != recomputed_score or result.business_score != recomputed_score:
            error("BUSINESS_SCORE_MISMATCH", "candidate business score does not match recomputed metrics")

    valid = not errors
    return ScheduleVerificationReport(
        valid=valid,
        publishable=valid and result.solver_status in PUBLISHABLE_STATUSES and result.solution_found,
        errors=errors,
        warnings=warnings,
        coverage=coverage,
        recomputed_kpis=recomputed_kpis,
        checked_at=_now(),
    )
