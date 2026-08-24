from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict

from ._version import __version__
from .hashing import content_hash
from .models import (
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CapacityOptionId,
    CapacityOptionResult,
    CapacityReferenceMode,
    CostAnalysis,
    DecisionAnalysisScope,
    DecisionCostPolicy,
    PlanCostBreakdown,
    PlanVersion,
    Point,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    RiskSimulationResult,
    ScheduleResult,
    ScheduleScenario,
    Skill,
    Technician,
)
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
    "add_technician": "增加一名复合技能技师",
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


def _validate_analysis_input(plan: PlanVersion, provider: TravelTimeProvider) -> ScheduleScenario:
    if plan.scenario_snapshot is None:
        raise DecisionAnalysisError("PLAN_SNAPSHOT_MISSING", "方案缺少业务快照")
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
    started = sorted(item.id for item in plan.scenario_snapshot.work_orders if item.status.value == "started")
    completed = sorted(item.id for item in plan.scenario_snapshot.work_orders if item.status.value == "completed")
    if started or completed:
        raise DecisionAnalysisError(
            "EXECUTION_ANALYSIS_CONTEXT_REQUIRED",
            "方案包含现场执行事实；当前版本尚未绑定执行水位，不能给出全日经营分析",
            started_work_order_ids=started,
            completed_work_order_ids=completed,
        )
    return plan.scenario_snapshot.model_copy(deep=True)


def schedule_signature(schedule: ScheduleResult) -> str:
    return content_hash(
        {
            "assignments": [
                (item.work_order_id, item.technician_id, item.sequence, item.start_time, item.finish_time)
                for item in schedule.assignments
            ],
            "unassigned": [(item.work_order_id, item.reason.value) for item in schedule.unassigned],
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
    technicians = {item.id: item for item in scenario.technicians}
    orders = {item.id: item for item in scenario.work_orders}
    technician_costs: dict[str, int] = {}
    labor_cost = 0
    overtime_cost = 0
    for kpi in schedule.kpis.technician:
        technician = technicians.get(kpi.technician_id)
        if technician is None:
            continue
        labor = kpi.occupied_minutes * technician.cost_per_minute_cents
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
) -> CostAnalysis:
    scenario = _validate_analysis_input(plan, provider)
    policy = policy or DecisionCostPolicy()
    input_hash = content_hash(
        {
            "scenario_snapshot_hash": plan.scenario_snapshot_hash,
            "schedule_signature": schedule_signature(plan.selected),
            "travel_model_fingerprint": provider.fingerprint,
            "policy": policy,
            "analysis_scope": DecisionAnalysisScope.full_day_plan,
            "analysis_code_version": __version__,
        }
    )
    return CostAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        schedule_signature=schedule_signature(plan.selected),
        analysis_scope=DecisionAnalysisScope.full_day_plan,
        travel_model_fingerprint=provider.fingerprint,
        analysis_code_version=__version__,
        analysis_input_hash=input_hash,
        policy=policy,
        policy_fingerprint=content_hash(policy),
        breakdown=analyze_plan_cost(scenario, plan.selected, policy),
        assumptions=[
            "人工成本按计划占用分钟计，包括服务、行程和等待。",
            "加班成本只计算相对正常人工成本的额外溢价。",
            "现金运营成本与 SLA/未服务经济损失分开列示；总经济影响不是财务结算。",
            "该口径只适用于尚未开始执行的全日计划；含服务中工单的方案会被明确拒绝。",
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
) -> tuple[ScheduleScenario, str, bool]:
    alternative = scenario.model_copy(deep=True)
    if option_id == "add_technician":
        skills = sorted(
            {skill for order in alternative.work_orders for skill in order.required_skills},
            key=lambda item: item.value,
        ) or list(Skill)
        shifts = alternative.technicians
        shift_start = min((item.shift_start for item in shifts), default=480)
        shift_end = max((item.shift_end for item in shifts), default=1020)
        rate = sorted(item.cost_per_minute_cents for item in shifts)
        cost = rate[len(rate) // 2] if rate else 100
        alternative.technicians.append(
            Technician(
                id="CAP-TECH-01",
                name="容量测算技师",
                skills=skills,
                shift_start=shift_start,
                shift_end=shift_end,
                start_location=_average_point([item.start_location for item in shifts]),
                overtime_limit=60,
                cost_per_minute_cents=cost,
                color="#526b5d",
            )
        )
        return alternative, "新增技师具备当前需求涉及的全部技能，班次覆盖现有团队服务时段。", True

    if option_id == "add_skill":
        demand = Counter(skill for order in alternative.work_orders for skill in order.required_skills)
        coverage = Counter(skill for technician in alternative.technicians for skill in technician.skills)
        candidates = sorted(demand, key=lambda skill: (coverage[skill], -demand[skill], skill.value))
        if not candidates:
            return alternative, "场景没有技能需求，无法形成补充技能方案。", False
        skill = candidates[0]
        targets = [item for item in alternative.technicians if skill not in item.skills]
        if not targets:
            return alternative, f"所有技师都已具备 {skill.value}，该方案没有增量。", False
        target = min(targets, key=lambda item: (len(item.skills), item.id))
        target.skills.append(skill)
        return alternative, f"为 {target.id} 补充最稀缺的 {skill.value} 技能。", True

    if option_id == "extend_shift":
        changed = False
        for technician in alternative.technicians:
            extended = min(1800, technician.shift_end + 60)
            changed = changed or extended != technician.shift_end
            technician.shift_end = extended
        return alternative, "所有技师班次结束时间延长 60 分钟，最高不超过次日 06:00。", changed

    if option_id == "allow_overtime":
        changed = False
        for technician in alternative.technicians:
            increased = min(240, technician.overtime_limit + 60)
            changed = changed or increased != technician.overtime_limit
            technician.overtime_limit = increased
        return alternative, "所有技师允许的加班上限增加 60 分钟，最高为 240 分钟。", changed

    if option_id == "relocate_one_technician_start":
        if not alternative.technicians or not alternative.work_orders:
            return alternative, "缺少技师或工单，无法评估出发点迁移。", False
        travel_by_tech: dict[str, int] = defaultdict(int)
        for assignment in base.assignments:
            travel_by_tech[assignment.technician_id] += assignment.travel_minutes
        target = max(alternative.technicians, key=lambda item: (travel_by_tech[item.id], item.id))
        target.start_location = _average_point([item.location for item in alternative.work_orders])
        return alternative, f"仅把行程最高的 {target.id} 出发点移至需求重心；不创建站点、库存或仓容。", True

    return alternative, "未服务工单由同日外部服务商承接，并假设在 SLA 内完成。", True


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


def _anchored_incremental_schedule(
    scenario: ScheduleScenario,
    alternative: ScheduleScenario,
    selected: ScheduleResult,
    option_id: CapacityOptionId,
    provider: TravelTimeProvider,
) -> tuple[ScheduleResult, bool]:
    """Keep every selected assignment fixed and place only previously unserved work.

    This is deliberately narrower than a reoptimization. It gives the selected
    plan delta mode a truthful counterfactual without attributing route changes
    from a different algorithm to the capacity option.
    """
    result = selected.model_copy(deep=True)
    result.id = f"ANALYSIS-{schedule_signature(selected)[:12]}-{option_id}"
    base_routes: dict[str, list] = defaultdict(list)
    for assignment in result.assignments:
        base_routes[assignment.technician_id].append(assignment)
    for route in base_routes.values():
        route.sort(key=lambda item: item.sequence)

    if option_id == "relocate_one_technician_start":
        original_techs = {item.id: item for item in scenario.technicians}
        alternative_techs = {item.id: item for item in alternative.technicians}
        orders = {item.id: item for item in alternative.work_orders}
        feasible = True
        for technician_id, route in base_routes.items():
            original = original_techs.get(technician_id)
            moved = alternative_techs.get(technician_id)
            if not original or not moved or original.start_location == moved.start_location or not route:
                continue
            first = route[0]
            travel = provider.minutes(moved.start_location, orders[first.work_order_id].location, moved.shift_start)
            arrival = moved.shift_start + travel
            if arrival > first.start_time:
                feasible = False
                continue
            first.travel_minutes = travel
            first.arrival_time = arrival
            first.evidence["capacity_relocation_origin"] = moved.start_location.model_dump(mode="json")
        result.kpis = calculate_kpis(alternative, result.assignments, result.unassigned, provider=provider)
        return result, feasible

    pending_ids = {item.work_order_id for item in selected.unassigned}
    if not pending_ids:
        result.kpis = calculate_kpis(alternative, result.assignments, result.unassigned, provider=provider)
        return result, True
    incremental = alternative.model_copy(deep=True)
    incremental.work_orders = [item for item in incremental.work_orders if item.id in pending_ids]
    incremental.locked_assignments = []
    order_locations = {item.id: item.location for item in alternative.work_orders}
    usable_technicians: list[Technician] = []
    for technician in incremental.technicians:
        route = base_routes.get(technician.id, [])
        if route:
            tail = route[-1]
            technician.shift_start = tail.finish_time
            technician.start_location = order_locations[tail.work_order_id]
        if technician.shift_start <= technician.shift_end + technician.overtime_limit:
            usable_technicians.append(technician)
    incremental.technicians = usable_technicians
    added = baseline_schedule(incremental, 0, strategy="baseline", provider=provider)
    sequence_offsets = {technician_id: len(route) for technician_id, route in base_routes.items()}
    added_assignments = [item.model_copy(deep=True) for item in added.assignments]
    for assignment in added_assignments:
        assignment.sequence += sequence_offsets.get(assignment.technician_id, 0)
        assignment.changed = False
        assignment.evidence["capacity_incremental_assignment"] = True
    result.assignments.extend(added_assignments)
    result.assignments.sort(key=lambda item: (item.technician_id, item.sequence))
    result.unassigned = [item.model_copy(deep=True) for item in added.unassigned]
    result.kpis = calculate_kpis(alternative, result.assignments, result.unassigned, provider=provider)
    return result, True


def capacity_analysis(
    plan: PlanVersion,
    request: CapacityAnalysisRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> CapacityAnalysis:
    scenario = _validate_analysis_input(plan, provider)
    policy = request.cost_policy
    selected_options = tuple(request.option_ids) if request.option_ids else CAPACITY_OPTIONS
    selected_signature = schedule_signature(plan.selected)
    if request.reference_mode is CapacityReferenceMode.selected_plan_delta:
        base = plan.selected.model_copy(deep=True)
        evaluation_method = "SELECTED_PLAN_ANCHORED_INCREMENTAL_GREEDY_V2"
        reference_policy_fingerprint = plan.selected.solver_policy.fingerprint if plan.selected.solver_policy else ""
    else:
        base = baseline_schedule(scenario, 0, strategy="baseline", provider=provider)
        evaluation_method = "CONTROLLED_DETERMINISTIC_GREEDY_REOPTIMIZATION_V2"
        reference_policy_fingerprint = base.solver_policy.fingerprint if base.solver_policy else ""
    base_cost = analyze_plan_cost(scenario, base, policy)
    analysis_input_hash = content_hash(
        {
            "scenario_snapshot_hash": plan.scenario_snapshot_hash,
            "selected_plan_signature": selected_signature,
            "reference_schedule_signature": schedule_signature(base),
            "reference_mode": request.reference_mode,
            "travel_model_fingerprint": provider.fingerprint,
            "request": request,
            "analysis_code_version": __version__,
        }
    )
    options: list[CapacityOptionResult] = []
    active_orders = [item for item in scenario.work_orders if item.status.value != "completed"]

    for option_id in selected_options:
        alternative, assumption, feasible = _capacity_scenario(scenario, option_id, base)
        fixed_cost = _capacity_fixed_cost(request, option_id)
        if option_id == "outsource_unserved":
            outsourced = len(base.unassigned)
            alternative_schedule = base
            alternative_cost = analyze_plan_cost(scenario, base, policy, outsourced_orders=outsourced)
            projected_total = (
                alternative_cost.total_economic_impact_cents - alternative_cost.unserved_revenue_cents + fixed_cost
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
            if request.reference_mode is CapacityReferenceMode.selected_plan_delta:
                alternative_schedule, anchored_feasible = _anchored_incremental_schedule(
                    scenario, alternative, base, option_id, provider
                )
                feasible = feasible and anchored_feasible
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
            projected_total = alternative_cost.total_economic_impact_cents + fixed_cost
            completion_rate = alternative_schedule.kpis.completion_rate
            sla_rate = alternative_schedule.kpis.committed_on_time_rate
            unassigned_count = alternative_schedule.kpis.unassigned_count
            travel_minutes = alternative_schedule.kpis.total_travel_minutes
            overtime_minutes = alternative_schedule.kpis.total_overtime_minutes
            signature = schedule_signature(alternative_schedule)
        options.append(
            CapacityOptionResult(
                option_id=option_id,
                name=CAPACITY_NAMES[option_id],
                assumption=assumption,
                feasible=feasible,
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
                fixed_capacity_cost_cents=fixed_cost,
                marginal_cost_cents=projected_total - base_cost.total_economic_impact_cents,
                projected_total_cost_cents=projected_total,
                schedule_signature=signature,
            )
        )
    return CapacityAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        analysis_scope=DecisionAnalysisScope.full_day_plan,
        analysis_code_version=__version__,
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
) -> RiskSimulationResult:
    scenario = _validate_analysis_input(plan, provider)
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
        failed = False
        for technician_id, route in routes.items():
            technician = technicians[technician_id]
            if technician_id in absent:
                unserved += len(route)
                failed = True
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
                    failed = True
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
                    failed = True
                current = finish
                current_location = order.location
            if route:
                return_minutes = provider.minutes(current_location, technician.start_location, current)
                delay_percent = rng.randint(0, request.travel_delay_max_percent)
                current += (return_minutes * (100 + delay_percent) + 99) // 100
                overtime = max(0, current - technician.shift_end)
                total_overtime += overtime
                if overtime > technician.overtime_limit:
                    failed = True
        sla_rates.append(on_time / active_count if active_count else 1.0)
        late_totals.append(total_late)
        overtime_totals.append(total_overtime)
        unserved_totals.append(unserved)
        if failed:
            failed_trials += 1

    input_payload = {
        "scenario_snapshot_hash": plan.scenario_snapshot_hash,
        "schedule_signature": schedule_signature(schedule),
        "request": request.model_dump(mode="json"),
        "resolved_seed": seed,
        "travel_model_fingerprint": provider.fingerprint,
        "analysis_code_version": __version__,
    }
    mean_sla = sum(sla_rates) / request.trials
    standard_error = statistics.pstdev(sla_rates) / math.sqrt(request.trials)
    ci_low = max(0.0, mean_sla - 1.96 * standard_error)
    ci_high = min(1.0, mean_sla + 1.96 * standard_error)
    disruption_probability = round(failed_trials / request.trials, 4)
    expected_total_unserved = round(sum(unserved_totals) / request.trials, 2)
    return RiskSimulationResult(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        schedule_signature=schedule_signature(schedule),
        analysis_scope=DecisionAnalysisScope.full_day_plan,
        travel_model_fingerprint=provider.fingerprint,
        execution_policy=request.execution_policy,
        analysis_code_version=__version__,
        simulation_input_hash=content_hash(input_payload),
        seed=seed,
        trials=request.trials,
        expected_sla_on_time_rate=round(mean_sla, 4),
        sla_rate_ci_low=round(ci_low, 4),
        sla_rate_ci_high=round(ci_high, 4),
        late_minutes_p50=_percentile(late_totals, 0.5),
        late_minutes_p90=_percentile(late_totals, 0.9),
        late_minutes_p95=_percentile(late_totals, 0.95),
        expected_overtime_minutes=round(sum(overtime_totals) / request.trials, 2),
        additional_disruption_probability=disruption_probability,
        baseline_unserved_orders=initially_unserved,
        expected_total_unserved_orders=expected_total_unserved,
        plan_failure_probability=disruption_probability,
        expected_unserved_orders=expected_total_unserved,
        assumptions=[
            "旅行延误和服务时长波动使用离散整数比例，固定 seed 可复现。",
            "技师缺勤会使其整条路线失效；客户不在场按 10 分钟现场处置后离开。",
            "突发工单以 30–90 分钟的随机容量占用表示，不生成正式方案版本。",
            "默认服从已发布开始时刻；只有显式 EARLIEST_FEASIBLE_EXECUTION 才按最早可行时刻执行。",
            "已知未分配需求与新增扰动概率分开列示，不把原有缺口称为随机失效。",
        ],
    )
