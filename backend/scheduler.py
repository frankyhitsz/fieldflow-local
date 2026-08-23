from __future__ import annotations

import math
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .models import (
    ScheduleAssignment,
    ScheduleKPI,
    ScheduleResult,
    ScheduleScenario,
    SolverStatus,
    Technician,
    TechnicianKPI,
    UnassignedReason,
    UnassignedWorkOrder,
    WorkOrder,
    StrategyProfile,
)
from .timeutils import hhmm, travel_minutes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _eligible(tech: Technician, order: WorkOrder) -> bool:
    return set(order.required_skills).issubset(set(tech.skills))


def _diagnose_unassigned(
    order: WorkOrder,
    scenario: ScheduleScenario,
    locked: dict[str, str],
    dropped_by_solver: bool = False,
) -> UnassignedWorkOrder:
    eligible = [t for t in scenario.technicians if _eligible(t, order)]
    if not eligible:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.no_eligible_technician,
            detail=f"没有技师同时具备 {', '.join(s.value for s in order.required_skills)} 技能",
            suggestions=["调整技能要求", "安排具备复合技能的外援"],
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
            )
        eligible = [tech]
    individually_feasible = []
    for tech in eligible:
        arrive = tech.shift_start + travel_minutes(tech.start_location, order.location)
        start = max(arrive, order.window_start)
        finish = start + order.service_duration
        if start <= order.window_end:
            individually_feasible.append((tech, finish))
    if not individually_feasible:
        if locked_to:
            return UnassignedWorkOrder(
                work_order_id=order.id,
                reason=UnassignedReason.locked_plan_conflict,
                detail=f"锁定技师 {locked_to} 无法在客户时间窗内到达",
                suggestions=["解除锁定", "放宽客户时间窗", "调整指定技师班次"],
            )
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.time_window_infeasible,
            detail=f"最早可到达时间晚于客户时间窗 {hhmm(order.window_start)}–{hhmm(order.window_end)}",
            suggestions=["与客户协商放宽时间窗", "调整技师班次开始时间"],
        )
    if all(finish > tech.shift_end + tech.overtime_limit for tech, finish in individually_feasible):
        if locked_to:
            return UnassignedWorkOrder(
                work_order_id=order.id,
                reason=UnassignedReason.locked_plan_conflict,
                detail=f"锁定技师 {locked_to} 执行后会超过允许加班上限",
                suggestions=["解除锁定", "增加指定技师班次容量"],
            )
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.shift_capacity_exceeded,
            detail="可选技师即使单独执行也会超过允许加班上限",
            suggestions=["增加班次容量", "拆分或缩短服务任务"],
        )
    if dropped_by_solver:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.dropped_by_objective,
            detail="存在可行技师，但纳入当前路线会导致更高的综合业务代价",
            suggestions=["提高工单优先级", "放宽时间窗", "增加可用技师"],
        )
    if locked_to:
        return UnassignedWorkOrder(
            work_order_id=order.id,
            reason=UnassignedReason.locked_plan_conflict,
            detail=f"锁定技师 {locked_to} 的现有路线无法同时满足该承诺",
            suggestions=["解除锁定", "调整同一技师的其他工单", "放宽客户时间窗"],
        )
    return UnassignedWorkOrder(
        work_order_id=order.id,
        reason=UnassignedReason.shift_capacity_exceeded,
        detail="当前路线剩余容量无法容纳此工单",
        suggestions=["允许更长加班", "将低优先级工单移至下一服务日"],
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
    alternatives = [t for t in scenario.technicians if t.id != tech.id and _eligible(t, order)]
    if alternatives:
        best_alt = min(travel_minutes(t.start_location, order.location) for t in alternatives)
        delta = best_alt - travel_minutes(tech.start_location, order.location)
        if delta > 0:
            items.append(f"从班次起点估算，相比其他候选技师至少减少 {delta} 分钟行程")
        else:
            items.append(f"在 {len(alternatives) + 1} 名合格技师中平衡了时间窗、路程与工作量")
    items.append("不会产生加班" if finish <= tech.shift_end else f"产生 {finish - tech.shift_end} 分钟加班，未超过上限")
    if locked:
        items.append("人工锁定已生效，本次求解保持指定技师")
    if changed:
        items.append("为接纳突发工单或降低 SLA 风险，本项安排发生调整")
    if order.vip:
        items.append("VIP / 高优先级保护：未分配惩罚已显著提高")
    return items


def _calculate_kpis(
    scenario: ScheduleScenario,
    assignments: list[ScheduleAssignment],
    unassigned: list[UnassignedWorkOrder],
    previous: ScheduleResult | None = None,
) -> ScheduleKPI:
    orders = {o.id: o for o in scenario.work_orders}
    techs = {t.id: t for t in scenario.technicians}
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in assignments:
        grouped[assignment.technician_id].append(assignment)

    tech_kpis: list[TechnicianKPI] = []
    workloads: list[int] = []
    total_travel = 0
    total_service = 0
    total_overtime = 0
    for tech in scenario.technicians:
        route = sorted(grouped.get(tech.id, []), key=lambda a: a.sequence)
        service = sum(orders[a.work_order_id].service_duration for a in route)
        route_travel = sum(a.travel_minutes for a in route)
        if route:
            route_travel += travel_minutes(orders[route[-1].work_order_id].location, tech.start_location)
            route_end = route[-1].finish_time + travel_minutes(orders[route[-1].work_order_id].location, tech.start_location)
        else:
            route_end = tech.shift_start
        overtime = max(0, route_end - tech.shift_end)
        shift_minutes = max(1, tech.shift_end - tech.shift_start)
        utilization = service / shift_minutes
        workloads.append(service)
        total_travel += route_travel
        total_service += service
        total_overtime += overtime
        tech_kpis.append(
            TechnicianKPI(
                technician_id=tech.id,
                service_minutes=service,
                travel_minutes=route_travel,
                overtime_minutes=overtime,
                utilization=round(utilization, 4),
                assignment_count=len(route),
            )
        )

    assigned_count = len(assignments)
    active_assigned_count = sum(
        1
        for assignment in assignments
        if orders.get(assignment.work_order_id)
        and orders[assignment.work_order_id].status.value != "completed"
    )
    active_total = len([o for o in scenario.work_orders if o.status.value != "completed"])
    late_count = sum(1 for a in assignments if a.sla_late_minutes > 0)
    high_missed = sum(1 for u in unassigned if orders[u.work_order_id].priority.value in {"urgent", "high"})
    stability: float | None = None
    if previous:
        previous_pending = [a for a in previous.assignments if orders.get(a.work_order_id) and orders[a.work_order_id].status.value == "pending"]
        current_by_id = {a.work_order_id: a for a in assignments}
        kept = 0
        for old in previous_pending:
            new = current_by_id.get(old.work_order_id)
            if new and new.technician_id == old.technician_id and new.sequence == old.sequence:
                kept += 1
        stability = kept / len(previous_pending) if previous_pending else 1.0

    return ScheduleKPI(
        completion_rate=round(active_assigned_count / active_total, 4) if active_total else 1.0,
        sla_on_time_rate=round((assigned_count - late_count) / assigned_count, 4) if assigned_count else 0.0,
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
    )


def _objective_breakdown(
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
        "imbalance": round(kpis.workload_stddev * config.imbalance_weight, 2),
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
) -> ScheduleResult:
    kpis = _calculate_kpis(scenario, assignments, unassigned, previous)
    # Stability is a replanning concern. A normal optimization may be compared
    # with a baseline, but must not pay a penalty merely for improving it.
    changes = sum(1 for a in assignments if a.changed) if kind == "replan" else 0
    breakdown = _objective_breakdown(scenario, kpis, unassigned, assignments, changes)
    return ScheduleResult(
        id=f"SCH-{scenario.id}-{version}-{uuid.uuid4().hex[:6]}",
        scenario_id=scenario.id,
        kind=kind,  # type: ignore[arg-type]
        version=version,
        created_at=_now(),
        solver_status=status,
        runtime_ms=runtime_ms,
        objective=round(sum(breakdown.values()), 2),
        assignments=sorted(assignments, key=lambda a: (a.technician_id, a.sequence)),
        unassigned=unassigned,
        kpis=kpis,
        source_schedule_id=previous.id if previous else None,
        solver_note=note,
        scenario_revision=scenario.revision,
        strategy=strategy,  # type: ignore[arg-type]
        objective_breakdown=breakdown,
    )


def scenario_for_strategy(scenario: ScheduleScenario, strategy: str) -> ScheduleScenario:
    """Return a copy with a documented business optimization profile applied."""
    effective = scenario.model_copy(deep=True)
    profiles = {
        "balanced": (4, 12, 30, 1, 80, 1.0),
        "completion": (1, 1, 1, 1, 60, 5.0),
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


def baseline_schedule(scenario: ScheduleScenario, version: int, strategy: str = "baseline") -> ScheduleResult:
    started_at = time.perf_counter()
    techs = {t.id: t for t in scenario.technicians}
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
            travel = travel_minutes(point, order.location)
            arrival = available + travel
            start = max(arrival, order.window_start)
            finish = start + order.service_duration
            return_time = travel_minutes(order.location, tech.start_location)
            if start <= order.window_end and finish + return_time <= tech.shift_end + tech.overtime_limit:
                choices.append((start, travel, finish, tech))
        if not choices:
            unassigned.append(_diagnose_unassigned(order, scenario, locked))
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
            locked=order.id in locked,
        )
        routes[tech.id].append(assignment)
        assignments.append(assignment)
        state[tech.id] = (finish, order.location)

    runtime_ms = max(1, round((time.perf_counter() - started_at) * 1000))
    return _result(
        scenario, "baseline", version, SolverStatus.feasible, runtime_ms, assignments, unassigned,
        note="确定性贪心基线：按 SLA 截止时间排序，并选择最早可到达的合格技师。",
        strategy=strategy,
    )


def optimized_schedule(
    scenario: ScheduleScenario,
    version: int,
    previous: ScheduleResult | None = None,
    kind: str = "optimized",
    current_time: int | None = None,
    time_limit_seconds: float | None = None,
    strategy: str = "balanced",
) -> ScheduleResult:
    started_at = time.perf_counter()
    technicians = scenario.technicians
    active_orders = [o for o in scenario.work_orders if o.status.value != "completed"]
    tech_index = {t.id: i for i, t in enumerate(technicians)}
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
    for vehicle, tech in enumerate(technicians):
        def time_cb(from_index: int, to_index: int, vehicle: int = vehicle) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            service = node_orders[from_node].service_duration if from_node in node_orders else 0
            return service + travel_minutes(locations[from_node], locations[to_node])

        def cost_cb(from_index: int, to_index: int, vehicle: int = vehicle) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            cost = travel_minutes(locations[from_node], locations[to_node]) * scenario.solver_config.travel_weight
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
    time_dimension.SetGlobalSpanCostCoefficient(max(0, scenario.solver_config.imbalance_weight))

    for vehicle, tech in enumerate(technicians):
        start_index = routing.Start(vehicle)
        end_index = routing.End(vehicle)
        start_min = max(tech.shift_start, current_time or tech.shift_start) if previous else tech.shift_start
        time_dimension.CumulVar(start_index).SetRange(start_min, start_min)
        time_dimension.CumulVar(end_index).SetRange(start_min, tech.shift_end + tech.overtime_limit)
        time_dimension.SetCumulVarSoftUpperBound(end_index, tech.shift_end, scenario.solver_config.overtime_weight)

    precheck_unassigned: set[str] = set()
    for order in active_orders:
        node = order_nodes[order.id]
        index = manager.NodeToIndex(node)
        time_dimension.CumulVar(index).SetRange(order.window_start, order.window_end)
        eligible_vehicles = [i for i, tech in enumerate(technicians) if _eligible(tech, order)]
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
        latest_on_time_start = max(order.window_start, order.sla_deadline - order.service_duration)
        time_dimension.SetCumulVarSoftUpperBound(index, latest_on_time_start, scenario.solver_config.sla_late_weight)

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    # A fixed solution count makes the normal path reproducible; wall time remains
    # a safety ceiling for unexpectedly hard/custom scenarios.
    # Balancing service load is a global property and needs a deeper local
    # search than route-focused profiles to become visible on larger fixtures.
    search.solution_limit = 120 if strategy in {"balanced", "completion", "punctuality", "low_overtime", "fair_workload"} else 20
    limit = time_limit_seconds or scenario.solver_config.time_limit_seconds
    # Routing's sub-second Duration can behave as an unbounded search on some builds.
    # Round up to one whole second so every user-supplied limit remains bounded.
    effective_limit = max(1, int(math.ceil(limit)))
    search.time_limit.seconds = effective_limit
    search.time_limit.nanos = 0
    search.log_search = False
    solution = routing.SolveWithParameters(search)
    runtime_ms = max(1, round((time.perf_counter() - started_at) * 1000))

    if not solution:
        status = SolverStatus.time_limit if runtime_ms >= effective_limit * 950 else SolverStatus.infeasible
        unassigned = [_diagnose_unassigned(order, scenario, locked, False) for order in active_orders]
        return _result(
            scenario, kind, version, status, runtime_ms, [], unassigned, previous,
            note="求解器未在限制内找到可执行方案；原计划未被覆盖。",
            strategy=strategy,
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
                travel = travel_minutes(previous_point, order.location)
                arrival = available + travel
                finish = start + order.service_duration
                old = previous_by_id.get(order.id)
                changed = bool(old and (old.technician_id != tech.id or old.sequence != sequence))
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
                    locked=order.id in locked,
                    changed=changed,
                )
                assignments.append(assignment)
                assigned_ids.add(order.id)
                previous_point = order.location
                available = finish
            index = solution.Value(routing.NextVar(index))

    unassigned = [
        _diagnose_unassigned(order, scenario, locked, dropped_by_solver=order.id not in precheck_unassigned)
        for order in active_orders
        if order.id not in assigned_ids
    ]
    strategy_names = {"balanced": "均衡", "completion": "完成率优先", "punctuality": "准时优先", "low_travel": "少跑优先", "stable": "稳定优先"}
    return _result(
        scenario, kind, version, SolverStatus.feasible, runtime_ms, assignments, unassigned, previous,
        note=f"按“{strategy_names.get(strategy, strategy)}”策略在 {effective_limit:g} 秒计算限制内返回可行方案；复杂路由未声称全局最优。",
        strategy=strategy,
    )


def replan_schedule(
    scenario: ScheduleScenario,
    version: int,
    previous: ScheduleResult,
    current_time: int,
    time_limit_seconds: float | None = None,
    strategy: str = "stable",
) -> ScheduleResult:
    """Replan only work that has not started, keeping the executed prefix byte-for-byte stable."""
    scenario_copy = scenario.model_copy(deep=True)
    orders = {o.id: o for o in scenario_copy.work_orders}
    fixed = [
        a.model_copy(deep=True)
        for a in previous.assignments
        if orders.get(a.work_order_id)
        and (
            orders[a.work_order_id].status.value in {"started", "completed"}
            or a.start_time <= current_time
            or a.arrival_time - a.travel_minutes <= current_time
        )
    ]
    fixed_ids = {a.work_order_id for a in fixed}
    scenario_copy.work_orders = [o for o in scenario_copy.work_orders if o.id not in fixed_ids]
    scenario_copy.locked_assignments = [l for l in scenario_copy.locked_assignments if l.work_order_id not in fixed_ids]

    original_orders = {o.id: o for o in scenario.work_orders}
    fixed_by_tech: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for item in fixed:
        fixed_by_tech[item.technician_id].append(item)
        item.changed = False
        if "已开始或已完成" not in "".join(item.explanation):
            item.explanation.append("已开始或已完成，本次局部重排保持原安排不变")
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
    )
    offsets = {tech.id: len(fixed_by_tech.get(tech.id, [])) for tech in scenario.technicians}
    previous_by_id = {a.work_order_id: a for a in previous.assignments}
    replanned: list[ScheduleAssignment] = []
    for item in partial.assignments:
        item.sequence += offsets[item.technician_id]
        old = previous_by_id.get(item.work_order_id)
        item.changed = bool(old and (old.technician_id != item.technician_id or old.sequence != item.sequence))
        change_note = "为接纳突发工单或降低 SLA 风险，本项安排发生调整"
        item.explanation = [line for line in item.explanation if line != change_note]
        if item.changed:
            item.explanation.append(change_note)
        replanned.append(item)
    merged = fixed + replanned
    final = _result(
        scenario,
        "replan",
        version,
        partial.solver_status,
        partial.runtime_ms,
        merged,
        partial.unassigned,
        previous,
        note=f"局部重排固定了 {len(fixed)} 个已开始/已完成工单；{partial.solver_note}",
        strategy=strategy,
    )
    return final


def validate_schedule(scenario: ScheduleScenario, result: ScheduleResult) -> list[str]:
    """Return human-readable hard-constraint violations."""
    errors: list[str] = []
    orders = {o.id: o for o in scenario.work_orders}
    techs = {t.id: t for t in scenario.technicians}
    locked = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    seen: set[str] = set()
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in result.assignments:
        order = orders.get(assignment.work_order_id)
        tech = techs.get(assignment.technician_id)
        if not order:
            errors.append(f"{assignment.work_order_id}: work order does not exist")
            continue
        if not tech:
            errors.append(f"{assignment.work_order_id}: technician {assignment.technician_id} does not exist")
            continue
        if order.id in seen:
            errors.append(f"{order.id}: assigned more than once")
        seen.add(order.id)
        if not _eligible(tech, order):
            errors.append(f"{order.id}: technician lacks required skill")
        if not order.window_start <= assignment.start_time <= order.window_end:
            errors.append(f"{order.id}: start outside time window")
        if assignment.arrival_time > assignment.start_time:
            errors.append(f"{order.id}: arrival occurs after service start")
        if assignment.finish_time != assignment.start_time + order.service_duration:
            errors.append(f"{order.id}: finish time does not match service duration")
        if assignment.sla_late_minutes != max(0, assignment.finish_time - order.sla_deadline):
            errors.append(f"{order.id}: SLA lateness is inconsistent")
        if assignment.start_time < tech.shift_start:
            errors.append(f"{order.id}: starts before technician shift")
        if assignment.finish_time > tech.shift_end + tech.overtime_limit:
            errors.append(f"{order.id}: exceeds overtime limit")
        if locked.get(order.id) and assignment.technician_id != locked[order.id]:
            errors.append(f"{order.id}: locked technician changed")
        grouped[tech.id].append(assignment)
    for tech_id, assignments in grouped.items():
        ordered = sorted(assignments, key=lambda item: item.sequence)
        if [item.sequence for item in ordered] != list(range(1, len(ordered) + 1)):
            errors.append(f"{tech_id}: route sequence is not contiguous")
        for before, after in zip(ordered, ordered[1:]):
            travel = travel_minutes(orders[before.work_order_id].location, orders[after.work_order_id].location)
            if after.travel_minutes != travel:
                errors.append(f"{after.work_order_id}: travel minutes do not match route")
            if after.arrival_time < before.finish_time + travel:
                errors.append(f"{after.work_order_id}: arrival precedes prior service and travel")
            if before.finish_time + travel > after.start_time:
                errors.append(f"{tech_id}: {before.work_order_id} overlaps travel to {after.work_order_id}")
        if ordered:
            tech = techs[tech_id]
            first = ordered[0]
            first_travel = travel_minutes(tech.start_location, orders[first.work_order_id].location)
            if first.travel_minutes != first_travel:
                errors.append(f"{first.work_order_id}: first-leg travel minutes do not match depot")
            if first.arrival_time < tech.shift_start + first_travel:
                errors.append(f"{first.work_order_id}: arrival precedes possible depot departure")
            last = ordered[-1]
            return_travel = travel_minutes(orders[last.work_order_id].location, tech.start_location)
            if last.finish_time + return_travel > tech.shift_end + tech.overtime_limit:
                errors.append(f"{tech_id}: route return exceeds overtime limit")
    return errors
