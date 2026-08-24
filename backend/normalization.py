from __future__ import annotations

from collections import defaultdict

from .hashing import content_hash
from .models import ScheduleAssignment, ScheduleResult, ScheduleScenario
from .scheduler import BUSINESS_SCORE_POLICY_VERSION, METRIC_POLICY_VERSION, calculate_kpis, objective_breakdown
from .timeutils import hhmm
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider


def _change_flags(
    assignments: list[ScheduleAssignment],
    source: ScheduleResult | None,
) -> dict[str, bool]:
    if source is None:
        return {item.work_order_id: False for item in assignments}

    current_ids = {item.work_order_id for item in assignments}
    source_items = [item for item in source.assignments if item.work_order_id in current_ids]
    source_by_id = {item.work_order_id: item for item in source_items}

    def predecessors(items: list[ScheduleAssignment]) -> dict[str, str | None]:
        answer: dict[str, str | None] = {}
        grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
        for item in items:
            grouped[item.technician_id].append(item)
        for route in grouped.values():
            previous: str | None = None
            for item in sorted(route, key=lambda assignment: assignment.sequence):
                answer[item.work_order_id] = previous
                previous = item.work_order_id
        return answer

    before = predecessors(source_items)
    after = predecessors([item for item in assignments if item.work_order_id in source_by_id])
    return {
        item.work_order_id: bool(
            (old := source_by_id.get(item.work_order_id))
            and (old.technician_id != item.technician_id or before.get(item.work_order_id) != after.get(item.work_order_id))
        )
        for item in assignments
    }


def normalize_schedule(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    source: ScheduleResult | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> ScheduleResult:
    """Rebuild every assignment fact that is derived from business input or route order."""
    normalized = result.model_copy(deep=True)
    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    locks = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in normalized.assignments:
        grouped[assignment.technician_id].append(assignment)
    for route in grouped.values():
        route.sort(key=lambda item: item.sequence)

    changes = _change_flags(normalized.assignments, source if normalized.kind == "replan" else None)

    def cheapest_delta(technician_id: str, order_id: str) -> int:
        technician = technicians[technician_id]
        order = orders[order_id]
        route = [item for item in grouped.get(technician_id, []) if item.work_order_id != order_id]
        points = [
            technician.start_location,
            *[orders[item.work_order_id].location for item in route],
            technician.start_location,
        ]
        return min(
            provider.minutes(points[index], order.location)
            + provider.minutes(order.location, points[index + 1])
            - provider.minutes(points[index], points[index + 1])
            for index in range(len(points) - 1)
        )

    for technician_id, route in grouped.items():
        technician = technicians[technician_id]
        for index, assignment in enumerate(route):
            order = orders[assignment.work_order_id]
            previous_point = technician.start_location if index == 0 else orders[route[index - 1].work_order_id].location
            next_point = technician.start_location if index == len(route) - 1 else orders[route[index + 1].work_order_id].location
            travel = provider.minutes(previous_point, order.location)
            locked = locks.get(order.id) == technician_id
            changed = changes.get(order.id, False)
            eligible = [
                item.id
                for item in scenario.technicians
                if set(order.required_skills).issubset(set(item.skills))
            ]
            alternatives = {
                item.id: cheapest_delta(item.id, order.id)
                for item in scenario.technicians
                if item.id != technician_id and item.id in eligible
            }
            insertion_delta = (
                provider.minutes(previous_point, order.location)
                + provider.minutes(order.location, next_point)
                - provider.minutes(previous_point, next_point)
            )
            explanation = [
                f"{technician.id} 具备 {'、'.join(skill.value for skill in order.required_skills)} 技能",
                f"预计 {hhmm(assignment.start_time)} 开始，满足 {hhmm(order.window_start)}–{hhmm(order.window_end)} 客户时间窗",
                f"本段行程 {travel} 分钟，服务预计于 {hhmm(assignment.finish_time)} 完成",
            ]
            if len(eligible) > 1:
                explanation.append(f"共有 {len(eligible)} 名技师满足技能要求；路线计算选择 {technician.id}")
            explanation.append(
                "不会产生加班"
                if assignment.finish_time <= technician.shift_end
                else f"产生 {assignment.finish_time - technician.shift_end} 分钟加班，未超过上限"
            )
            if locked:
                explanation.append("人工锁定已生效，本次求解保持指定技师")
            if changed:
                explanation.append("为接纳突发工单或降低 SLA 风险，本项安排发生调整")
            if order.vip:
                explanation.append("VIP 工单的未分配代价较高")
            explanation.append(f"在当前路线的前后工单之间插入此单，行程净增 {insertion_delta} 分钟")
            if alternatives:
                explanation.append(f"其他技能匹配路线的最低行程增量估算为 {min(alternatives.values())} 分钟（未重新排时）")

            assignment.travel_minutes = travel
            assignment.sla_late_minutes = max(0, assignment.finish_time - order.sla_deadline)
            assignment.locked = locked
            assignment.changed = changed
            assignment.explanation = explanation
            assignment.evidence = {
                "required_skills": [item.value for item in order.required_skills],
                "eligible_technician_ids": eligible,
                "window_start": order.window_start,
                "window_end": order.window_end,
                "arrival_time": assignment.arrival_time,
                "start_time": assignment.start_time,
                "finish_time": assignment.finish_time,
                "leg_travel_minutes": travel,
                "overtime_minutes": max(0, assignment.finish_time - technician.shift_end),
                "route_insertion_travel_delta_minutes": insertion_delta,
                "alternative_route_travel_delta_minutes": alternatives,
                "alternative_delta_scope": "travel_only_without_rescheduling",
            }

    normalized.assignments = sorted(normalized.assignments, key=lambda item: (item.technician_id, item.sequence))
    normalized.scenario_id = scenario.id
    normalized.scenario_revision = scenario.revision
    normalized.scenario_snapshot_hash = content_hash(scenario)
    normalized.solver_config_hash = content_hash(scenario.solver_config)
    normalized.travel_model_version = provider.version
    normalized.metric_policy_version = METRIC_POLICY_VERSION
    normalized.business_score_policy_version = BUSINESS_SCORE_POLICY_VERSION
    normalized.kpis = calculate_kpis(
        scenario,
        normalized.assignments,
        normalized.unassigned,
        source if normalized.kind == "replan" else None,
        provider,
    )
    change_count = sum(1 for item in normalized.assignments if item.changed) if normalized.kind == "replan" else 0
    normalized.objective_breakdown = objective_breakdown(
        scenario,
        normalized.kpis,
        normalized.unassigned,
        normalized.assignments,
        change_count,
    )
    normalized.business_score = round(sum(normalized.objective_breakdown.values()), 2)
    normalized.objective = normalized.business_score
    return normalized
