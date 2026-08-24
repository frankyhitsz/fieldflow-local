from __future__ import annotations

import math
import statistics
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import ortools
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .hashing import content_hash
from .models import (
    PlanningContext,
    ScheduleAssignment,
    ScheduleKPI,
    ScheduleResult,
    ScheduleScenario,
    SolverStatus,
    StrategyProfile,
    Technician,
    TechnicianKPI,
    UnassignedReason,
    UnassignedWorkOrder,
    WorkOrder,
)
from .timeutils import hhmm, service_ready_at
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider

BUSINESS_SCORE_POLICY_VERSION = "FIELD_SERVICE_SCORE_V2"
METRIC_POLICY_VERSION = "FIELD_SERVICE_METRICS_V2"


ROUTING_STATUS_NAMES = {
    0: "ROUTING_NOT_SOLVED",
    1: "ROUTING_SUCCESS",
    2: "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
    3: "ROUTING_FAIL",
    4: "ROUTING_FAIL_TIMEOUT",
    5: "ROUTING_INVALID",
    6: "ROUTING_INFEASIBLE",
    7: "ROUTING_OPTIMAL",
}


def solver_status_from_routing(status_code: int, solution_found: bool) -> SolverStatus:
    if status_code == 7 and solution_found:
        return SolverStatus.optimal
    if status_code == 2 and solution_found:
        # This partial-search status does not identify the stopping condition.
        return SolverStatus.feasible
    if status_code == 1 and solution_found:
        return SolverStatus.feasible
    if status_code == 4:
        return SolverStatus.time_limit_feasible if solution_found else SolverStatus.time_limit_no_solution
    if status_code == 5:
        return SolverStatus.invalid_model
    if status_code == 6:
        return SolverStatus.infeasible
    if status_code == 3:
        return SolverStatus.no_solution
    return SolverStatus.failed


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _eligible(tech: Technician, order: WorkOrder) -> bool:
    return set(order.required_skills).issubset(set(tech.skills))


def _diagnose_unassigned(
    order: WorkOrder,
    scenario: ScheduleScenario,
    locked: dict[str, str],
    dropped_by_solver: bool = False,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> UnassignedWorkOrder:
    eligible = [t for t in scenario.technicians if _eligible(t, order)]
    if not eligible:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.no_eligible_technician,
            detail=f"没有技师同时具备 {', '.join(s.value for s in order.required_skills)} 技能",
            suggestions=["调整技能要求", "安排具备复合技能的外援"],
            evidence={"eligible_technician_ids": []},
        )
    locked_to = locked.get(order.id)
    if locked_to:
        tech = next((t for t in scenario.technicians if t.id == locked_to), None)
        if not tech or not _eligible(tech, order):
            return UnassignedWorkOrder(
                work_order_id=order.id,
                reason=UnassignedReason.locked_plan_conflict,
                detail=f"锁定技师 {locked_to} 不满足技能要求或已不可用",
                suggestions=["解除锁定", "改锁到具备所需技能的技师"],
                evidence={"locked_technician_id": locked_to, "eligible": False},
            )
        eligible = [tech]
    individually_feasible = []
    for tech in eligible:
        arrive = tech.shift_start + provider.minutes(tech.start_location, order.location)
        start = max(arrive, service_ready_at(order))
        finish = start + order.service_duration
        if start <= order.window_end:
            return_at = finish + provider.minutes(order.location, tech.start_location)
            individually_feasible.append((tech, return_at))
    if not individually_feasible:
        if locked_to:
            return UnassignedWorkOrder(
                work_order_id=order.id,
                reason=UnassignedReason.locked_plan_conflict,
                detail=f"锁定技师 {locked_to} 无法在客户时间窗内到达",
                suggestions=["解除锁定", "放宽客户时间窗", "调整指定技师班次"],
                evidence={"locked_technician_id": locked_to, "window_start": order.window_start, "window_end": order.window_end},
            )
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.time_window_infeasible,
            detail=f"最早可到达时间晚于客户时间窗 {hhmm(order.window_start)}–{hhmm(order.window_end)}",
            suggestions=["与客户协商放宽时间窗", "调整技师班次开始时间"],
            evidence={"eligible_technician_ids": [item.id for item in eligible], "window_start": order.window_start, "window_end": order.window_end},
        )
    if all(return_at > tech.shift_end + tech.overtime_limit for tech, return_at in individually_feasible):
        if locked_to:
            return UnassignedWorkOrder(
                work_order_id=order.id,
                reason=UnassignedReason.locked_plan_conflict,
                detail=f"锁定技师 {locked_to} 执行后会超过允许加班上限",
                suggestions=["解除锁定", "增加指定技师班次容量"],
                evidence={"locked_technician_id": locked_to, "overtime_limit": tech.overtime_limit},
            )
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.shift_capacity_exceeded,
            detail="可选技师即使单独执行也会超过允许加班上限",
            suggestions=["增加班次容量", "拆分或缩短服务任务"],
            evidence={"eligible_technician_ids": [item.id for item in eligible]},
        )
    if dropped_by_solver:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.dropped_by_objective,
            detail="按当前策略权重，这张工单没有进入最终路线",
            suggestions=["提高工单优先级", "放宽时间窗", "增加可用技师"],
            evidence={"eligible_technician_ids": [item.id for item in eligible], "drop_penalty": order.drop_penalty},
        )
    if locked_to:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.locked_plan_conflict,
            detail=f"锁定技师 {locked_to} 的现有路线无法同时满足该承诺",
            suggestions=["解除锁定", "调整同一技师的其他工单", "放宽客户时间窗"],
            evidence={"locked_technician_id": locked_to},
        )
    return UnassignedWorkOrder(
        work_order_id=order.id,
        reason=UnassignedReason.shift_capacity_exceeded,
        detail="当前路线剩余容量无法容纳此工单",
        suggestions=["允许更长加班", "将低优先级工单移至下一服务日"],
        evidence={"eligible_technician_ids": [item.id for item in eligible]},
    )


def _explanation(
    order: WorkOrder,
    tech: Technician,
    start: int,
    travel: int,
    finish: int,
    scenario: ScheduleScenario,
    locked: bool,
    changed: bool,
) -> list[str]:
    skill_text = "、".join(s.value for s in order.required_skills)
    items = [
        f"{tech.id} 具备 {skill_text} 技能",
        f"预计 {hhmm(start)} 开始，满足 {hhmm(order.window_start)}–{hhmm(order.window_end)} 客户时间窗",
        f"本段行程 {travel} 分钟，服务预计于 {hhmm(finish)} 完成",
    ]
    eligible = [item.id for item in scenario.technicians if _eligible(item, order)]
    if len(eligible) > 1:
        items.append(f"共有 {len(eligible)} 名技师满足技能要求；路线计算选择 {tech.id}")
    items.append("不会产生加班" if finish <= tech.shift_end else f"产生 {finish - tech.shift_end} 分钟加班，未超过上限")
    if locked:
        items.append("人工锁定已生效，本次求解保持指定技师")
    if changed:
        items.append("为接纳突发工单或降低 SLA 风险，本项安排发生调整")
    if order.vip:
        items.append("VIP 工单的未分配代价较高")
    return items


def _attach_route_insertion_evidence(
    scenario: ScheduleScenario,
    assignments: list[ScheduleAssignment],
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> None:
    """Record route-local travel deltas without claiming a global counterfactual optimum."""
    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in assignments:
        grouped[assignment.technician_id].append(assignment)
    for route in grouped.values():
        route.sort(key=lambda item: item.sequence)

    def cheapest_delta(technician: Technician, route: list[ScheduleAssignment], order: WorkOrder) -> int:
        points = [technician.start_location, *[orders[item.work_order_id].location for item in route], technician.start_location]
        return min(
            provider.minutes(points[index], order.location)
            + provider.minutes(order.location, points[index + 1])
            - provider.minutes(points[index], points[index + 1])
            for index in range(len(points) - 1)
        )

    for technician_id, route in grouped.items():
        technician = technicians[technician_id]
        last_order = orders[route[-1].work_order_id]
        return_travel = provider.minutes(last_order.location, technician.start_location)
        route_return_at = route[-1].finish_time + return_travel
        route_overtime = max(0, route_return_at - technician.shift_end)
        for index, assignment in enumerate(route):
            order = orders[assignment.work_order_id]
            previous_point = technician.start_location if index == 0 else orders[route[index - 1].work_order_id].location
            next_point = technician.start_location if index == len(route) - 1 else orders[route[index + 1].work_order_id].location
            insertion_delta = (
                provider.minutes(previous_point, order.location)
                + provider.minutes(order.location, next_point)
                - provider.minutes(previous_point, next_point)
            )
            alternatives = {
                item.id: cheapest_delta(item, grouped.get(item.id, []), order)
                for item in scenario.technicians
                if item.id != technician_id and _eligible(item, order)
            }
            assignment.evidence["route_insertion_travel_delta_minutes"] = insertion_delta
            assignment.evidence["alternative_route_travel_delta_minutes"] = alternatives
            assignment.evidence["alternative_delta_scope"] = "travel_only_without_rescheduling"
            assignment.evidence["reported_at"] = order.reported_at
            assignment.evidence["service_ready_at"] = service_ready_at(order)
            assignment.evidence["route_return_travel_minutes"] = return_travel
            assignment.evidence["route_return_at"] = route_return_at
            assignment.evidence["overtime_minutes"] = route_overtime
            overtime_text = (
                "不会产生加班"
                if route_overtime == 0
                else f"含返程预计产生 {route_overtime} 分钟加班，未超过上限"
            )
            for explanation_index, line in enumerate(assignment.explanation):
                if line == "不会产生加班" or line.startswith("产生 "):
                    assignment.explanation[explanation_index] = overtime_text
                    break
            assignment.explanation.append(f"在当前路线的前后工单之间插入此单，行程净增 {insertion_delta} 分钟")
            if alternatives:
                assignment.explanation.append(
                    f"其他技能匹配路线的最低行程增量估算为 {min(alternatives.values())} 分钟（未重新排时）"
                )


def _mark_replan_changes(
    assignments: list[ScheduleAssignment],
    previous: ScheduleResult,
    relevant_order_ids: set[str],
) -> None:
    previous_items = [item for item in previous.assignments if item.work_order_id in relevant_order_ids]
    old_predecessor: dict[str, str | None] = {}
    new_predecessor: dict[str, str | None] = {}
    old_by_id = {item.work_order_id: item for item in previous_items}
    for source, target in ((previous_items, old_predecessor), (assignments, new_predecessor)):
        grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
        for item in source:
            grouped[item.technician_id].append(item)
        for route in grouped.values():
            predecessor: str | None = None
            for item in sorted(route, key=lambda assignment: assignment.sequence):
                target[item.work_order_id] = predecessor
                predecessor = item.work_order_id
    for item in assignments:
        old = old_by_id.get(item.work_order_id)
        item.changed = bool(
            old
            and (
                old.technician_id != item.technician_id
                or old_predecessor.get(item.work_order_id) != new_predecessor.get(item.work_order_id)
            )
        )


def calculate_kpis(
    scenario: ScheduleScenario,
    assignments: list[ScheduleAssignment],
    unassigned: list[UnassignedWorkOrder],
    previous: ScheduleResult | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleKPI:
    orders = {o.id: o for o in scenario.work_orders}
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in assignments:
        grouped[assignment.technician_id].append(assignment)

    tech_kpis: list[TechnicianKPI] = []
    workloads: list[int] = []
    total_travel = 0
    total_service = 0
    total_overtime = 0
    total_waiting = 0
    for tech in scenario.technicians:
        route = sorted(grouped.get(tech.id, []), key=lambda a: a.sequence)
        service = sum(orders[a.work_order_id].service_duration for a in route)
        route_travel = sum(a.travel_minutes for a in route)
        waiting = sum(max(0, item.start_time - item.arrival_time) for item in route)
        if route:
            route_travel += provider.minutes(orders[route[-1].work_order_id].location, tech.start_location)
            route_end = route[-1].finish_time + provider.minutes(orders[route[-1].work_order_id].location, tech.start_location)
        else:
            route_end = tech.shift_start
        overtime = max(0, route_end - tech.shift_end)
        shift_minutes = max(1, tech.shift_end - tech.shift_start)
        utilization = service / shift_minutes
        occupied = route_travel + service + waiting
        workloads.append(service)
        total_travel += route_travel
        total_service += service
        total_overtime += overtime
        total_waiting += waiting
        tech_kpis.append(
            TechnicianKPI(
                technician_id=tech.id,
                service_minutes=service,
                travel_minutes=route_travel,
                overtime_minutes=overtime,
                utilization=round(utilization, 4),
                assignment_count=len(route),
                waiting_minutes=waiting,
                occupied_minutes=occupied,
                service_utilization=round(utilization, 4),
                occupied_utilization=round(occupied / shift_minutes, 4),
                travel_ratio=round(route_travel / occupied, 4) if occupied else 0,
                waiting_ratio=round(waiting / occupied, 4) if occupied else 0,
                overtime_ratio=round(overtime / shift_minutes, 4),
                normalized_workload=round(service / shift_minutes, 4),
            )
        )

    active_assignments = [
        assignment for assignment in assignments
        if orders.get(assignment.work_order_id) and orders[assignment.work_order_id].status.value != "completed"
    ]
    active_assigned_count = len(active_assignments)
    active_total = len([o for o in scenario.work_orders if o.status.value != "completed"])
    late_values = [item.sla_late_minutes for item in active_assignments]
    late_count = sum(1 for value in late_values if value > 0)
    on_time_count = active_assigned_count - late_count
    high_missed = sum(1 for u in unassigned if orders.get(u.work_order_id) and orders[u.work_order_id].priority.value in {"urgent", "high"})
    stability: float | None = None
    same_technician_rate: float | None = None
    adjacency_rate: float | None = None
    shift_median: float | None = None
    shift_p90: int | None = None
    shift_over_15: int | None = None
    notification_count: int | None = None
    if previous:
        previous_pending = [a for a in previous.assignments if orders.get(a.work_order_id) and orders[a.work_order_id].status.value == "pending"]
        current_by_id = {a.work_order_id: a for a in assignments}
        previous_ids = {item.work_order_id for item in previous_pending}
        old_predecessor: dict[str, str | None] = {}
        new_predecessor: dict[str, str | None] = {}
        old_routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
        new_routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
        for item in previous_pending:
            old_routes[item.technician_id].append(item)
        for item in assignments:
            if item.work_order_id in previous_ids:
                new_routes[item.technician_id].append(item)
        old_pairs: set[tuple[str, str, str]] = set()
        for tech_id, route in old_routes.items():
            predecessor: str | None = None
            ordered = sorted(route, key=lambda item: item.sequence)
            for item in ordered:
                old_predecessor[item.work_order_id] = predecessor
                if predecessor is not None:
                    old_pairs.add((tech_id, predecessor, item.work_order_id))
                predecessor = item.work_order_id
        new_pairs: set[tuple[str, str, str]] = set()
        for tech_id, route in new_routes.items():
            predecessor = None
            ordered = sorted(route, key=lambda item: item.sequence)
            for item in ordered:
                new_predecessor[item.work_order_id] = predecessor
                if predecessor is not None:
                    new_pairs.add((tech_id, predecessor, item.work_order_id))
                predecessor = item.work_order_id
        same_count = sum(1 for old in previous_pending if current_by_id.get(old.work_order_id) and current_by_id[old.work_order_id].technician_id == old.technician_id)
        kept = sum(1 for old in previous_pending if current_by_id.get(old.work_order_id) and current_by_id[old.work_order_id].technician_id == old.technician_id and new_predecessor.get(old.work_order_id) == old_predecessor.get(old.work_order_id))
        shifts = [abs(current_by_id[old.work_order_id].start_time - old.start_time) for old in previous_pending if old.work_order_id in current_by_id]
        stability = kept / len(previous_pending) if previous_pending else 1.0
        same_technician_rate = same_count / len(previous_pending) if previous_pending else 1.0
        adjacency_rate = len(old_pairs & new_pairs) / len(old_pairs) if old_pairs else 1.0
        shift_median = statistics.median(shifts) if shifts else 0
        shift_p90 = sorted(shifts)[max(0, math.ceil(len(shifts) * .9) - 1)] if shifts else 0
        shift_over_15 = sum(1 for value in shifts if value > 15)
        notification_count = sum(1 for old in previous_pending if old.work_order_id not in current_by_id or current_by_id[old.work_order_id].technician_id != old.technician_id or abs(current_by_id[old.work_order_id].start_time - old.start_time) > 15)

    assigned_rate = on_time_count / active_assigned_count if active_assigned_count else 0.0
    committed_rate = on_time_count / active_total if active_total else 1.0
    p90_late = sorted(late_values)[max(0, math.ceil(len(late_values) * .9) - 1)] if late_values else 0
    workload_range = max(workloads) - min(workloads) if workloads else 0
    normalized_values = [item.normalized_workload for item in tech_kpis]

    return ScheduleKPI(
        completion_rate=round(active_assigned_count / active_total, 4) if active_total else 1.0,
        sla_on_time_rate=round(assigned_rate, 4),
        sla_late_count=late_count,
        total_travel_minutes=total_travel,
        total_service_minutes=total_service,
        total_overtime_minutes=total_overtime,
        average_utilization=round(sum(k.utilization for k in tech_kpis) / len(tech_kpis), 4) if tech_kpis else 0,
        unassigned_count=len(unassigned),
        high_priority_missed=high_missed,
        workload_stddev=round(statistics.pstdev(workloads), 2) if workloads else 0,
        stability_rate=round(stability, 4) if stability is not None else None,
        technician=tech_kpis,
        assigned_on_time_rate=round(assigned_rate, 4),
        committed_on_time_rate=round(committed_rate, 4),
        total_late_minutes=sum(late_values),
        p90_late_minutes=p90_late,
        total_waiting_minutes=total_waiting,
        average_occupied_utilization=round(sum(item.occupied_utilization for item in tech_kpis) / len(tech_kpis), 4) if tech_kpis else 0,
        workload_range=workload_range,
        normalized_workload_range=round(max(normalized_values) - min(normalized_values), 4) if normalized_values else 0,
        same_technician_rate=round(same_technician_rate, 4) if same_technician_rate is not None else None,
        adjacency_preservation_rate=round(adjacency_rate, 4) if adjacency_rate is not None else None,
        start_time_shift_median=round(shift_median, 2) if shift_median is not None else None,
        start_time_shift_p90=shift_p90,
        start_time_shift_over_15m_count=shift_over_15,
        customer_notification_count=notification_count,
    )


def objective_breakdown(
    scenario: ScheduleScenario,
    kpis: ScheduleKPI,
    unassigned: list[UnassignedWorkOrder],
    assignments: list[ScheduleAssignment],
    changes: int = 0,
) -> dict[str, float]:
    config = scenario.solver_config
    order_map = {o.id: o for o in scenario.work_orders}
    drop_cost = sum(order_map[u.work_order_id].drop_penalty for u in unassigned)
    late_minutes = sum(item.sla_late_minutes for item in assignments)
    return {
        "travel": round(kpis.total_travel_minutes * config.travel_weight, 2),
        "sla_late": round(late_minutes * config.sla_late_weight, 2),
        "overtime": round(kpis.total_overtime_minutes * config.overtime_weight, 2),
        "unassigned": round(drop_cost, 2),
        "imbalance": round(kpis.normalized_workload_range * 1000 * config.imbalance_weight, 2),
        "replan_changes": round(changes * config.replan_change_weight, 2),
    }


def _result(
    scenario: ScheduleScenario,
    kind: str,
    version: int,
    status: SolverStatus,
    runtime_ms: int,
    assignments: list[ScheduleAssignment],
    unassigned: list[UnassignedWorkOrder],
    previous: ScheduleResult | None = None,
    note: str = "",
    strategy: str = "balanced",
    requested_time_limit_ms: int | None = None,
    effective_time_limit_ms: int | None = None,
    solver_status_code: int | None = None,
    termination_reason: str | None = None,
    solution_found: bool = True,
    solver_objective_value: float | None = None,
    solver_name: str = "fieldflow-greedy",
    solver_version: str = "1",
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    kpis = calculate_kpis(scenario, assignments, unassigned, previous if kind == "replan" else None, provider)
    # Stability is a replanning concern. A normal optimization may be compared
    # with a baseline, but must not pay a penalty merely for improving it.
    changes = sum(1 for a in assignments if a.changed) if kind == "replan" else 0
    breakdown = objective_breakdown(scenario, kpis, unassigned, assignments, changes)
    business_score = round(sum(breakdown.values()), 2)
    return ScheduleResult(
        id=f"SCH-{scenario.id}-{version}-{uuid.uuid4().hex[:6]}",
        scenario_id=scenario.id,
        kind=kind,  # type: ignore[arg-type]
        version=version,
        created_at=_now(),
        solver_status=status,
        runtime_ms=runtime_ms,
        objective=business_score,
        assignments=sorted(assignments, key=lambda a: (a.technician_id, a.sequence)),
        unassigned=unassigned,
        kpis=kpis,
        source_schedule_id=previous.id if previous else None,
        solver_note=note,
        scenario_revision=scenario.revision,
        strategy=strategy,  # type: ignore[arg-type]
        objective_breakdown=breakdown,
        requested_time_limit_ms=requested_time_limit_ms,
        effective_time_limit_ms=effective_time_limit_ms,
        solver_status_code=solver_status_code,
        termination_reason=termination_reason,
        solution_found=solution_found,
        solver_objective_value=solver_objective_value,
        business_score=business_score,
        business_score_policy_version=BUSINESS_SCORE_POLICY_VERSION,
        scenario_snapshot_hash=content_hash(scenario),
        solver_config_hash=content_hash(scenario.solver_config),
        travel_model_version=provider.version,
        travel_model_fingerprint=provider.fingerprint,
        metric_policy_version=METRIC_POLICY_VERSION,
        solver_name=solver_name,
        solver_version=solver_version,
    )


def recompute_business_result(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    previous: ScheduleResult | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    """Bind solver output to the immutable business snapshot and scoring policy."""
    rebound = result.model_copy(deep=True)
    rebound.scenario_id = scenario.id
    rebound.scenario_revision = scenario.revision
    rebound.scenario_snapshot_hash = content_hash(scenario)
    rebound.kpis = calculate_kpis(
        scenario,
        rebound.assignments,
        rebound.unassigned,
        previous if rebound.kind == "replan" else None,
        provider,
    )
    changes = sum(1 for item in rebound.assignments if item.changed) if rebound.kind == "replan" else 0
    rebound.objective_breakdown = objective_breakdown(
        scenario,
        rebound.kpis,
        rebound.unassigned,
        rebound.assignments,
        changes,
    )
    rebound.business_score = round(sum(rebound.objective_breakdown.values()), 2)
    rebound.objective = rebound.business_score
    rebound.business_score_policy_version = BUSINESS_SCORE_POLICY_VERSION
    rebound.metric_policy_version = METRIC_POLICY_VERSION
    rebound.travel_model_version = provider.version
    rebound.travel_model_fingerprint = provider.fingerprint
    return rebound


def scenario_for_strategy(scenario: ScheduleScenario, strategy: str) -> ScheduleScenario:
    """Return a copy with a documented business optimization profile applied."""
    effective = scenario.model_copy(deep=True)
    profiles = {
        "balanced": (4, 12, 30, 1, 80, 1.0),
        "completion": (1, 1, 1, 0, 60, 5.0),
        "punctuality": (2, 200, 30, 2, 100, 1.0),
        "low_travel": (30, 8, 8, 1, 80, 0.8),
        "low_overtime": (2, 5, 500, 1, 90, 0.5),
        "fair_workload": (3, 10, 8, 10, 90, 1.3),
        "stable": (4, 16, 10, 2, 260, 1.0),
    }
    travel, sla, overtime, imbalance, changes, drop_scale = profiles.get(strategy, profiles["balanced"])
    effective.solver_config.travel_weight = travel
    effective.solver_config.sla_late_weight = sla
    effective.solver_config.overtime_weight = overtime
    effective.solver_config.imbalance_weight = imbalance
    effective.solver_config.replan_change_weight = changes
    for order in effective.work_orders:
        order.drop_penalty = max(1, round(order.drop_penalty * drop_scale))
    return effective


def scenario_for_profile(scenario: ScheduleScenario, profile: StrategyProfile) -> ScheduleScenario:
    """Apply a saved profile without changing the live business scenario."""
    effective = scenario.model_copy(deep=True)
    weights = profile.weights
    effective.solver_config.travel_weight = weights.travel_weight
    effective.solver_config.sla_late_weight = weights.sla_late_weight
    effective.solver_config.overtime_weight = weights.overtime_weight
    effective.solver_config.imbalance_weight = weights.imbalance_weight
    effective.solver_config.replan_change_weight = weights.replan_change_weight
    effective.solver_config.time_limit_seconds = profile.time_limit_seconds
    for order in effective.work_orders:
        order.drop_penalty = max(1, round(order.drop_penalty * weights.unassigned_penalty_scale))
    return effective


def baseline_schedule(
    scenario: ScheduleScenario,
    version: int,
    strategy: str = "baseline",
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    started_at = time.perf_counter()
    locked = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    state = {t.id: (t.shift_start, t.start_location) for t in scenario.technicians}
    assignments: list[ScheduleAssignment] = []
    unassigned: list[UnassignedWorkOrder] = []

    orders = sorted(
        [o for o in scenario.work_orders if o.status.value != "completed"],
        key=lambda o: (o.sla_deadline, {"urgent": 0, "high": 1, "normal": 2, "low": 3}[o.priority.value], o.id),
    )
    for order in orders:
        candidates = [t for t in scenario.technicians if _eligible(t, order)]
        if order.id in locked:
            candidates = [t for t in candidates if t.id == locked[order.id]]
        choices: list[tuple[int, int, int, Technician]] = []
        for tech in candidates:
            available, point = state[tech.id]
            travel = provider.minutes(point, order.location)
            arrival = available + travel
            start = max(arrival, service_ready_at(order))
            finish = start + order.service_duration
            return_time = provider.minutes(order.location, tech.start_location)
            if start <= order.window_end and finish + return_time <= tech.shift_end + tech.overtime_limit:
                choices.append((start, travel, finish, tech))
        if not choices:
            unassigned.append(_diagnose_unassigned(order, scenario, locked, provider=provider))
            continue
        start, travel, finish, tech = min(choices, key=lambda choice: (choice[0], choice[1], choice[3].id))
        arrival = state[tech.id][0] + travel
        assignment = ScheduleAssignment(
            work_order_id=order.id,
            technician_id=tech.id,
            sequence=len(routes[tech.id]) + 1,
            arrival_time=arrival,
            start_time=start,
            finish_time=finish,
            travel_minutes=travel,
            sla_late_minutes=max(0, finish - order.sla_deadline),
            explanation=_explanation(order, tech, start, travel, finish, scenario, order.id in locked, False),
            evidence={
                "required_skills": [item.value for item in order.required_skills],
                "eligible_technician_ids": [item.id for item in scenario.technicians if _eligible(item, order)],
                "window_start": order.window_start,
                "window_end": order.window_end,
                "reported_at": order.reported_at,
                "service_ready_at": service_ready_at(order),
                "arrival_time": arrival,
                "start_time": start,
                "finish_time": finish,
                "leg_travel_minutes": travel,
                "overtime_minutes": max(0, finish - tech.shift_end),
            },
            locked=order.id in locked,
        )
        routes[tech.id].append(assignment)
        assignments.append(assignment)
        state[tech.id] = (finish, order.location)

    _attach_route_insertion_evidence(scenario, assignments, provider)
    runtime_ms = max(1, round((time.perf_counter() - started_at) * 1000))
    return _result(
        scenario, "baseline", version, SolverStatus.feasible, runtime_ms, assignments, unassigned,
        note="确定性贪心基线：按 SLA 截止时间排序，并选择最早可到达的合格技师。",
        strategy=strategy,
        provider=provider,
    )


def optimized_schedule(
    scenario: ScheduleScenario,
    version: int,
    previous: ScheduleResult | None = None,
    kind: str = "optimized",
    current_time: int | None = None,
    time_limit_seconds: float | None = None,
    strategy: str = "balanced",
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    started_at = time.perf_counter()
    technicians = scenario.technicians
    active_orders = [o for o in scenario.work_orders if o.status.value != "completed"]
    order_by_id = {o.id: o for o in active_orders}
    locked = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    previous_by_id = {a.work_order_id: a for a in previous.assignments} if previous else {}
    preserve_previous = kind == "replan" or current_time is not None
    previous_active_predecessor: dict[str, str | None] = {}
    if preserve_previous and previous:
        previous_routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
        for item in previous.assignments:
            if item.work_order_id in order_by_id:
                previous_routes[item.technician_id].append(item)
        for route in previous_routes.values():
            predecessor: str | None = None
            for item in sorted(route, key=lambda assignment: assignment.sequence):
                previous_active_predecessor[item.work_order_id] = predecessor
                predecessor = item.work_order_id

    # Started work is immutable during replanning.
    if previous:
        for order in active_orders:
            old = previous_by_id.get(order.id)
            if order.status.value == "started" and old:
                locked[order.id] = old.technician_id

    locations = [t.start_location for t in technicians] + [o.location for o in active_orders]
    node_orders: dict[int, WorkOrder] = {len(technicians) + i: order for i, order in enumerate(active_orders)}
    order_nodes = {order.id: len(technicians) + i for i, order in enumerate(active_orders)}
    starts = list(range(len(technicians)))
    ends = list(range(len(technicians)))
    manager = pywrapcp.RoutingIndexManager(len(locations), len(technicians), starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    time_callbacks: list[int] = []
    cost_callbacks: list[int] = []
    for vehicle, _tech in enumerate(technicians):
        def time_cb(from_index: int, to_index: int, vehicle: int = vehicle) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            service = node_orders[from_node].service_duration if from_node in node_orders else 0
            return service + provider.minutes(locations[from_node], locations[to_node])

        def cost_cb(from_index: int, to_index: int, vehicle: int = vehicle) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            cost = provider.minutes(locations[from_node], locations[to_node]) * scenario.solver_config.travel_weight
            order = node_orders.get(to_node)
            old = previous_by_id.get(order.id) if order else None
            if preserve_previous and old and old.technician_id != technicians[vehicle].id:
                cost += scenario.solver_config.replan_change_weight
            elif preserve_previous and old and order:
                from_order = node_orders.get(from_node)
                current_predecessor = from_order.id if from_order else None
                if previous_active_predecessor.get(order.id) != current_predecessor:
                    cost += scenario.solver_config.replan_change_weight
            return int(cost)

        time_callbacks.append(routing.RegisterTransitCallback(time_cb))
        cost_callbacks.append(routing.RegisterTransitCallback(cost_cb))
        routing.SetArcCostEvaluatorOfVehicle(cost_callbacks[-1], vehicle)

    # Waiting is a legitimate part of field-service routing. Cover the full
    # planning horizon so wide customer windows do not become false conflicts.
    routing.AddDimensionWithVehicleTransits(time_callbacks, 1800, 2040, False, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")
    service_callbacks: list[int] = []
    for technician in technicians:
        shift_minutes = max(1, technician.shift_end - technician.shift_start)
        service_callbacks.append(
            routing.RegisterTransitCallback(
                lambda from_index, to_index, shift_minutes=shift_minutes: round(
                    (
                        node_orders[manager.IndexToNode(from_index)].service_duration
                        if manager.IndexToNode(from_index) in node_orders
                        else 0
                    )
                    * 10_000
                    / shift_minutes
                )
            )
        )
    routing.AddDimensionWithVehicleTransits(service_callbacks, 0, 100_000, True, "ServiceLoad")
    service_dimension = routing.GetDimensionOrDie("ServiceLoad")
    service_dimension.SetGlobalSpanCostCoefficient(max(0, scenario.solver_config.imbalance_weight))

    vehicle_available: list[bool] = []
    for vehicle, tech in enumerate(technicians):
        start_index = routing.Start(vehicle)
        end_index = routing.End(vehicle)
        start_min = max(tech.shift_start, current_time or tech.shift_start) if previous else tech.shift_start
        latest_return = tech.shift_end + tech.overtime_limit
        available = start_min <= latest_return
        vehicle_available.append(available)
        safe_start = start_min if available else latest_return
        time_dimension.CumulVar(start_index).SetRange(safe_start, safe_start)
        time_dimension.CumulVar(end_index).SetRange(safe_start, latest_return)
        time_dimension.SetCumulVarSoftUpperBound(end_index, tech.shift_end, scenario.solver_config.overtime_weight)

    precheck_unassigned: set[str] = set()
    for order in active_orders:
        node = order_nodes[order.id]
        index = manager.NodeToIndex(node)
        ready_at = service_ready_at(order)
        time_dimension.CumulVar(index).SetRange(ready_at, order.window_end)
        eligible_vehicles = [i for i, tech in enumerate(technicians) if vehicle_available[i] and _eligible(tech, order)]
        locked_to = locked.get(order.id)
        if locked_to:
            eligible_vehicles = [i for i in eligible_vehicles if technicians[i].id == locked_to]
        if eligible_vehicles:
            routing.SetAllowedVehiclesForIndex(eligible_vehicles, index)
        else:
            precheck_unassigned.add(order.id)
            routing.ActiveVar(index).SetValue(0)
        penalty = int(order.drop_penalty)
        if order.status.value == "started":
            penalty *= 100
        # A valid manual lock is a hard commitment: restricting the vehicle is
        # insufficient because a disjunction would still let the solver drop it.
        if not locked_to:
            routing.AddDisjunction([index], penalty)
        latest_on_time_start = max(ready_at, order.sla_deadline - order.service_duration)
        time_dimension.SetCumulVarSoftUpperBound(index, latest_on_time_start, scenario.solver_config.sla_late_weight)

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    # A fixed solution count makes the normal path reproducible; wall time remains
    # a safety ceiling for unexpectedly hard/custom scenarios.
    # Balancing service load is a global property and needs a deeper local
    # search than route-focused profiles to become visible on larger fixtures.
    # Use the same deterministic search budget for every profile. Giving the
    # route-focused profiles fewer accepted solutions made their displayed KPI
    # worse than the balanced profile even when travel carried the larger cost.
    search.solution_limit = 120
    limit = time_limit_seconds if time_limit_seconds is not None else scenario.solver_config.time_limit_seconds
    if limit < 1:
        raise ValueError("time_limit_seconds must be at least 1 second")
    requested_limit_ms = int(round(limit * 1000))
    effective_limit_ms = requested_limit_ms
    search.time_limit.seconds, remainder_ms = divmod(effective_limit_ms, 1000)
    search.time_limit.nanos = remainder_ms * 1_000_000
    search.log_search = False
    solution = routing.SolveWithParameters(search)
    runtime_ms = max(1, round((time.perf_counter() - started_at) * 1000))
    routing_status_code = int(routing.status())
    mapped_status = solver_status_from_routing(routing_status_code, solution is not None)
    termination_reason = ROUTING_STATUS_NAMES.get(routing_status_code, f"ROUTING_STATUS_{routing_status_code}")

    if not solution:
        unassigned = [_diagnose_unassigned(order, scenario, locked, False, provider) for order in active_orders]
        return _result(
            scenario, kind, version, mapped_status, runtime_ms, [], unassigned, previous,
            note=f"没有生成可执行候选（{termination_reason}），当前正式方案保持不变。",
            strategy=strategy,
            requested_time_limit_ms=requested_limit_ms,
            effective_time_limit_ms=effective_limit_ms,
            solver_status_code=routing_status_code,
            termination_reason=termination_reason,
            solution_found=False,
            solver_name="ortools-routing",
            solver_version=ortools.__version__,
            provider=provider,
        )

    assignments: list[ScheduleAssignment] = []
    assigned_ids: set[str] = set()
    for vehicle, tech in enumerate(technicians):
        index = routing.Start(vehicle)
        sequence = 0
        previous_point = tech.start_location
        available = solution.Value(time_dimension.CumulVar(index))
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node in node_orders:
                order = node_orders[node]
                sequence += 1
                start = solution.Value(time_dimension.CumulVar(index))
                travel = provider.minutes(previous_point, order.location)
                arrival = available + travel
                finish = start + order.service_duration
                old = previous_by_id.get(order.id)
                changed = False
                assignment = ScheduleAssignment(
                    work_order_id=order.id,
                    technician_id=tech.id,
                    sequence=sequence,
                    arrival_time=arrival,
                    start_time=start,
                    finish_time=finish,
                    travel_minutes=travel,
                    sla_late_minutes=max(0, finish - order.sla_deadline),
                    explanation=_explanation(order, tech, start, travel, finish, scenario, order.id in locked, changed),
                    evidence={
                        "required_skills": [item.value for item in order.required_skills],
                        "eligible_technician_ids": [item.id for item in scenario.technicians if _eligible(item, order)],
                        "window_start": order.window_start,
                        "window_end": order.window_end,
                        "reported_at": order.reported_at,
                        "service_ready_at": service_ready_at(order),
                        "arrival_time": arrival,
                        "start_time": start,
                        "finish_time": finish,
                        "leg_travel_minutes": travel,
                        "overtime_minutes": max(0, finish - tech.shift_end),
                    },
                    locked=order.id in locked,
                    changed=changed,
                )
                assignments.append(assignment)
                assigned_ids.add(order.id)
                previous_point = order.location
                available = finish
            index = solution.Value(routing.NextVar(index))

    if preserve_previous and previous:
        _mark_replan_changes(assignments, previous, set(order_by_id))

    unassigned = [
        _diagnose_unassigned(order, scenario, locked, dropped_by_solver=order.id not in precheck_unassigned, provider=provider)
        for order in active_orders
        if order.id not in assigned_ids
    ]
    _attach_route_insertion_evidence(scenario, assignments, provider)
    strategy_names = {"balanced": "均衡", "completion": "完成率优先", "punctuality": "准时优先", "low_travel": "低行程", "low_overtime": "低加班", "fair_workload": "工作量公平", "stable": "稳定优先", "custom": "自定义"}
    optimality_note = (
        "求解器已证明当前候选为全局最优解。"
        if mapped_status is SolverStatus.optimal
        else "当前候选可执行，但尚未完成全局最优性证明。"
    )
    return _result(
        scenario, kind, version, mapped_status, runtime_ms, assignments, unassigned, previous,
        note=(
            f"“{strategy_names.get(strategy, strategy)}”策略在本次搜索预算内找到了可执行方案；"
            f"终止原因：{termination_reason}。{optimality_note}"
        ),
        strategy=strategy,
        requested_time_limit_ms=requested_limit_ms,
        effective_time_limit_ms=effective_limit_ms,
        solver_status_code=routing_status_code,
        termination_reason=termination_reason,
        solution_found=True,
        solver_objective_value=float(solution.ObjectiveValue()),
        solver_name="ortools-routing",
        solver_version=ortools.__version__,
        provider=provider,
    )


def replan_schedule(
    scenario: ScheduleScenario,
    version: int,
    previous: ScheduleResult,
    current_time: int,
    time_limit_seconds: float | None = None,
    strategy: str = "stable",
    planning_context: PlanningContext | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    """Replan pending work while preserving assignments frozen by explicit execution facts."""
    scenario_copy = scenario.model_copy(deep=True)
    orders = {o.id: o for o in scenario_copy.work_orders}
    if planning_context and planning_context.scenario_revision != scenario.revision:
        raise ValueError("planning context does not match scenario revision")
    frozen_ids = (
        {item.work_order_id for item in planning_context.frozen_assignments}
        if planning_context
        else {order.id for order in scenario.work_orders if order.status.value in {"started", "completed"}}
    )
    fixed = [
        a.model_copy(deep=True)
        for a in previous.assignments
        if orders.get(a.work_order_id) and a.work_order_id in frozen_ids
    ]
    fixed_ids = {a.work_order_id for a in fixed}
    scenario_copy.work_orders = [o for o in scenario_copy.work_orders if o.id not in fixed_ids]
    scenario_copy.locked_assignments = [lock for lock in scenario_copy.locked_assignments if lock.work_order_id not in fixed_ids]

    original_orders = {o.id: o for o in scenario.work_orders}
    fixed_by_tech: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for item in fixed:
        fixed_by_tech[item.technician_id].append(item)
        item.changed = False
        if "本次不调整" not in "".join(item.explanation):
            item.explanation.append("工单已有明确执行状态，本次不调整")
    for tech in scenario_copy.technicians:
        prefix = sorted(fixed_by_tech.get(tech.id, []), key=lambda a: a.sequence)
        if prefix:
            last = prefix[-1]
            tech.start_location = original_orders[last.work_order_id].location
            tech.shift_start = max(current_time, last.finish_time)
        else:
            tech.shift_start = max(current_time, tech.shift_start)

    partial = optimized_schedule(
        scenario_copy,
        version,
        previous=previous,
        kind="replan",
        current_time=current_time,
        time_limit_seconds=time_limit_seconds,
        strategy=strategy,
        provider=provider,
    )
    offsets = {tech.id: len(fixed_by_tech.get(tech.id, [])) for tech in scenario.technicians}
    replanned: list[ScheduleAssignment] = []
    for item in partial.assignments:
        item.sequence += offsets[item.technician_id]
        change_note = "为接纳突发工单或降低 SLA 风险，本项安排发生调整"
        item.explanation = [line for line in item.explanation if line != change_note]
        replanned.append(item)
    merged = fixed + replanned
    pending_ids = {order.id for order in scenario.work_orders if order.status.value == "pending"}
    _mark_replan_changes(merged, previous, pending_ids)
    for item in replanned:
        if item.changed:
            item.explanation.append("为接纳突发工单或降低 SLA 风险，本项安排发生调整")
    warning_count = len(planning_context.inferred_departure_warnings) if planning_context else 0
    warning_note = f"；另有 {warning_count} 个工单仅按计划时间可能已出发，未据此自动冻结" if warning_count else ""
    final = _result(
        scenario,
        "replan",
        version,
        partial.solver_status,
        partial.runtime_ms,
        merged,
        partial.unassigned,
        previous,
        note=f"局部重排保留了 {len(fixed)} 个具有明确执行状态的工单{warning_note}；{partial.solver_note}",
        strategy=strategy,
        requested_time_limit_ms=partial.requested_time_limit_ms,
        effective_time_limit_ms=partial.effective_time_limit_ms,
        solver_status_code=partial.solver_status_code,
        termination_reason=partial.termination_reason,
        solution_found=partial.solution_found,
        solver_objective_value=partial.solver_objective_value,
        solver_name=partial.solver_name,
        solver_version=partial.solver_version,
        provider=provider,
    )
    return final


def validate_schedule(scenario: ScheduleScenario, result: ScheduleResult) -> list[str]:
    """Compatibility wrapper around the independent publication verifier."""
    from .verification import verify_schedule

    return [issue.message for issue in verify_schedule(scenario, result).errors]
