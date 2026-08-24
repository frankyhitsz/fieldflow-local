from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict

from ._version import __version__
from .hashing import content_hash
from .models import (
    AnalysisHorizon,
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CapacityOptionId,
    CapacityOptionResult,
    CapacityReferenceMode,
    CapacityVerificationReport,
    CapacityViolation,
    CostAnalysis,
    CostCadence,
    DecisionAnalysisContext,
    DecisionAnalysisScope,
    DecisionCostPolicy,
    LaborCostMode,
    PlanCostBreakdown,
    PlanVersion,
    Point,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    RiskSimulationResult,
    ScheduleAssignment,
    ScheduleResult,
    ScheduleScenario,
    Skill,
    Technician,
    UnassignedWorkOrder,
)
from .provenance import DECISION_ALGORITHM_VERSION, decision_build_sha
from .scheduler import baseline_schedule, calculate_kpis
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider

CAPACITY_OPTIONS: tuple[CapacityOptionId, ...] = (
    "add_technician",
    "add_skill",
    "extend_shift",
    "allow_overtime",
    "outsource_unserved",
    "relocate_one_technician_start",
)

CAPACITY_NAMES: dict[CapacityOptionId, str] = {
    "add_technician": "增加一名候选技师",
    "add_skill": "补充最稀缺技能",
    "extend_shift": "统一延长班次 60 分钟",
    "allow_overtime": "增加 60 分钟加班容量",
    "outsource_unserved": "外包未服务工单",
    "relocate_one_technician_start": "将一名高行程技师的出发点移至需求中心",
}


class DecisionAnalysisError(ValueError):
    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def default_analysis_context(
    scope: DecisionAnalysisScope = DecisionAnalysisScope.ex_ante_frozen_plan,
) -> DecisionAnalysisContext:
    return DecisionAnalysisContext(analysis_scope=scope)


def _core_kpi_payload(schedule: ScheduleResult) -> dict[str, object]:
    payload = schedule.kpis.model_dump(mode="json")
    for key in (
        "stability_rate",
        "same_technician_rate",
        "adjacency_preservation_rate",
        "start_time_shift_median",
        "start_time_shift_p90",
        "start_time_shift_over_15m_count",
        "customer_notification_count",
    ):
        payload.pop(key, None)
    return payload


def _validate_analysis_input(
    plan: PlanVersion,
    provider: TravelTimeProvider,
    context: DecisionAnalysisContext,
) -> ScheduleScenario:
    if context.analysis_scope is not DecisionAnalysisScope.ex_ante_frozen_plan:
        raise DecisionAnalysisError(
            "ANALYSIS_SCOPE_NOT_SUPPORTED",
            "当前版本只支持事前冻结计划分析；执行实绩与剩余预测尚未实现",
            requested_scope=context.analysis_scope.value,
            supported_scopes=[DecisionAnalysisScope.ex_ante_frozen_plan.value],
        )
    if plan.scenario_snapshot is None:
        raise DecisionAnalysisError("PLAN_SNAPSHOT_MISSING", "方案缺少业务快照")
    frozen_hash = content_hash(plan.scenario_snapshot)
    if plan.scenario_snapshot_hash != frozen_hash:
        raise DecisionAnalysisError(
            "PLAN_SNAPSHOT_HASH_MISMATCH",
            "方案业务快照与冻结哈希不一致，不能生成经营分析",
            stored_hash=plan.scenario_snapshot_hash,
            recomputed_hash=frozen_hash,
        )
    if plan.selected.scenario_snapshot_hash != plan.scenario_snapshot_hash:
        raise DecisionAnalysisError(
            "SCHEDULE_SNAPSHOT_HASH_MISMATCH",
            "排程引用的业务快照与方案版本不一致",
            plan_snapshot_hash=plan.scenario_snapshot_hash,
            schedule_snapshot_hash=plan.selected.scenario_snapshot_hash,
        )
    if plan.selected.travel_model_fingerprint != provider.fingerprint:
        raise DecisionAnalysisError(
            "TRAVEL_MODEL_NOT_AVAILABLE",
            "当前旅行模型与该方案冻结的模型不一致，不能生成可复现的经营分析",
            plan_travel_model_fingerprint=plan.selected.travel_model_fingerprint,
            available_travel_model_fingerprint=provider.fingerprint,
        )
    solver_policy = plan.selected.solver_policy
    if solver_policy is None:
        raise DecisionAnalysisError(
            "SOLVER_POLICY_NOT_REPRODUCIBLE",
            "方案缺少完整求解政策快照，不能生成可复现的经营分析",
        )
    expected_policy_fingerprint = content_hash(solver_policy.model_dump(exclude={"fingerprint"}, mode="json"))
    if solver_policy.fingerprint != expected_policy_fingerprint:
        raise DecisionAnalysisError(
            "SOLVER_POLICY_NOT_REPRODUCIBLE",
            "方案求解政策指纹与快照内容不一致",
            stored_fingerprint=solver_policy.fingerprint,
            recomputed_fingerprint=expected_policy_fingerprint,
        )
    orders = {item.id: item for item in plan.scenario_snapshot.work_orders}
    for assignment in plan.selected.assignments:
        order = orders.get(assignment.work_order_id)
        if not order:
            raise DecisionAnalysisError(
                "SCHEDULE_INTEGRITY_FAILED",
                "排程引用了冻结快照中不存在的工单",
                work_order_id=assignment.work_order_id,
            )
        if assignment.finish_time - assignment.start_time != order.service_duration:
            raise DecisionAnalysisError(
                "SCHEDULE_INTEGRITY_FAILED",
                "排程服务时长与冻结工单不一致",
                work_order_id=assignment.work_order_id,
            )
        if assignment.sla_late_minutes != max(0, assignment.finish_time - order.sla_deadline):
            raise DecisionAnalysisError(
                "SCHEDULE_INTEGRITY_FAILED",
                "排程 SLA 延迟字段不是由冻结工单重算所得",
                work_order_id=assignment.work_order_id,
            )
    recomputed_kpis = calculate_kpis(
        plan.scenario_snapshot,
        plan.selected.assignments,
        plan.selected.unassigned,
        provider=provider,
    )
    recomputed = plan.selected.model_copy(update={"kpis": recomputed_kpis})
    if _core_kpi_payload(plan.selected) != _core_kpi_payload(recomputed):
        raise DecisionAnalysisError(
            "SCHEDULE_KPI_INTEGRITY_FAILED",
            "排程 KPI 无法从冻结快照和 assignment 重新得到",
        )
    return plan.scenario_snapshot.model_copy(deep=True)


def schedule_signature(schedule: ScheduleResult) -> str:
    """Hash every authoritative normalized schedule field used by analysis."""
    payload = schedule.model_dump(mode="json")
    for volatile in ("id", "created_at", "runtime_ms"):
        payload.pop(volatile, None)
    return content_hash(payload)


def canonical_decision_input_hash(
    plan: PlanVersion,
    analysis_type: str,
    authoritative_request: object,
    context: DecisionAnalysisContext,
    provider: TravelTimeProvider,
) -> str:
    return content_hash(
        {
            "analysis_type": analysis_type,
            "scenario_snapshot_hash": plan.scenario_snapshot_hash,
            "canonical_schedule_hash": schedule_signature(plan.selected),
            "travel_model_fingerprint": provider.fingerprint,
            "authoritative_request": authoritative_request,
            "analysis_context": context,
            "analysis_code_version": __version__,
            "algorithm_version": DECISION_ALGORITHM_VERSION,
            "build_sha": decision_build_sha(),
        }
    )


def analyze_plan_cost(
    scenario: ScheduleScenario,
    schedule: ScheduleResult,
    policy: DecisionCostPolicy | None = None,
    *,
    outsourced_orders: int = 0,
) -> PlanCostBreakdown:
    """Calculate operating exposure using integer cents only.

    Labor covers planned occupied minutes (service, travel and waiting).
    Overtime is an additional premium, not a second copy of base wages.
    """
    policy = policy or DecisionCostPolicy()
    if policy.labor_cost_mode is LaborCostMode.salaried_allocation:
        raise DecisionAnalysisError(
            "LABOR_COST_MODE_NOT_SUPPORTED",
            "固定薪酬分摊需要月薪和分摊规则，当前数据模型尚不支持",
            requested_mode=policy.labor_cost_mode.value,
        )
    technicians = {item.id: item for item in scenario.technicians}
    orders = {item.id: item for item in scenario.work_orders}
    technician_costs: dict[str, int] = {}
    labor_cost = 0
    overtime_cost = 0
    for kpi in schedule.kpis.technician:
        technician = technicians.get(kpi.technician_id)
        if technician is None:
            continue
        paid_minutes = (
            technician.shift_end - technician.shift_start
            if policy.labor_cost_mode is LaborCostMode.paid_shift
            else kpi.occupied_minutes
        )
        labor = paid_minutes * technician.cost_per_minute_cents
        overtime = (
            kpi.overtime_minutes * technician.cost_per_minute_cents * policy.overtime_premium_basis_points // 10_000
        )
        technician_costs[technician.id] = labor + overtime
        labor_cost += labor
        overtime_cost += overtime

    travel_cost = schedule.kpis.total_travel_minutes * policy.travel_cost_per_minute_cents
    sla_penalty = sum(item.sla_late_minutes for item in schedule.assignments)
    sla_penalty *= policy.sla_penalty_per_late_minute_cents
    priority_revenue = {
        "low": policy.unserved_low_revenue_cents,
        "normal": policy.unserved_normal_revenue_cents,
        "high": policy.unserved_high_revenue_cents,
        "urgent": policy.unserved_urgent_revenue_cents,
    }
    unserved_revenue = 0
    for item in schedule.unassigned:
        order = orders.get(item.work_order_id)
        if order is None:
            continue
        unserved_revenue += priority_revenue[order.priority.value]
        if order.vip:
            unserved_revenue += policy.vip_revenue_premium_cents
    outsourcing_cost = outsourced_orders * policy.outsourcing_cost_per_order_cents
    cash = labor_cost + travel_cost + overtime_cost + outsourcing_cost
    service_failure_loss = sla_penalty + unserved_revenue
    total = cash + service_failure_loss
    return PlanCostBreakdown(
        labor_cost_cents=labor_cost,
        travel_cost_cents=travel_cost,
        overtime_cost_cents=overtime_cost,
        sla_penalty_cents=sla_penalty,
        unserved_revenue_cents=unserved_revenue,
        outsourcing_cost_cents=outsourcing_cost,
        cash_operating_cost_cents=cash,
        service_failure_loss_cents=service_failure_loss,
        total_economic_impact_cents=total,
        total_cost_cents=total,
        technician_cost_cents=technician_costs,
    )


def cost_analysis(
    plan: PlanVersion,
    policy: DecisionCostPolicy | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
    horizon: AnalysisHorizon | None = None,
) -> CostAnalysis:
    context = context or default_analysis_context()
    horizon = horizon or AnalysisHorizon()
    scenario = _validate_analysis_input(plan, provider, context)
    policy = policy or DecisionCostPolicy()
    signature = schedule_signature(plan.selected)
    build_sha = decision_build_sha()
    input_hash = canonical_decision_input_hash(
        plan,
        "COST",
        {"policy": policy, "analysis_horizon": horizon},
        context,
        provider,
    )
    breakdown = analyze_plan_cost(scenario, plan.selected, policy)
    return CostAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        schedule_signature=signature,
        analysis_scope=context.analysis_scope,
        current_execution_watermark=context.current_execution_watermark,
        analysis_as_of_time=context.analysis_as_of_time,
        execution_context_hash=context.execution_context_hash,
        actual_execution_included=context.actual_execution_included,
        travel_model_fingerprint=provider.fingerprint,
        analysis_code_version=__version__,
        algorithm_version=DECISION_ALGORITHM_VERSION,
        build_sha=build_sha,
        analysis_input_hash=input_hash,
        analysis_horizon=horizon,
        policy=policy,
        policy_fingerprint=content_hash(policy),
        breakdown=breakdown,
        horizon_total_economic_impact_cents=breakdown.total_economic_impact_cents * horizon.days,
        assumptions=[
            "该记录是事前冻结计划分析，不含实际执行，也不代表当前剩余预测。",
            (
                "人工成本按完整付费班次计。"
                if policy.labor_cost_mode is LaborCostMode.paid_shift
                else "人工成本按计划占用分钟计，包括服务、行程和等待。"
            ),
            "加班成本只计算相对正常人工成本的额外溢价。",
            "现金运营成本与 SLA/未服务经济损失分开列示；总经济影响不是财务结算。",
            f"分析周期为 {horizon.days} 个工作日；每日计划假设在周期内重复。",
        ],
    )


def _average_point(points: list[Point]) -> Point:
    if not points:
        return Point(x=50, y=50)
    return Point(
        x=round(sum(item.x for item in points) / len(points), 3),
        y=round(sum(item.y for item in points) / len(points), 3),
    )


def _capacity_scenario(
    scenario: ScheduleScenario,
    option_id: CapacityOptionId,
    base: ScheduleResult,
    request: CapacityAnalysisRequest,
) -> tuple[ScheduleScenario, str, bool, dict[str, object]]:
    alternative = scenario.model_copy(deep=True)
    if option_id == "add_technician":
        shifts = alternative.technicians
        archetype = request.candidate_technician
        if archetype is None:
            unserved_ids = {item.work_order_id for item in base.unassigned}
            candidates = [item for item in alternative.work_orders if item.id in unserved_ids]
            if not candidates:
                return alternative, "当前方案没有未服务工单，新增技师没有可测算的增量需求。", False, {}
            priority = {"urgent": 4, "high": 3, "normal": 2, "low": 1}
            target_order = max(
                candidates,
                key=lambda item: (item.drop_penalty, priority[item.priority.value], item.vip, item.id),
            )
            starts = sorted(item.shift_start for item in shifts)
            ends = sorted(item.shift_end for item in shifts)
            rates = sorted(item.cost_per_minute_cents for item in shifts)
            archetype_payload = {
                "name": "候选技师",
                "skills": target_order.required_skills,
                "shift_start": starts[len(starts) // 2] if starts else 480,
                "shift_end": ends[len(ends) // 2] if ends else 1020,
                "start_location": _average_point([item.start_location for item in shifts]),
                "overtime_limit": 60,
                "cost_per_minute_cents": rates[len(rates) // 2] if rates else 100,
            }
        else:
            archetype_payload = archetype.model_dump(mode="json")
        alternative.technicians.append(
            Technician(
                id="CAP-TECH-01",
                **archetype_payload,
                color="#526b5d",
            )
        )
        changes: dict[str, object] = {"candidate_technician": archetype_payload}
        return (
            alternative,
            "新增技师使用明确的人员模板；未指定时，只针对最高损失未服务工单推荐技能组合。",
            True,
            changes,
        )

    if option_id == "add_skill":
        unserved_ids = {item.work_order_id for item in base.unassigned}
        unserved_reasons = {item.work_order_id: item.reason.value for item in base.unassigned}
        unserved = [item for item in alternative.work_orders if item.id in unserved_ids]
        if not unserved:
            return alternative, "当前方案没有未服务工单，补充技能没有可测算的增量需求。", False, {}
        if request.skill_investment_target:
            target = next(
                (item for item in alternative.technicians if item.id == request.skill_investment_target.technician_id),
                None,
            )
            skill = request.skill_investment_target.skill
            if not target or skill in target.skills:
                return alternative, "指定的技师或技能投资目标无效。", False, {}
        else:
            scored: list[tuple[int, str, str, Technician, Skill]] = []
            for technician in alternative.technicians:
                for skill in Skill:
                    if skill in technician.skills:
                        continue
                    unlocked = [
                        order
                        for order in unserved
                        if skill in order.required_skills
                        and set(order.required_skills).issubset(set(technician.skills) | {skill})
                    ]
                    score = sum(order.drop_penalty + (10_000 if order.vip else 0) for order in unlocked)
                    if score:
                        scored.append((score, technician.id, skill.value, technician, skill))
            if not scored:
                return alternative, "单项技能培训无法解除当前未服务工单的技能约束。", False, {}
            _, _, _, target, skill = max(scored, key=lambda item: (item[0], item[1], item[2]))
        target.skills.append(skill)
        unlocked_orders = sorted(
            order.id
            for order in unserved
            if skill in order.required_skills and set(order.required_skills).issubset(set(target.skills))
        )
        changes = {
            "technician_id": str(target.id),
            "added_skill": skill.value,
            "targeted_work_order_ids": unlocked_orders,
            "source_unassigned_reasons": {
                work_order_id: unserved_reasons[work_order_id] for work_order_id in unlocked_orders
            },
        }
        return (
            alternative,
            f"为 {target.id} 补充 {skill.value}；目标工单和原未分配原因均记录在 changed_inputs。",
            True,
            changes,
        )

    if option_id == "extend_shift":
        changed = False
        for technician in alternative.technicians:
            extended = min(1800, technician.shift_end + 60)
            changed = changed or extended != technician.shift_end
            technician.shift_end = extended
        return (
            alternative,
            "所有技师班次结束时间延长 60 分钟，最高不超过次日 06:00。",
            changed,
            {"shift_extension_minutes": 60},
        )

    if option_id == "allow_overtime":
        changed = False
        for technician in alternative.technicians:
            increased = min(240, technician.overtime_limit + 60)
            changed = changed or increased != technician.overtime_limit
            technician.overtime_limit = increased
        return (
            alternative,
            "所有技师允许的加班上限增加 60 分钟，最高为 240 分钟。",
            changed,
            {"overtime_limit_increase_minutes": 60},
        )

    if option_id == "relocate_one_technician_start":
        if not alternative.technicians or not alternative.work_orders:
            return alternative, "缺少技师或工单，无法评估出发点迁移。", False, {}
        travel_by_tech: dict[str, int] = defaultdict(int)
        for assignment in base.assignments:
            travel_by_tech[assignment.technician_id] += assignment.travel_minutes
        target = max(alternative.technicians, key=lambda item: (travel_by_tech[item.id], item.id))
        previous = target.start_location.model_dump(mode="json")
        target.start_location = _average_point([item.location for item in alternative.work_orders])
        return (
            alternative,
            f"仅把行程最高的 {target.id} 出发点移至需求重心；不创建站点、库存或仓容。",
            True,
            {
                "technician_id": target.id,
                "previous_start_location": previous,
                "new_start_location": target.start_location.model_dump(mode="json"),
            },
        )

    return (
        alternative,
        "未服务工单由同日外部服务商承接，并假设在 SLA 内完成。",
        bool(base.unassigned),
        {"outsourced_work_order_ids": [item.work_order_id for item in base.unassigned]},
    )


def _capacity_fixed_cost(request: CapacityAnalysisRequest, option_id: CapacityOptionId) -> int:
    policy = request.capacity_policy
    return {
        "add_technician": policy.add_technician_fixed_cost_cents,
        "add_skill": policy.add_skill_fixed_cost_cents,
        "extend_shift": policy.extend_shift_fixed_cost_cents,
        "allow_overtime": policy.allow_overtime_fixed_cost_cents,
        "outsource_unserved": policy.outsource_unserved_fixed_cost_cents,
        "relocate_one_technician_start": policy.relocate_one_technician_start_fixed_cost_cents,
    }[option_id]


def _capacity_cost_cadence(request: CapacityAnalysisRequest, option_id: CapacityOptionId) -> CostCadence:
    policy = request.capacity_policy
    return {
        "add_technician": policy.add_technician_cost_cadence,
        "add_skill": policy.add_skill_cost_cadence,
        "extend_shift": policy.extend_shift_cost_cadence,
        "allow_overtime": policy.allow_overtime_cost_cadence,
        "outsource_unserved": policy.outsource_unserved_cost_cadence,
        "relocate_one_technician_start": policy.relocate_one_technician_start_cost_cadence,
    }[option_id]


def _horizon_cost(fixed_cost: int, cadence: CostCadence, horizon: AnalysisHorizon, units: int = 1) -> int:
    if cadence is CostCadence.one_time:
        return fixed_cost
    if cadence in {CostCadence.per_day, CostCadence.per_shift}:
        return fixed_cost * horizon.days
    if cadence is CostCadence.per_order:
        return fixed_cost * units * horizon.days
    return round(fixed_cost * horizon.days / horizon.workdays_per_month)


def _daily_equivalent_cost(fixed_cost: int, cadence: CostCadence, horizon: AnalysisHorizon, units: int = 1) -> int:
    return round(_horizon_cost(fixed_cost, cadence, horizon, units) / horizon.days)


def _tail_append_counterfactual(
    alternative: ScheduleScenario,
    selected: ScheduleResult,
    option_id: CapacityOptionId,
    provider: TravelTimeProvider,
) -> ScheduleResult:
    """Append only unserved work while retaining the real terminal depot."""
    result = selected.model_copy(deep=True)
    result.id = f"ANALYSIS-{schedule_signature(selected)[:12]}-{option_id}"
    technicians = {item.id: item for item in alternative.technicians}
    orders = {item.id: item for item in alternative.work_orders}
    routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in result.assignments:
        routes[assignment.technician_id].append(assignment)
    for route in routes.values():
        route.sort(key=lambda item: item.sequence)

    if option_id == "relocate_one_technician_start":
        for technician_id, route in routes.items():
            technician = technicians.get(technician_id)
            if not technician or not route:
                continue
            first = route[0]
            order = orders[first.work_order_id]
            travel = provider.minutes(technician.start_location, order.location, technician.shift_start)
            first.travel_minutes = travel
            first.arrival_time = technician.shift_start + travel
            first.evidence["capacity_relocation_origin"] = technician.start_location.model_dump(mode="json")

    unassigned_by_id = {item.work_order_id: item for item in selected.unassigned}
    pending = sorted(
        (orders[work_order_id] for work_order_id in unassigned_by_id if work_order_id in orders),
        key=lambda item: (item.sla_deadline, item.window_end, item.id),
    )
    still_unassigned: list[UnassignedWorkOrder] = []
    for order in pending:
        choices: list[tuple[int, int, int, Technician]] = []
        for technician in alternative.technicians:
            if not set(order.required_skills).issubset(set(technician.skills)):
                continue
            route = routes.get(technician.id, [])
            if route:
                tail = route[-1]
                available_at = tail.finish_time
                current_location = orders[tail.work_order_id].location
            else:
                available_at = technician.shift_start
                current_location = technician.start_location
            travel = provider.minutes(current_location, order.location, available_at)
            arrival = available_at + travel
            start = max(arrival, order.window_start, order.reported_at or 0)
            finish = start + order.service_duration
            return_to_terminal = provider.minutes(order.location, technician.start_location, finish)
            if start <= order.window_end and finish + return_to_terminal <= (
                technician.shift_end + technician.overtime_limit
            ):
                choices.append((start, travel, finish, technician))
        if not choices:
            original = unassigned_by_id[order.id]
            still_unassigned.append(original.model_copy(deep=True))
            continue
        start, travel, finish, technician = min(choices, key=lambda item: (item[0], item[1], item[3].id))
        route = routes.get(technician.id, [])
        available_at = route[-1].finish_time if route else technician.shift_start
        assignment = ScheduleAssignment(
            work_order_id=order.id,
            technician_id=technician.id,
            sequence=len(route) + 1,
            arrival_time=available_at + travel,
            start_time=start,
            finish_time=finish,
            travel_minutes=travel,
            sla_late_minutes=max(0, finish - order.sla_deadline),
            explanation=[
                "容量反事实仅追加原未服务工单，不移动任何已承诺 assignment。",
                "返程按技师真实终点计算，并纳入加班上限。",
            ],
            evidence={
                "capacity_incremental_assignment": True,
                "terminal_depot": technician.start_location.model_dump(mode="json"),
                "return_travel_minutes": provider.minutes(order.location, technician.start_location, finish),
            },
        )
        routes[technician.id].append(assignment)
        result.assignments.append(assignment)
    result.assignments.sort(key=lambda item: (item.technician_id, item.sequence))
    result.unassigned = still_unassigned
    result.kpis = calculate_kpis(alternative, result.assignments, result.unassigned, provider=provider)
    return result


def verify_counterfactual_schedule(
    scenario: ScheduleScenario,
    schedule: ScheduleResult,
    provider: TravelTimeProvider,
    *,
    fixed_schedule: ScheduleResult | None = None,
    externally_covered_work_order_ids: set[str] | None = None,
) -> CapacityVerificationReport:
    violations: list[CapacityViolation] = []
    technicians = {item.id: item for item in scenario.technicians}
    orders = {item.id: item for item in scenario.work_orders if item.status.value != "completed"}
    assigned_ids = [item.work_order_id for item in schedule.assignments]
    unassigned_ids = [item.work_order_id for item in schedule.unassigned]
    external_ids = externally_covered_work_order_ids or set()
    for work_order_id in sorted(set(assigned_ids) & set(unassigned_ids)):
        violations.append(
            CapacityViolation(code="DUPLICATE_COVERAGE", message="工单同时已分配和未分配", work_order_id=work_order_id)
        )
    for work_order_id in sorted((set(assigned_ids) | set(unassigned_ids)) & external_ids):
        violations.append(
            CapacityViolation(
                code="DUPLICATE_COVERAGE",
                message="工单同时由内部排程和外部服务覆盖",
                work_order_id=work_order_id,
            )
        )
    for work_order_id in sorted({item for item in assigned_ids if assigned_ids.count(item) > 1}):
        violations.append(
            CapacityViolation(code="DUPLICATE_ASSIGNMENT", message="工单被重复分配", work_order_id=work_order_id)
        )
    for work_order_id in sorted({item for item in unassigned_ids if unassigned_ids.count(item) > 1}):
        violations.append(
            CapacityViolation(
                code="DUPLICATE_UNASSIGNED", message="工单被重复标记为未分配", work_order_id=work_order_id
            )
        )
    for work_order_id in sorted((set(assigned_ids + unassigned_ids) | external_ids) - set(orders)):
        violations.append(
            CapacityViolation(
                code="UNKNOWN_WORK_ORDER",
                message="反事实覆盖了不存在或已完成的工单",
                work_order_id=work_order_id,
            )
        )
    missing = set(orders) - set(assigned_ids) - set(unassigned_ids) - external_ids
    for work_order_id in sorted(missing):
        violations.append(
            CapacityViolation(code="MISSING_COVERAGE", message="活动工单未进入反事实结果", work_order_id=work_order_id)
        )

    locked = {item.work_order_id: item.technician_id for item in scenario.locked_assignments}
    for work_order_id in sorted(set(unassigned_ids) & set(locked)):
        violations.append(
            CapacityViolation(
                code="LOCKED_WORK_ORDER_UNASSIGNED",
                message="人工锁定工单不能标记为未分配",
                work_order_id=work_order_id,
                technician_id=locked[work_order_id],
            )
        )
    for work_order_id in sorted(external_ids & set(locked)):
        violations.append(
            CapacityViolation(
                code="LOCKED_WORK_ORDER_OUTSOURCED",
                message="人工锁定工单不能改由外部服务覆盖",
                work_order_id=work_order_id,
                technician_id=locked[work_order_id],
            )
        )
    routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in schedule.assignments:
        routes[assignment.technician_id].append(assignment)
    for technician_id, route in routes.items():
        technician = technicians.get(technician_id)
        if not technician:
            violations.append(
                CapacityViolation(
                    code="UNKNOWN_TECHNICIAN", message="反事实使用了不存在的技师", technician_id=technician_id
                )
            )
            continue
        ordered = sorted(route, key=lambda item: item.sequence)
        if [item.sequence for item in ordered] != list(range(1, len(ordered) + 1)):
            violations.append(
                CapacityViolation(code="NON_CONTIGUOUS_SEQUENCE", message="路线序号不连续", technician_id=technician_id)
            )
        available_at = technician.shift_start
        location = technician.start_location
        for assignment in ordered:
            order = orders.get(assignment.work_order_id)
            if not order:
                violations.append(
                    CapacityViolation(
                        code="UNKNOWN_WORK_ORDER",
                        message="反事实引用了不存在或已完成的工单",
                        work_order_id=assignment.work_order_id,
                        technician_id=technician_id,
                    )
                )
                continue
            if not set(order.required_skills).issubset(set(technician.skills)):
                violations.append(
                    CapacityViolation(
                        code="SKILL_MISMATCH",
                        message="技师缺少工单所需技能",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            travel = provider.minutes(location, order.location, available_at)
            arrival = available_at + travel
            if assignment.travel_minutes != travel or assignment.arrival_time != arrival:
                violations.append(
                    CapacityViolation(
                        code="TRAVEL_DISCONTINUITY",
                        message="路线旅行或到达时间不连续",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            if (
                assignment.start_time < max(arrival, order.window_start, order.reported_at or 0)
                or assignment.start_time > order.window_end
            ):
                violations.append(
                    CapacityViolation(
                        code="TIME_WINDOW_VIOLATION",
                        message="工单开始时间不满足到达或客户时间窗",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            if assignment.finish_time != assignment.start_time + order.service_duration:
                violations.append(
                    CapacityViolation(
                        code="SERVICE_DURATION_MISMATCH",
                        message="反事实服务时长不正确",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            if assignment.sla_late_minutes != max(0, assignment.finish_time - order.sla_deadline):
                violations.append(
                    CapacityViolation(
                        code="SLA_LATE_MISMATCH",
                        message="反事实 SLA 延迟字段不正确",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            if locked.get(order.id) not in {None, technician_id}:
                violations.append(
                    CapacityViolation(
                        code="LOCK_VIOLATION",
                        message="反事实违反人工锁定",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            available_at = assignment.finish_time
            location = order.location
        if ordered:
            return_minutes = provider.minutes(location, technician.start_location, available_at)
            if available_at + return_minutes > technician.shift_end + technician.overtime_limit:
                violations.append(
                    CapacityViolation(
                        code="RETURN_OVERTIME_LIMIT_EXCEEDED",
                        message="末单返回真实出发点后超过加班上限",
                        technician_id=technician_id,
                    )
                )

    if fixed_schedule:
        actual = {item.work_order_id: item for item in schedule.assignments}
        for fixed in fixed_schedule.assignments:
            candidate = actual.get(fixed.work_order_id)
            if not candidate or (
                candidate.technician_id,
                candidate.sequence,
                candidate.start_time,
                candidate.finish_time,
            ) != (fixed.technician_id, fixed.sequence, fixed.start_time, fixed.finish_time):
                violations.append(
                    CapacityViolation(
                        code="FIXED_ASSIGNMENT_CHANGED",
                        message="容量反事实改变了已承诺 assignment",
                        work_order_id=fixed.work_order_id,
                    )
                )
    return CapacityVerificationReport(valid=not violations, violations=violations)


def capacity_analysis(
    plan: PlanVersion,
    request: CapacityAnalysisRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
) -> CapacityAnalysis:
    context = context or default_analysis_context(request.analysis_scope or DecisionAnalysisScope.ex_ante_frozen_plan)
    scenario = _validate_analysis_input(plan, provider, context)
    policy = request.cost_policy
    selected_options = tuple(request.option_ids) if request.option_ids else CAPACITY_OPTIONS
    selected_signature = schedule_signature(plan.selected)
    if request.reference_mode is CapacityReferenceMode.selected_plan_delta:
        base = plan.selected.model_copy(deep=True)
        evaluation_method = "SELECTED_PLAN_TAIL_APPEND_COUNTERFACTUAL_V3"
        reference_policy_fingerprint = plan.selected.solver_policy.fingerprint if plan.selected.solver_policy else ""
    else:
        base = baseline_schedule(scenario, 0, strategy="baseline", provider=provider)
        evaluation_method = "CONTROLLED_DETERMINISTIC_GREEDY_REOPTIMIZATION_V2"
        reference_policy_fingerprint = base.solver_policy.fingerprint if base.solver_policy else ""
    base_cost = analyze_plan_cost(scenario, base, policy)
    build_sha = decision_build_sha()
    analysis_input_hash = canonical_decision_input_hash(plan, "CAPACITY", request, context, provider)
    options: list[CapacityOptionResult] = []
    active_orders = [item for item in scenario.work_orders if item.status.value != "completed"]

    for option_id in selected_options:
        alternative, assumption, applicable, changed_inputs = _capacity_scenario(scenario, option_id, base, request)
        fixed_cost = _capacity_fixed_cost(request, option_id)
        cadence = _capacity_cost_cadence(request, option_id)
        outsourced = 0
        if option_id == "outsource_unserved":
            outsourced = len(base.unassigned)
            alternative_schedule = base.model_copy(deep=True)
            external_ids = {item.work_order_id for item in base.unassigned}
            alternative_schedule.unassigned = []
            alternative_cost = analyze_plan_cost(scenario, base, policy, outsourced_orders=outsourced)
            daily_alternative_total = (
                alternative_cost.total_economic_impact_cents - alternative_cost.unserved_revenue_cents
            )
            active_count = len(active_orders)
            on_time_assigned = sum(1 for item in base.assignments if item.sla_late_minutes == 0)
            completion_rate = 1.0 if active_count else 1.0
            sla_rate = (on_time_assigned + outsourced) / active_count if active_count else 1.0
            unassigned_count = 0
            travel_minutes = base.kpis.total_travel_minutes
            overtime_minutes = base.kpis.total_overtime_minutes
            signature = content_hash(
                {"base": schedule_signature(base), "outsourced": sorted(item.work_order_id for item in base.unassigned)}
            )
        else:
            external_ids = set()
            if request.reference_mode is CapacityReferenceMode.selected_plan_delta:
                alternative_schedule = _tail_append_counterfactual(alternative, base, option_id, provider)
            else:
                alternative_schedule = baseline_schedule(alternative, 0, strategy="baseline", provider=provider)
                option_policy = alternative_schedule.solver_policy
                if not option_policy or option_policy.fingerprint != reference_policy_fingerprint:
                    raise DecisionAnalysisError(
                        "CONTROLLED_POLICY_DRIFT",
                        "容量选项没有使用与参考排程相同的求解政策",
                        option_id=option_id,
                    )
            alternative_cost = analyze_plan_cost(alternative, alternative_schedule, policy)
            daily_alternative_total = alternative_cost.total_economic_impact_cents
            completion_rate = alternative_schedule.kpis.completion_rate
            sla_rate = alternative_schedule.kpis.committed_on_time_rate
            unassigned_count = alternative_schedule.kpis.unassigned_count
            travel_minutes = alternative_schedule.kpis.total_travel_minutes
            overtime_minutes = alternative_schedule.kpis.total_overtime_minutes
            signature = schedule_signature(alternative_schedule)
        verification = verify_counterfactual_schedule(
            alternative,
            alternative_schedule,
            provider,
            fixed_schedule=base if request.reference_mode is CapacityReferenceMode.selected_plan_delta else None,
            externally_covered_work_order_ids=external_ids,
        )
        violations = list(verification.violations)
        if not applicable:
            violations.insert(0, CapacityViolation(code="OPTION_NOT_APPLICABLE", message=assumption))
        daily_operating_delta = daily_alternative_total - base_cost.total_economic_impact_cents
        charged_fixed_cost = fixed_cost if applicable else 0
        horizon_fixed_cost = _horizon_cost(
            charged_fixed_cost,
            cadence,
            request.analysis_horizon,
            max(1, outsourced),
        )
        daily_equivalent_fixed = _daily_equivalent_cost(
            charged_fixed_cost,
            cadence,
            request.analysis_horizon,
            max(1, outsourced),
        )
        one_time_investment = charged_fixed_cost if cadence is CostCadence.one_time else 0
        daily_benefit = max(0, -daily_operating_delta)
        break_even_days = (
            round(one_time_investment / daily_benefit, 2) if one_time_investment and daily_benefit else None
        )
        horizon_total_impact = daily_operating_delta * request.analysis_horizon.days + horizon_fixed_cost
        marginal_daily_impact = daily_operating_delta + daily_equivalent_fixed
        projected_total = max(0, base_cost.total_economic_impact_cents + marginal_daily_impact)
        options.append(
            CapacityOptionResult(
                option_id=option_id,
                name=CAPACITY_NAMES[option_id],
                assumption=assumption,
                option_applicable=applicable,
                schedule_feasible=verification.valid,
                violations=violations,
                changed_inputs=changed_inputs,
                placement_mode=request.placement_mode,
                feasible=applicable and verification.valid,
                completion_rate=round(completion_rate, 4),
                sla_on_time_rate=round(sla_rate, 4),
                unassigned_count=unassigned_count,
                travel_minutes=travel_minutes,
                overtime_minutes=overtime_minutes,
                completion_improvement_percentage_points=round((completion_rate - base.kpis.completion_rate) * 100, 2),
                sla_improvement_percentage_points=round((sla_rate - base.kpis.committed_on_time_rate) * 100, 2),
                unassigned_delta=unassigned_count - base.kpis.unassigned_count,
                travel_delta_minutes=travel_minutes - base.kpis.total_travel_minutes,
                overtime_delta_minutes=overtime_minutes - base.kpis.total_overtime_minutes,
                fixed_capacity_cost_cents=charged_fixed_cost,
                fixed_cost_cadence=cadence,
                one_time_investment_cents=one_time_investment,
                daily_operating_delta_cents=daily_operating_delta,
                horizon_total_impact_cents=horizon_total_impact,
                break_even_days=break_even_days,
                marginal_cost_cents=marginal_daily_impact,
                projected_total_cost_cents=projected_total,
                schedule_signature=signature,
            )
        )
    return CapacityAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        analysis_scope=context.analysis_scope,
        current_execution_watermark=context.current_execution_watermark,
        analysis_as_of_time=context.analysis_as_of_time,
        execution_context_hash=context.execution_context_hash,
        actual_execution_included=context.actual_execution_included,
        analysis_code_version=__version__,
        algorithm_version=DECISION_ALGORITHM_VERSION,
        build_sha=build_sha,
        analysis_input_hash=analysis_input_hash,
        evaluation_method=evaluation_method,
        reference_mode=request.reference_mode,
        selected_plan_signature=selected_signature,
        reference_schedule_signature=schedule_signature(base),
        reference_solver_policy_fingerprint=reference_policy_fingerprint,
        reference_travel_model_fingerprint=provider.fingerprint,
        reference_kpis=base.kpis,
        cost_policy_fingerprint=content_hash(policy),
        capacity_policy=request.capacity_policy,
        capacity_policy_fingerprint=content_hash(request.capacity_policy),
        analysis_horizon=request.analysis_horizon,
        placement_mode=request.placement_mode,
        base_schedule_signature=schedule_signature(base),
        base_cost=base_cost,
        options=options,
    )


def _percentile(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def simulate_plan_risk(
    plan: PlanVersion,
    request: RiskSimulationRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
) -> RiskSimulationResult:
    context = context or default_analysis_context(request.analysis_scope or DecisionAnalysisScope.ex_ante_frozen_plan)
    scenario = _validate_analysis_input(plan, provider, context)
    schedule = plan.selected
    seed = scenario.seed if request.seed is None else request.seed
    rng = random.Random(seed)
    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    routes: dict[str, list] = defaultdict(list)
    for assignment in schedule.assignments:
        routes[assignment.technician_id].append(assignment)
    for route in routes.values():
        route.sort(key=lambda item: item.sequence)
    active_count = len([item for item in scenario.work_orders if item.status.value != "completed"])
    initially_unserved = len(schedule.unassigned)
    sla_rates: list[float] = []
    late_totals: list[int] = []
    overtime_totals: list[int] = []
    unserved_totals: list[int] = []
    failed_trials = 0
    absence_trials = 0
    no_show_trials = 0
    window_failure_trials = 0
    overtime_failure_trials = 0
    emergency_trials = 0

    for _ in range(request.trials):
        absent = {
            technician_id for technician_id in routes if rng.randrange(10_000) < request.technician_absence_basis_points
        }
        emergency_delay: dict[str, int] = {}
        if routes and rng.randrange(10_000) < request.emergency_order_basis_points:
            technician_id = sorted(routes)[rng.randrange(len(routes))]
            emergency_delay[technician_id] = rng.randint(30, 90)
        on_time = 0
        total_late = 0
        total_overtime = 0
        unserved = initially_unserved
        # Already-unassigned work is a known baseline exposure, not a random
        # failure of this trial. It remains in expected_unserved_orders and the
        # SLA denominator; failure probability measures additional disruption.
        absence_failure = bool(absent)
        no_show_failure = False
        window_failure = False
        overtime_failure = False
        emergency_disruption = bool(emergency_delay)
        for technician_id, route in routes.items():
            technician = technicians[technician_id]
            if technician_id in absent:
                unserved += len(route)
                continue
            current = technician.shift_start + emergency_delay.get(technician_id, 0)
            current_location = technician.start_location
            for assignment in route:
                order = orders[assignment.work_order_id]
                delay_percent = rng.randint(0, request.travel_delay_max_percent)
                planned_travel = provider.minutes(current_location, order.location, current)
                travel = (planned_travel * (100 + delay_percent) + 99) // 100
                arrival = current + travel
                start = max(arrival, order.window_start, order.reported_at or 0)
                if request.execution_policy is RiskExecutionPolicy.follow_published_schedule:
                    start = max(start, assignment.start_time)
                if rng.randrange(10_000) < request.customer_no_show_basis_points:
                    unserved += 1
                    current = start + 10
                    current_location = order.location
                    no_show_failure = True
                    continue
                jitter = request.service_duration_jitter_percent
                service_percent = rng.randint(100 - jitter, 100 + jitter)
                duration = max(1, (order.service_duration * service_percent + 50) // 100)
                finish = start + duration
                late = max(0, finish - order.sla_deadline)
                total_late += late
                if late == 0:
                    on_time += 1
                if start > order.window_end:
                    window_failure = True
                current = finish
                current_location = order.location
            if route:
                return_minutes = provider.minutes(current_location, technician.start_location, current)
                delay_percent = rng.randint(0, request.travel_delay_max_percent)
                current += (return_minutes * (100 + delay_percent) + 99) // 100
                overtime = max(0, current - technician.shift_end)
                total_overtime += overtime
                if overtime > technician.overtime_limit:
                    overtime_failure = True
        sla_rates.append(on_time / active_count if active_count else 1.0)
        late_totals.append(total_late)
        overtime_totals.append(total_overtime)
        unserved_totals.append(unserved)
        if absence_failure or no_show_failure or window_failure or overtime_failure or emergency_disruption:
            failed_trials += 1
        absence_trials += int(absence_failure)
        no_show_trials += int(no_show_failure)
        window_failure_trials += int(window_failure)
        overtime_failure_trials += int(overtime_failure)
        emergency_trials += int(emergency_disruption)

    input_hash = canonical_decision_input_hash(
        plan,
        "RISK",
        {"request": request, "resolved_seed": seed},
        context,
        provider,
    )
    mean_sla = sum(sla_rates) / request.trials
    standard_error = statistics.pstdev(sla_rates) / math.sqrt(request.trials)
    ci_low = max(0.0, mean_sla - 1.96 * standard_error)
    ci_high = min(1.0, mean_sla + 1.96 * standard_error)
    disruption_probability = round(failed_trials / request.trials, 4)
    expected_total_unserved = round(sum(unserved_totals) / request.trials, 2)
    late_p50 = _percentile(late_totals, 0.5)
    late_p90 = _percentile(late_totals, 0.9)
    late_p95 = _percentile(late_totals, 0.95)
    return RiskSimulationResult(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        schedule_signature=schedule_signature(schedule),
        analysis_scope=context.analysis_scope,
        current_execution_watermark=context.current_execution_watermark,
        analysis_as_of_time=context.analysis_as_of_time,
        execution_context_hash=context.execution_context_hash,
        actual_execution_included=context.actual_execution_included,
        travel_model_fingerprint=provider.fingerprint,
        execution_policy=request.execution_policy,
        analysis_code_version=__version__,
        algorithm_version=DECISION_ALGORITHM_VERSION,
        build_sha=decision_build_sha(),
        simulation_input_hash=input_hash,
        seed=seed,
        trials=request.trials,
        expected_sla_on_time_rate=round(mean_sla, 4),
        monte_carlo_mean_ci_low=round(ci_low, 4),
        monte_carlo_mean_ci_high=round(ci_high, 4),
        sla_rate_ci_low=round(ci_low, 4),
        sla_rate_ci_high=round(ci_high, 4),
        full_day_total_late_minutes_p50=late_p50,
        full_day_total_late_minutes_p90=late_p90,
        full_day_total_late_minutes_p95=late_p95,
        late_minutes_p50=late_p50,
        late_minutes_p90=late_p90,
        late_minutes_p95=late_p95,
        expected_overtime_minutes=round(sum(overtime_totals) / request.trials, 2),
        additional_disruption_probability=disruption_probability,
        absence_disruption_probability=round(absence_trials / request.trials, 4),
        no_show_disruption_probability=round(no_show_trials / request.trials, 4),
        window_failure_probability=round(window_failure_trials / request.trials, 4),
        overtime_failure_probability=round(overtime_failure_trials / request.trials, 4),
        emergency_capacity_disruption_probability=round(emergency_trials / request.trials, 4),
        baseline_unserved_orders=initially_unserved,
        expected_total_unserved_orders=expected_total_unserved,
        plan_failure_probability=disruption_probability,
        expected_unserved_orders=expected_total_unserved,
        assumptions=[
            "该记录是事前冻结计划分析，不含实际执行，也不代表当前剩余预测。",
            "旅行延误和服务时长波动使用离散整数比例，固定 seed 可复现。",
            "技师缺勤会使其整条路线失效；客户不在场按 10 分钟现场处置后离开。",
            "突发工单以 30–90 分钟的随机容量占用表示，不生成正式方案版本。",
            "默认服从已发布开始时刻；只有显式 EARLIEST_FEASIBLE_EXECUTION 才按最早可行时刻执行。",
            "已知未分配需求与新增扰动概率分开列示，不把原有缺口称为随机失效。",
            "模拟均值抽样区间只描述 Monte Carlo 均值误差，不是现实业务参数的置信区间。",
            "迟到分位数表示每次模拟的全日总迟到分钟，不是单张工单迟到分位数。",
        ],
    )
