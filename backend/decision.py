from __future__ import annotations

import math
import random
from collections import Counter, defaultdict

from .hashing import content_hash
from .models import (
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CapacityOptionId,
    CapacityOptionResult,
    CostAnalysis,
    DecisionCostPolicy,
    PlanCostBreakdown,
    PlanVersion,
    Point,
    RiskSimulationRequest,
    RiskSimulationResult,
    ScheduleResult,
    ScheduleScenario,
    Skill,
    Technician,
)
from .scheduler import baseline_schedule
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider

CAPACITY_OPTIONS: tuple[CapacityOptionId, ...] = (
    "add_technician",
    "add_skill",
    "extend_shift",
    "allow_overtime",
    "outsource_unserved",
    "add_service_depot",
)

CAPACITY_NAMES: dict[CapacityOptionId, str] = {
    "add_technician": "增加一名复合技能技师",
    "add_skill": "补充最稀缺技能",
    "extend_shift": "统一延长班次 60 分钟",
    "allow_overtime": "增加 60 分钟加班容量",
    "outsource_unserved": "外包未服务工单",
    "add_service_depot": "在需求中心增加服务站点",
}

FIXED_CAPACITY_COST_CENTS: dict[CapacityOptionId, int] = {
    "add_technician": 60_000,
    "add_skill": 15_000,
    "extend_shift": 0,
    "allow_overtime": 0,
    "outsource_unserved": 0,
    "add_service_depot": 40_000,
}


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
    total = labor_cost + travel_cost + overtime_cost + sla_penalty + unserved_revenue + outsourcing_cost
    return PlanCostBreakdown(
        labor_cost_cents=labor_cost,
        travel_cost_cents=travel_cost,
        overtime_cost_cents=overtime_cost,
        sla_penalty_cents=sla_penalty,
        unserved_revenue_cents=unserved_revenue,
        outsourcing_cost_cents=outsourcing_cost,
        total_cost_cents=total,
        technician_cost_cents=technician_costs,
    )


def cost_analysis(
    plan: PlanVersion,
    policy: DecisionCostPolicy | None = None,
) -> CostAnalysis:
    if plan.scenario_snapshot is None:
        raise ValueError("方案缺少业务快照")
    policy = policy or DecisionCostPolicy()
    return CostAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        policy=policy,
        policy_fingerprint=content_hash(policy),
        breakdown=analyze_plan_cost(plan.scenario_snapshot, plan.selected, policy),
        assumptions=[
            "人工成本按计划占用分钟计，包括服务、行程和等待。",
            "加班成本只计算相对正常人工成本的额外溢价。",
            "未服务收入损失按优先级和 VIP 标记估算，不等同于财务结算。",
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

    if option_id == "add_service_depot":
        if not alternative.technicians or not alternative.work_orders:
            return alternative, "缺少技师或工单，无法评估新服务站点。", False
        travel_by_tech: dict[str, int] = defaultdict(int)
        for assignment in base.assignments:
            travel_by_tech[assignment.technician_id] += assignment.travel_minutes
        target = max(alternative.technicians, key=lambda item: (travel_by_tech[item.id], item.id))
        target.start_location = _average_point([item.location for item in alternative.work_orders])
        return alternative, f"把行程最高的 {target.id} 调整为从需求重心的新站点出发。", True

    return alternative, "未服务工单由同日外部服务商承接，并假设在 SLA 内完成。", True


def capacity_analysis(
    plan: PlanVersion,
    request: CapacityAnalysisRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> CapacityAnalysis:
    if plan.scenario_snapshot is None:
        raise ValueError("方案缺少业务快照")
    scenario = plan.scenario_snapshot.model_copy(deep=True)
    policy = request.cost_policy
    selected_options = tuple(request.option_ids) if request.option_ids else CAPACITY_OPTIONS
    base = baseline_schedule(scenario, 0, strategy="baseline", provider=provider)
    base_cost = analyze_plan_cost(scenario, base, policy)
    options: list[CapacityOptionResult] = []
    active_orders = [item for item in scenario.work_orders if item.status.value != "completed"]

    for option_id in selected_options:
        alternative, assumption, feasible = _capacity_scenario(scenario, option_id, base)
        fixed_cost = FIXED_CAPACITY_COST_CENTS[option_id]
        if option_id == "outsource_unserved":
            outsourced = len(base.unassigned)
            alternative_schedule = base
            alternative_cost = analyze_plan_cost(scenario, base, policy, outsourced_orders=outsourced)
            projected_total = alternative_cost.total_cost_cents - alternative_cost.unserved_revenue_cents
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
            alternative_schedule = baseline_schedule(alternative, 0, strategy="baseline", provider=provider)
            alternative_cost = analyze_plan_cost(alternative, alternative_schedule, policy)
            projected_total = alternative_cost.total_cost_cents + fixed_cost
            completion_rate = alternative_schedule.kpis.completion_rate
            sla_rate = alternative_schedule.kpis.sla_on_time_rate
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
                sla_improvement_percentage_points=round((sla_rate - base.kpis.sla_on_time_rate) * 100, 2),
                unassigned_delta=unassigned_count - base.kpis.unassigned_count,
                travel_delta_minutes=travel_minutes - base.kpis.total_travel_minutes,
                overtime_delta_minutes=overtime_minutes - base.kpis.total_overtime_minutes,
                fixed_capacity_cost_cents=fixed_cost,
                marginal_cost_cents=projected_total - base_cost.total_cost_cents,
                projected_total_cost_cents=projected_total,
                schedule_signature=signature,
            )
        )
    return CapacityAnalysis(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        evaluation_method="DETERMINISTIC_GREEDY_WHAT_IF_V1",
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
) -> RiskSimulationResult:
    if plan.scenario_snapshot is None:
        raise ValueError("方案缺少业务快照")
    scenario = plan.scenario_snapshot
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
            for assignment in route:
                order = orders[assignment.work_order_id]
                delay_percent = rng.randint(0, request.travel_delay_max_percent)
                travel = (assignment.travel_minutes * (100 + delay_percent) + 99) // 100
                arrival = current + travel
                start = max(arrival, order.window_start, order.reported_at or 0)
                if rng.randrange(10_000) < request.customer_no_show_basis_points:
                    unserved += 1
                    current = start + 10
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
            if route:
                return_minutes = int(route[-1].evidence.get("route_return_travel_minutes", 0))
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
    }
    return RiskSimulationResult(
        scenario_id=plan.scenario_id,
        plan_version_id=plan.id,
        plan_number=plan.number,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        simulation_input_hash=content_hash(input_payload),
        seed=seed,
        trials=request.trials,
        expected_sla_on_time_rate=round(sum(sla_rates) / request.trials, 4),
        late_minutes_p50=_percentile(late_totals, 0.5),
        late_minutes_p90=_percentile(late_totals, 0.9),
        late_minutes_p95=_percentile(late_totals, 0.95),
        expected_overtime_minutes=round(sum(overtime_totals) / request.trials, 2),
        plan_failure_probability=round(failed_trials / request.trials, 4),
        expected_unserved_orders=round(sum(unserved_totals) / request.trials, 2),
        assumptions=[
            "旅行延误和服务时长波动使用离散整数比例，固定 seed 可复现。",
            "技师缺勤会使其整条路线失效；客户不在场按 10 分钟现场处置后离开。",
            "突发工单以 30–90 分钟的随机容量占用表示，不生成正式方案版本。",
            "计划中已知的未分配工单计入 SLA 和预计未服务数，但不重复计为随机失效。",
        ],
    )
