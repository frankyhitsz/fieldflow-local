from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TypedDict

from ._version import __version__
from .hashing import content_hash
from .models import (
    AnalysisHorizon,
    AnalysisIntegrityStatus,
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CapacityCostSource,
    CapacityCounterfactualKPI,
    CapacityDecisionStatus,
    CapacityOptionId,
    CapacityOptionResult,
    CapacityReferenceMode,
    CapacityVerificationReport,
    CapacityViolation,
    CostAnalysis,
    CostCadence,
    CostComponent,
    CostComponentKind,
    CostLedger,
    CostUnitType,
    DecisionAnalysisContext,
    DecisionAnalysisScope,
    DecisionCostPolicy,
    EmergencyDecisionInformationSet,
    EmergencyDispatchPolicy,
    EmergencyLocationPolicy,
    EmergencyResponderSelectionPolicy,
    ExternalAssignment,
    LaborCostMode,
    PlanCostBreakdown,
    PlanVersion,
    Point,
    RiskArtifactDetailPolicy,
    RiskExecutionPolicy,
    RiskSimulationRequest,
    RiskSimulationResult,
    RiskTrialMetric,
    RouteEntryContext,
    ScheduleAssignment,
    ScheduleResult,
    ScheduleScenario,
    SimulatedWorkOrderOutcome,
    SimulationEmergencyEvent,
    Skill,
    Technician,
    TechnicianCostSource,
    UnassignedWorkOrder,
    WorkOrderDisposition,
)
from .provenance import DECISION_ALGORITHM_VERSION, build_plan_manifest_payload, decision_build_sha
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


@dataclass
class _RiskRouteState:
    """Mutable state for one technician on one Monte Carlo timeline."""

    technician_id: str
    current: int
    location: Point
    predecessor_id: str
    next_assignment_index: int = 0
    ready_assignment_index: int | None = None
    returned: bool = False
    on_time: int = 0
    total_late: int = 0
    emergency_late: int = 0
    total_overtime: int = 0
    unserved: int = 0
    no_show_failure: bool = False
    window_failure: bool = False
    overtime_failure: bool = False
    emergency_completed: bool = False
    emergency_on_time: bool = False
    emergency_dispatch_time: int | None = None
    emergency_finish_time: int | None = None
    emergency_dispatch_location: Point | None = None
    work_order_late_minutes: dict[str, int | None] = field(default_factory=dict)
    work_order_outcomes: dict[str, SimulatedWorkOrderOutcome] = field(default_factory=dict)


class _RiskTrialOutcome(TypedDict):
    on_time: int
    total_late: int
    published_total_late: int
    emergency_late: int
    total_overtime: int
    unserved: int
    no_show_failure: bool
    window_failure: bool
    overtime_failure: bool
    emergency_completed: bool
    emergency_on_time: bool
    emergency_technician_id: str | None
    emergency_dispatch_time: int | None
    emergency_finish_time: int | None
    emergency_route_terminal_time: int | None
    emergency_dispatch_location: Point | None
    emergency_decision_information_set: EmergencyDecisionInformationSet | None
    emergency_sla_failure: bool
    work_order_late_minutes: dict[str, int | None]
    work_order_outcomes: dict[str, SimulatedWorkOrderOutcome]


class DecisionAnalysisError(ValueError):
    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def default_analysis_context(
    scope: DecisionAnalysisScope = DecisionAnalysisScope.frozen_full_plan,
) -> DecisionAnalysisContext:
    return DecisionAnalysisContext(analysis_scope=scope)


def validate_frozen_plan_integrity(
    plan: PlanVersion,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
) -> None:
    """Reject a frozen plan whose snapshot, schedule, policy, constraints, or KPI evidence changed."""
    _validate_analysis_input(
        plan,
        provider,
        default_analysis_context(
            DecisionAnalysisScope.publication_remaining_plan
            if plan.selected.kind == "replan" and plan.publication_planning_context is not None
            else DecisionAnalysisScope.frozen_full_plan
        ),
    )


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
    analysis_type: str = "COST",
) -> ScheduleScenario:
    if plan.effective_integrity is not AnalysisIntegrityStatus.verified:
        raise DecisionAnalysisError(
            "PLAN_REATTESTATION_REQUIRED"
            if plan.effective_integrity is AnalysisIntegrityStatus.legacy_unattested
            else "PLAN_ATTESTATION_FAILED",
            "方案未达到当前发布证明标准，不能用于经营分析"
            if plan.effective_integrity is AnalysisIntegrityStatus.legacy_unattested
            else "方案发布证明缺失或不一致，不能用于经营分析",
            plan_version_id=plan.id,
        )
    expected_scope = (
        DecisionAnalysisScope.publication_remaining_plan
        if plan.selected.kind == "replan" and plan.publication_planning_context is not None
        else DecisionAnalysisScope.frozen_full_plan
    )
    accepted_scopes = {expected_scope, DecisionAnalysisScope.ex_ante_frozen_plan}
    if context.analysis_scope not in accepted_scopes:
        raise DecisionAnalysisError(
            "ANALYSIS_SCOPE_MISMATCH",
            "请求范围与方案类型不一致",
            requested_scope=context.analysis_scope.value,
            supported_scopes=[expected_scope.value],
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
    if plan.published_schedule_hash and plan.published_schedule_hash != content_hash(plan.selected):
        raise DecisionAnalysisError(
            "PUBLISHED_SCHEDULE_HASH_MISMATCH",
            "冻结排程与发布时保存的完整哈希不一致",
            stored_hash=plan.published_schedule_hash,
            recomputed_hash=content_hash(plan.selected),
        )
    publication_context = plan.publication_planning_context
    if plan.selected.kind == "replan" and analysis_type in {"CAPACITY", "RISK"} and publication_context is None:
        raise DecisionAnalysisError(
            "REPLAN_ANALYSIS_CONTEXT_NOT_AVAILABLE",
            "该历史重排方案没有冻结发布时路线起点；风险与容量分析会产生误导，已拒绝运行",
            plan_version_id=plan.id,
        )
    if publication_context is not None:
        expected_context_hash = content_hash(
            publication_context.model_dump(exclude={"context_fingerprint"}, mode="json")
        )
        if (
            publication_context.context_fingerprint != expected_context_hash
            or plan.publication_planning_context_hash != expected_context_hash
        ):
            raise DecisionAnalysisError(
                "PUBLICATION_CONTEXT_HASH_MISMATCH",
                "发布计划上下文与冻结指纹不一致",
            )
    verification_artifact = plan.publication_verification_artifact
    if verification_artifact is not None:
        artifact_hash = content_hash(verification_artifact.model_dump(exclude={"artifact_hash"}, mode="json"))
        if artifact_hash != verification_artifact.artifact_hash:
            raise DecisionAnalysisError(
                "PUBLICATION_VERIFICATION_ARTIFACT_HASH_MISMATCH",
                "发布验证证据与冻结指纹不一致",
            )
        if verification_artifact.verified_schedule_hash != content_hash(plan.selected):
            raise DecisionAnalysisError(
                "PUBLICATION_VERIFICATION_SCHEDULE_MISMATCH",
                "发布验证证据没有绑定当前冻结排程",
            )
        report_hash = content_hash(verification_artifact.transaction_verification_report)
        if report_hash != plan.publication_verification_report_hash:
            raise DecisionAnalysisError(
                "PUBLICATION_VERIFICATION_REPORT_HASH_MISMATCH",
                "发布验证报告与方案保存的报告指纹不一致",
            )
        expected_manifest_hash = content_hash(build_plan_manifest_payload(plan))
        if plan.publication_manifest_hash != expected_manifest_hash:
            raise DecisionAnalysisError(
                "PUBLICATION_MANIFEST_HASH_MISMATCH",
                "发布清单没有完整绑定业务快照、排程与验证证据",
            )
    structural = verify_counterfactual_schedule(
        plan.scenario_snapshot,
        plan.selected,
        provider,
        route_entries=(publication_context.route_entries if publication_context else None),
        frozen_work_order_ids=(
            {item.work_order_id for item in publication_context.frozen_booking_identities}
            if publication_context
            else None
        ),
        allow_started_first=publication_context is None,
    )
    if not structural.valid:
        raise DecisionAnalysisError(
            "FROZEN_PLAN_INTEGRITY_FAILED",
            "冻结排程未通过完整约束复核",
            violations=[item.model_dump(mode="json") for item in structural.violations],
        )
    return plan.scenario_snapshot.model_copy(deep=True)


def schedule_signature(schedule: ScheduleResult) -> str:
    """Hash every authoritative normalized schedule field used by analysis."""
    payload = schedule.model_dump(mode="json")
    for volatile in ("id", "created_at", "runtime_ms"):
        payload.pop(volatile, None)
    return content_hash(payload)


def _analysis_work_view(
    plan: PlanVersion,
    scenario: ScheduleScenario,
    provider: TravelTimeProvider,
) -> tuple[ScheduleScenario, ScheduleResult]:
    """Return the one declared work set used by cost, capacity, and risk."""
    publication_context = plan.publication_planning_context
    if plan.selected.kind != "replan" or publication_context is None:
        return scenario, plan.selected.model_copy(deep=True)
    frozen_ids = {item.work_order_id for item in publication_context.frozen_booking_identities}
    scoped_scenario = scenario.model_copy(deep=True)
    scoped_scenario.work_orders = [item for item in scoped_scenario.work_orders if item.id not in frozen_ids]
    scoped_scenario.locked_assignments = [
        item for item in scoped_scenario.locked_assignments if item.work_order_id not in frozen_ids
    ]
    entries = {item.technician_id: item for item in publication_context.route_entries}
    for technician in scoped_scenario.technicians:
        entry = entries.get(technician.id)
        if entry is not None:
            # KPI return travel uses start_location as the terminal point. The
            # first-leg travel remains frozen on each assignment and starts at
            # entry.location in the route verifier/builder.
            technician.start_location = entry.return_location
    scoped_schedule = plan.selected.model_copy(deep=True)
    scoped_schedule.assignments = [
        item.model_copy(deep=True) for item in scoped_schedule.assignments if item.work_order_id not in frozen_ids
    ]
    grouped: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in scoped_schedule.assignments:
        grouped[assignment.technician_id].append(assignment)
    for route in grouped.values():
        for sequence, assignment in enumerate(sorted(route, key=lambda item: item.sequence), start=1):
            assignment.sequence = sequence
    scoped_schedule.assignments.sort(key=lambda item: (item.technician_id, item.sequence))
    active_ids = {item.id for item in scoped_scenario.work_orders if item.status.value != "completed"}
    scoped_schedule.unassigned = [
        item.model_copy(deep=True) for item in scoped_schedule.unassigned if item.work_order_id in active_ids
    ]
    scoped_schedule.kpis = calculate_kpis(
        scoped_scenario,
        scoped_schedule.assignments,
        scoped_schedule.unassigned,
        provider=provider,
    )
    return scoped_scenario, scoped_schedule


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
            "publication_planning_context_hash": plan.publication_planning_context_hash,
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
    paid_shift_start_by_technician: dict[str, int] | None = None,
    paid_shift_default_start: int | None = None,
    paid_shift_only_if_scheduled: bool = False,
    technician_cost_sources: dict[str, TechnicianCostSource] | None = None,
    analysis_scope: DecisionAnalysisScope = DecisionAnalysisScope.frozen_full_plan,
) -> PlanCostBreakdown:
    """Calculate operating exposure using integer cents only.

    OCCUPIED_MINUTES already includes the overtime base wage in occupied
    minutes. PAID_SHIFT covers only the normal shift, so overtime receives
    both its base wage and the configured premium.
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
    regular_labor_cost = 0
    overtime_base_cost = 0
    overtime_premium_cost = 0
    full_day_committed_labor_cost = 0
    ledger_components: list[CostComponent] = []
    scheduled_technicians = {item.technician_id for item in schedule.assignments}
    for kpi in schedule.kpis.technician:
        technician = technicians.get(kpi.technician_id)
        if technician is None:
            continue
        paid_shift_mode = policy.labor_cost_mode is LaborCostMode.paid_shift
        source = (technician_cost_sources or {}).get(technician.id, TechnicianCostSource())
        full_shift_minutes = technician.shift_end - technician.shift_start
        if paid_shift_mode and source.include_regular_wage:
            full_day_committed_labor_cost += full_shift_minutes * technician.cost_per_minute_cents
        if paid_shift_mode and paid_shift_only_if_scheduled and technician.id not in scheduled_technicians:
            regular_minutes = 0
        elif paid_shift_mode and (paid_shift_start_by_technician is not None or paid_shift_default_start is not None):
            incremental_start = (paid_shift_start_by_technician or {}).get(
                technician.id,
                paid_shift_default_start if paid_shift_default_start is not None else technician.shift_start,
            )
            regular_minutes = max(0, technician.shift_end - max(technician.shift_start, incremental_start))
        else:
            regular_minutes = full_shift_minutes if paid_shift_mode else kpi.occupied_minutes
        regular_labor = regular_minutes * technician.cost_per_minute_cents if source.include_regular_wage else 0
        overtime_base = (
            kpi.overtime_minutes * technician.cost_per_minute_cents
            if paid_shift_mode and source.include_overtime_base
            else 0
        )
        overtime_premium = (
            kpi.overtime_minutes * technician.cost_per_minute_cents * policy.overtime_premium_basis_points // 10_000
            if source.include_overtime_premium
            else 0
        )
        technician_costs[technician.id] = regular_labor + overtime_base + overtime_premium
        regular_labor_cost += regular_labor
        overtime_base_cost += overtime_base
        overtime_premium_cost += overtime_premium
        for component, amount in (
            (CostComponentKind.regular_labor, regular_labor),
            (CostComponentKind.overtime_base, overtime_base),
            (CostComponentKind.overtime_premium, overtime_premium),
        ):
            if amount:
                ledger_components.append(
                    CostComponent(
                        component=component,
                        source_id=technician.id,
                        amount_cents=amount,
                        scope=analysis_scope,
                    )
                )

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
    for component, source_id, amount in (
        (CostComponentKind.travel, "TRAVEL_PROVIDER", travel_cost),
        (CostComponentKind.outsourcing, "DECISION_COST_POLICY", outsourcing_cost),
        (CostComponentKind.sla_penalty, "PUBLISHED_ASSIGNMENTS", sla_penalty),
        (CostComponentKind.unserved_revenue, "UNASSIGNED_ORDERS", unserved_revenue),
    ):
        if amount:
            ledger_components.append(
                CostComponent(
                    component=component,
                    source_id=source_id,
                    amount_cents=amount,
                    scope=analysis_scope,
                )
            )
    cash = regular_labor_cost + overtime_base_cost + overtime_premium_cost + travel_cost + outsourcing_cost
    service_failure_loss = sla_penalty + unserved_revenue
    total = cash + service_failure_loss
    return PlanCostBreakdown(
        regular_labor_cost_cents=regular_labor_cost,
        overtime_base_cost_cents=overtime_base_cost,
        overtime_premium_cost_cents=overtime_premium_cost,
        labor_cost_cents=regular_labor_cost,
        travel_cost_cents=travel_cost,
        overtime_cost_cents=overtime_premium_cost,
        sla_penalty_cents=sla_penalty,
        unserved_revenue_cents=unserved_revenue,
        outsourcing_cost_cents=outsourcing_cost,
        cash_operating_cost_cents=cash,
        service_failure_loss_cents=service_failure_loss,
        total_economic_impact_cents=total,
        total_cost_cents=total,
        technician_cost_cents=technician_costs,
        full_day_committed_labor_cost_cents=(
            full_day_committed_labor_cost if policy.labor_cost_mode is LaborCostMode.paid_shift else regular_labor_cost
        ),
        remaining_incremental_labor_cost_cents=regular_labor_cost,
        ledger=CostLedger(components=ledger_components),
    )


def cost_analysis(
    plan: PlanVersion,
    policy: DecisionCostPolicy | None = None,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
    horizon: AnalysisHorizon | None = None,
    expected_input_hash: str | None = None,
) -> CostAnalysis:
    context = context or default_analysis_context(
        DecisionAnalysisScope.publication_remaining_plan
        if plan.selected.kind == "replan" and plan.publication_planning_context is not None
        else DecisionAnalysisScope.frozen_full_plan
    )
    horizon = horizon or AnalysisHorizon()
    if context.analysis_scope is DecisionAnalysisScope.publication_remaining_plan and horizon.days != 1:
        raise DecisionAnalysisError(
            "REMAINING_PLAN_HORIZON_MUST_BE_ONE",
            "发布时剩余计划是一次性日内范围，分析周期必须为 1 个工作日",
            requested_days=horizon.days,
        )
    scenario = _validate_analysis_input(plan, provider, context, "COST")
    scenario, analysis_schedule = _analysis_work_view(plan, scenario, provider)
    policy = policy or DecisionCostPolicy()
    signature = schedule_signature(analysis_schedule)
    build_sha = decision_build_sha()
    input_hash = expected_input_hash or canonical_decision_input_hash(
        plan, "COST", {"policy": policy, "analysis_horizon": horizon}, context, provider
    )
    remaining_scope = context.analysis_scope is DecisionAnalysisScope.publication_remaining_plan
    entry_starts = (
        {item.technician_id: item.available_at for item in plan.publication_planning_context.route_entries}
        if remaining_scope and plan.publication_planning_context
        else None
    )
    breakdown = analyze_plan_cost(
        scenario,
        analysis_schedule,
        policy,
        paid_shift_start_by_technician=entry_starts,
        paid_shift_default_start=context.analysis_as_of_time if remaining_scope else None,
        paid_shift_only_if_scheduled=remaining_scope,
        analysis_scope=context.analysis_scope,
    )
    assumptions = [
        (
            "该重排记录从发布时路线入口分析剩余计划，不重复计入已开始或已完成的冻结服务。"
            if plan.selected.kind == "replan" and plan.publication_planning_context is not None
            else "该记录分析完整冻结计划，不混入查询时的执行事实。"
        ),
        (
            "付费班次另列今日完整承诺成本；正式增量结果只计算发布时点之后仍有任务的技师。"
            if policy.labor_cost_mode is LaborCostMode.paid_shift and remaining_scope
            else "人工成本按完整付费班次计。"
            if policy.labor_cost_mode is LaborCostMode.paid_shift
            else "人工成本按计划占用分钟计，包括服务、行程和等待。"
        ),
        (
            "付费班次模式将加班基础工资与加班溢价分开计算。"
            if policy.labor_cost_mode is LaborCostMode.paid_shift
            else "占用分钟模式的正常人工已含加班基础工资，只另计加班溢价。"
        ),
        "现金运营成本与 SLA/未服务经济损失分开列示；总经济影响不是财务结算。",
        (
            "发布时剩余计划只分析当日一次性范围，不做多日重复外推。"
            if remaining_scope
            else f"分析周期为 {horizon.days} 个工作日；每日计划假设在周期内重复。"
        ),
    ]
    if plan.selected.kind == "replan" and plan.publication_planning_context is None:
        assumptions.append(
            "LEGACY_REPLAN_CONTEXT_WARNING：旧重排版本缺少发布时路线起点；本成本分析只使用冻结排程工时。"
        )
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
        assumptions=assumptions,
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
        candidate_id = f"ANALYSIS-TECH-{content_hash({'scenario': scenario.id, 'archetype': archetype_payload})[:10]}"
        if candidate_id in {item.id for item in alternative.technicians}:
            candidate_id = f"ANALYSIS-TECH-{content_hash({'scenario': scenario.id, 'archetype': archetype_payload, 'existing': sorted(item.id for item in alternative.technicians)})[:16]}"
        alternative.technicians.append(
            Technician(
                id=candidate_id,
                **archetype_payload,
                color="#526b5d",
            )
        )
        changes: dict[str, object] = {
            "candidate_technician_id": candidate_id,
            "candidate_technician": archetype_payload,
            "affected_technician_ids": [candidate_id],
        }
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
            "affected_technician_ids": [str(target.id)],
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
        affected: list[str] = []
        for technician in alternative.technicians:
            extended = min(1800, technician.shift_end + 60)
            changed = changed or extended != technician.shift_end
            if extended != technician.shift_end:
                affected.append(technician.id)
            technician.shift_end = extended
        return (
            alternative,
            "所有技师班次结束时间延长 60 分钟，最高不超过次日 06:00。",
            changed,
            {"shift_extension_minutes": 60, "affected_technician_ids": affected},
        )

    if option_id == "allow_overtime":
        changed = False
        affected = []
        for technician in alternative.technicians:
            increased = min(240, technician.overtime_limit + 60)
            changed = changed or increased != technician.overtime_limit
            if increased != technician.overtime_limit:
                affected.append(technician.id)
            technician.overtime_limit = increased
        return (
            alternative,
            "所有技师允许的加班上限增加 60 分钟，最高为 240 分钟。",
            changed,
            {"overtime_limit_increase_minutes": 60, "affected_technician_ids": affected},
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
                "affected_technician_ids": [target.id],
                "previous_start_location": previous,
                "new_start_location": target.start_location.model_dump(mode="json"),
            },
        )

    return (
        alternative,
        "未服务工单由同日外部服务商承接，并假设在 SLA 内完成。",
        bool(base.unassigned),
        {
            "outsourced_work_order_ids": [item.work_order_id for item in base.unassigned],
            "affected_work_order_ids": [item.work_order_id for item in base.unassigned],
        },
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


def _capacity_cost_basis(
    cadence: CostCadence,
    changed_inputs: dict[str, object],
    *,
    applicable: bool,
) -> tuple[CostUnitType, int, list[str]]:
    technician_value = changed_inputs.get("affected_technician_ids", [])
    work_order_value = changed_inputs.get(
        "affected_work_order_ids",
        changed_inputs.get("targeted_work_order_ids", []),
    )
    technician_ids = [str(item) for item in technician_value] if isinstance(technician_value, list) else []
    work_order_ids = [str(item) for item in work_order_value] if isinstance(work_order_value, list) else []
    if cadence is CostCadence.one_time:
        affected = technician_ids or work_order_ids
        return CostUnitType.investment, 1, affected
    if cadence is CostCadence.per_day:
        return CostUnitType.plan_day, 1, technician_ids or work_order_ids
    if cadence is CostCadence.per_month:
        return CostUnitType.work_month, 1, technician_ids or work_order_ids
    if cadence is CostCadence.per_shift:
        if applicable and not technician_ids:
            raise DecisionAnalysisError(
                "CAPACITY_COST_UNITS_UNDEFINED",
                "按班次计费的容量选项没有可识别的受影响技师",
            )
        return CostUnitType.technician_shift, max(1, len(technician_ids)), technician_ids
    if applicable and not work_order_ids:
        raise DecisionAnalysisError(
            "CAPACITY_COST_UNITS_UNDEFINED",
            "按工单计费的容量选项没有可识别的受影响工单",
        )
    return CostUnitType.work_order, max(1, len(work_order_ids)), work_order_ids


def _horizon_cost(fixed_cost: int, cadence: CostCadence, horizon: AnalysisHorizon, units: int = 1) -> int:
    if cadence is CostCadence.one_time:
        return fixed_cost
    if cadence is CostCadence.per_day:
        return fixed_cost * horizon.days
    if cadence is CostCadence.per_shift:
        return fixed_cost * units * horizon.days
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
    *,
    route_entries: list[RouteEntryContext] | None = None,
) -> ScheduleResult:
    """Append only unserved work while retaining the real terminal depot."""
    result = selected.model_copy(deep=True)
    result.id = f"ANALYSIS-{schedule_signature(selected)[:12]}-{option_id}"
    technicians = {item.id: item for item in alternative.technicians}
    orders = {item.id: item for item in alternative.work_orders}
    entries = {item.technician_id: item for item in route_entries or []}
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
                entry = entries.get(technician.id)
                available_at = entry.available_at if entry else technician.shift_start
                current_location = entry.location if entry else technician.start_location
            travel = provider.minutes(current_location, order.location, available_at)
            arrival = available_at + travel
            start = max(arrival, order.window_start, order.reported_at or 0)
            finish = start + order.service_duration
            entry = entries.get(technician.id)
            terminal = entry.return_location if entry else technician.start_location
            return_to_terminal = provider.minutes(order.location, terminal, finish)
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
        entry = entries.get(technician.id)
        available_at = route[-1].finish_time if route else entry.available_at if entry else technician.shift_start
        origin = (
            orders[route[-1].work_order_id].location
            if route
            else entry.location
            if entry
            else technician.start_location
        )
        terminal = entry.return_location if entry else technician.start_location
        assignment = ScheduleAssignment(
            work_order_id=order.id,
            technician_id=technician.id,
            sequence=len(route) + 1,
            arrival_time=available_at + provider.minutes(origin, order.location, available_at),
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
                "route_entry_origin": origin.model_dump(mode="json"),
                "terminal_depot": terminal.model_dump(mode="json"),
                "return_travel_minutes": provider.minutes(order.location, terminal, finish),
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
    allow_started_first: bool = False,
    route_entries: list[RouteEntryContext] | None = None,
    frozen_work_order_ids: set[str] | None = None,
) -> CapacityVerificationReport:
    violations: list[CapacityViolation] = []
    technicians = {item.id: item for item in scenario.technicians}
    orders = {item.id: item for item in scenario.work_orders if item.status.value != "completed"}
    assigned_ids = [item.work_order_id for item in schedule.assignments]
    unassigned_ids = [item.work_order_id for item in schedule.unassigned]
    external_ids = externally_covered_work_order_ids or set()
    frozen_ids = frozen_work_order_ids or set()
    entries = {item.technician_id: item for item in route_entries or []}
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
        entry = entries.get(technician_id)
        available_at = technician.shift_start
        location = technician.start_location
        for route_index, assignment in enumerate(ordered):
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
            if entry and (
                assignment.work_order_id == entry.first_future_work_order_id
                or (route_index == 0 and entry.first_future_work_order_id is None)
            ):
                available_at = entry.available_at
                location = entry.location
            frozen_prefix = bool(
                entry
                and assignment.work_order_id in frozen_ids
                and assignment.work_order_id != entry.first_future_work_order_id
            )
            contextual_entry = frozen_prefix or (
                route_index == 0 and allow_started_first and order.status.value == "started" and not entry
            )
            travel = provider.minutes(location, order.location, available_at)
            arrival = available_at + travel
            if not contextual_entry and (assignment.travel_minutes != travel or assignment.arrival_time != arrival):
                violations.append(
                    CapacityViolation(
                        code="TRAVEL_DISCONTINUITY",
                        message="路线旅行或到达时间不连续",
                        work_order_id=order.id,
                        technician_id=technician_id,
                    )
                )
            if not contextual_entry and (
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
            if not frozen_prefix:
                available_at = assignment.finish_time
                location = order.location
        if ordered:
            return_location = entry.return_location if entry else technician.start_location
            return_minutes = provider.minutes(location, return_location, available_at)
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


def _capacity_route_diff(
    base: ScheduleResult,
    alternative: ScheduleResult,
    externally_covered: set[str],
) -> list[dict[str, object]]:
    before = {item.work_order_id: item for item in base.assignments}
    after = {item.work_order_id: item for item in alternative.assignments}
    changes: list[dict[str, object]] = []
    for work_order_id in sorted(set(before) | set(after) | externally_covered):
        previous = before.get(work_order_id)
        current = after.get(work_order_id)
        if work_order_id in externally_covered:
            changes.append({"work_order_id": work_order_id, "change": "OUTSOURCED"})
        elif previous is None and current is not None:
            changes.append(
                {
                    "work_order_id": work_order_id,
                    "change": "ASSIGNED",
                    "technician_id": current.technician_id,
                    "sequence": current.sequence,
                    "start_time": current.start_time,
                }
            )
        elif previous is not None and current is None:
            changes.append({"work_order_id": work_order_id, "change": "UNASSIGNED"})
        elif previous and current and previous.model_dump(mode="json") != current.model_dump(mode="json"):
            changes.append(
                {
                    "work_order_id": work_order_id,
                    "change": "UPDATED",
                    "before": previous.model_dump(mode="json"),
                    "after": current.model_dump(mode="json"),
                }
            )
    return changes


def capacity_analysis(
    plan: PlanVersion,
    request: CapacityAnalysisRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
    expected_input_hash: str | None = None,
) -> CapacityAnalysis:
    context = context or default_analysis_context(
        request.analysis_scope
        or (
            DecisionAnalysisScope.publication_remaining_plan
            if plan.selected.kind == "replan" and plan.publication_planning_context is not None
            else DecisionAnalysisScope.frozen_full_plan
        )
    )
    if (
        context.analysis_scope is DecisionAnalysisScope.publication_remaining_plan
        and request.analysis_horizon.days != 1
    ):
        raise DecisionAnalysisError(
            "REMAINING_PLAN_HORIZON_MUST_BE_ONE",
            "发布时剩余计划的容量成本是一次性日内范围，分析周期必须为 1 个工作日",
            requested_days=request.analysis_horizon.days,
        )
    scenario = _validate_analysis_input(plan, provider, context, "CAPACITY")
    scenario, analysis_schedule = _analysis_work_view(plan, scenario, provider)
    if plan.selected.kind == "replan" and request.reference_mode is CapacityReferenceMode.controlled_reoptimization:
        raise DecisionAnalysisError(
            "REPLAN_CONTROLLED_REOPTIMIZATION_NOT_SUPPORTED",
            "重排方案缺少可证明等价的受控再优化模型；请选择已选方案增量模式",
        )
    policy = request.cost_policy
    remaining_scope = context.analysis_scope is DecisionAnalysisScope.publication_remaining_plan
    paid_shift_kwargs = {
        "paid_shift_start_by_technician": (
            {item.technician_id: item.available_at for item in plan.publication_planning_context.route_entries}
            if remaining_scope and plan.publication_planning_context
            else None
        ),
        "paid_shift_default_start": context.analysis_as_of_time if remaining_scope else None,
        "paid_shift_only_if_scheduled": remaining_scope,
    }
    selected_options = tuple(request.option_ids) if request.option_ids else CAPACITY_OPTIONS
    selected_signature = schedule_signature(analysis_schedule)
    if request.reference_mode is CapacityReferenceMode.selected_plan_delta:
        base = analysis_schedule.model_copy(deep=True)
        evaluation_method = "ROUTE_ENTRY_TAIL_APPEND_COUNTERFACTUAL_V4"
        reference_policy_fingerprint = plan.selected.solver_policy.fingerprint if plan.selected.solver_policy else ""
    else:
        base = baseline_schedule(scenario, 0, strategy="baseline", provider=provider)
        evaluation_method = "CONTROLLED_DETERMINISTIC_GREEDY_REOPTIMIZATION_V2"
        reference_policy_fingerprint = base.solver_policy.fingerprint if base.solver_policy else ""
    base_cost = analyze_plan_cost(
        scenario,
        base,
        policy,
        **paid_shift_kwargs,
        analysis_scope=context.analysis_scope,
    )
    build_sha = decision_build_sha()
    analysis_input_hash = expected_input_hash or canonical_decision_input_hash(
        plan, "CAPACITY", request, context, provider
    )
    options: list[CapacityOptionResult] = []
    active_orders = [item for item in scenario.work_orders if item.status.value != "completed"]

    for option_id in selected_options:
        alternative, assumption, applicable, changed_inputs = _capacity_scenario(scenario, option_id, base, request)
        if (
            option_id == "relocate_one_technician_start"
            and plan.selected.kind == "replan"
            and plan.publication_planning_context is not None
        ):
            applicable = False
            assumption = "重排方案的路线入口已经由发布上下文冻结，不能用出发点迁移改写历史入口。"
            changed_inputs = {}
        try:
            alternative = ScheduleScenario.model_validate(alternative.model_dump(mode="json"))
        except ValueError as error:
            raise DecisionAnalysisError(
                "CAPACITY_SCENARIO_INVALID",
                "容量选项产生了无效的场景输入",
                option_id=option_id,
                validation_error=str(error),
            ) from error
        fixed_cost = _capacity_fixed_cost(request, option_id)
        cadence = _capacity_cost_cadence(request, option_id)
        cost_unit_type, cost_units, affected_entity_ids = _capacity_cost_basis(
            cadence,
            changed_inputs,
            applicable=applicable,
        )
        outsourced = 0
        external_assignments: list[ExternalAssignment] = []
        if option_id == "outsource_unserved":
            outsourced = len(base.unassigned)
            alternative_schedule = base.model_copy(deep=True)
            external_ids = {item.work_order_id for item in base.unassigned}
            alternative_schedule.unassigned = []
            alternative_schedule.kpis = calculate_kpis(
                scenario,
                alternative_schedule.assignments,
                alternative_schedule.unassigned,
                provider=provider,
            )
            use_cost_policy = request.capacity_policy.outsource_cost_source is CapacityCostSource.cost_policy
            alternative_cost = analyze_plan_cost(
                scenario,
                base,
                policy,
                outsourced_orders=outsourced if use_cost_policy else 0,
                **paid_shift_kwargs,
                analysis_scope=context.analysis_scope,
            )
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
            external_assignments = [
                ExternalAssignment(
                    work_order_id=work_order_id,
                    cost_cents=(
                        policy.outsourcing_cost_per_order_cents
                        if use_cost_policy
                        else fixed_cost
                        if cadence is CostCadence.per_order
                        else 0
                    ),
                )
                for work_order_id in sorted(external_ids)
            ]
            changed_inputs.update(
                {
                    "outsourcing_cost_source": request.capacity_policy.outsource_cost_source.value,
                    "external_service_assumption": "SAME_DAY_WITHIN_SLA",
                    "external_assignment_count": len(external_assignments),
                }
            )
        else:
            external_ids = set()
            if not applicable:
                alternative_schedule = base.model_copy(deep=True)
            elif request.reference_mode is CapacityReferenceMode.selected_plan_delta:
                alternative_schedule = _tail_append_counterfactual(
                    alternative,
                    base,
                    option_id,
                    provider,
                    route_entries=(
                        plan.publication_planning_context.route_entries if plan.publication_planning_context else None
                    ),
                )
            else:
                alternative_schedule = baseline_schedule(alternative, 0, strategy="baseline", provider=provider)
                option_policy = alternative_schedule.solver_policy
                if not option_policy or option_policy.fingerprint != reference_policy_fingerprint:
                    raise DecisionAnalysisError(
                        "CONTROLLED_POLICY_DRIFT",
                        "容量选项没有使用与参考排程相同的求解政策",
                        option_id=option_id,
                    )
            affected_value = changed_inputs.get("affected_technician_ids", [])
            affected_ids = affected_value if isinstance(affected_value, list) else []
            candidate_id = next(
                (str(item) for item in affected_ids if str(item) not in {tech.id for tech in scenario.technicians}),
                None,
            )
            cost_sources = (
                {
                    candidate_id: TechnicianCostSource(
                        include_regular_wage=False,
                        include_overtime_base=False,
                        include_overtime_premium=False,
                        source="CAPACITY_FIXED_ONLY",
                    )
                }
                if option_id == "add_technician"
                and candidate_id
                and request.capacity_policy.add_technician_cost_mode.value == "FIXED_ONLY"
                else None
            )
            alternative_cost = analyze_plan_cost(
                alternative,
                alternative_schedule,
                policy,
                **paid_shift_kwargs,
                technician_cost_sources=cost_sources,
                analysis_scope=context.analysis_scope,
            )
            daily_alternative_total = alternative_cost.total_economic_impact_cents
            completion_rate = alternative_schedule.kpis.completion_rate
            sla_rate = alternative_schedule.kpis.committed_on_time_rate
            unassigned_count = alternative_schedule.kpis.unassigned_count
            travel_minutes = alternative_schedule.kpis.total_travel_minutes
            overtime_minutes = alternative_schedule.kpis.total_overtime_minutes
            signature = schedule_signature(alternative_schedule)
        if option_id == "add_technician":
            changed_inputs["cost_mode"] = request.capacity_policy.add_technician_cost_mode.value
            changed_inputs["wage_source"] = "DecisionCostPolicy + TechnicianArchetype.cost_per_minute_cents"
            changed_inputs["fixed_cost_source"] = "CapacityPolicy.add_technician_fixed_cost_cents"
        alternative_schedule.id = f"CF-{plan.id}-{option_id}"
        alternative_schedule.created_at = plan.created_at
        alternative_schedule.runtime_ms = 0
        verification = verify_counterfactual_schedule(
            alternative,
            alternative_schedule,
            provider,
            fixed_schedule=base if request.reference_mode is CapacityReferenceMode.selected_plan_delta else None,
            externally_covered_work_order_ids=external_ids,
            route_entries=(
                plan.publication_planning_context.route_entries
                if plan.publication_planning_context
                and request.reference_mode is CapacityReferenceMode.selected_plan_delta
                else None
            ),
            frozen_work_order_ids=(
                {item.work_order_id for item in plan.publication_planning_context.frozen_booking_identities}
                if plan.publication_planning_context
                and request.reference_mode is CapacityReferenceMode.selected_plan_delta
                else None
            ),
        )
        violations = list(verification.violations)
        if not applicable:
            violations.insert(0, CapacityViolation(code="OPTION_NOT_APPLICABLE", message=assumption))
        external_conditional = option_id == "outsource_unserved" and any(
            not item.capacity_verified for item in external_assignments
        )
        if external_conditional:
            violations.insert(
                0,
                CapacityViolation(
                    code="EXTERNAL_CAPACITY_UNVERIFIED",
                    message="供应商尚未确认容量、技能和 SLA；以下数字仅为全部接受时的条件上界。",
                ),
            )
            changed_inputs["decision_status"] = CapacityDecisionStatus.external_conditional.value
        decision_valid = applicable and verification.valid and not external_conditional
        decision_status = (
            CapacityDecisionStatus.not_applicable
            if not applicable
            else CapacityDecisionStatus.external_conditional
            if external_conditional
            else CapacityDecisionStatus.internal_verified
            if verification.valid
            else CapacityDecisionStatus.infeasible
        )
        daily_operating_delta = daily_alternative_total - base_cost.total_economic_impact_cents
        charged_fixed_cost = fixed_cost if applicable else 0
        if option_id == "outsource_unserved" and (
            request.capacity_policy.outsource_cost_source is CapacityCostSource.cost_policy
        ):
            charged_fixed_cost = 0
        if option_id == "add_technician" and request.capacity_policy.add_technician_cost_mode.value == "WAGE_ONLY":
            charged_fixed_cost = 0
        horizon_fixed_cost = _horizon_cost(
            charged_fixed_cost,
            cadence,
            request.analysis_horizon,
            cost_units,
        )
        daily_equivalent_fixed = _daily_equivalent_cost(
            charged_fixed_cost,
            cadence,
            request.analysis_horizon,
            cost_units,
        )
        one_time_investment = charged_fixed_cost if cadence is CostCadence.one_time else 0
        daily_benefit = max(0, -daily_operating_delta)
        economic_impact_offset_days = (
            round(one_time_investment / daily_benefit, 2) if one_time_investment and daily_benefit else None
        )
        daily_cash_delta = alternative_cost.cash_operating_cost_cents - base_cost.cash_operating_cost_cents
        cash_benefit = max(0, -daily_cash_delta)
        cash_payback_days = (
            round(one_time_investment / cash_benefit, 2) if one_time_investment and cash_benefit else None
        )
        horizon_total_impact = daily_operating_delta * request.analysis_horizon.days + horizon_fixed_cost
        marginal_daily_impact = daily_operating_delta + daily_equivalent_fixed
        projected_total = max(0, base_cost.total_economic_impact_cents + marginal_daily_impact)
        diagnostic_metrics: dict[str, float | int] = {
            "completion_rate": round(completion_rate, 4),
            "sla_on_time_rate": round(sla_rate, 4),
            "unassigned_count": unassigned_count,
            "travel_minutes": travel_minutes,
            "overtime_minutes": overtime_minutes,
            "completion_improvement_percentage_points": round(
                (completion_rate - base.kpis.completion_rate) * 100,
                2,
            ),
            "sla_improvement_percentage_points": round(
                (sla_rate - base.kpis.committed_on_time_rate) * 100,
                2,
            ),
            "unassigned_delta": unassigned_count - base.kpis.unassigned_count,
            "travel_delta_minutes": travel_minutes - base.kpis.total_travel_minutes,
            "overtime_delta_minutes": overtime_minutes - base.kpis.total_overtime_minutes,
            "daily_operating_delta_cents": daily_operating_delta,
            "horizon_total_impact_cents": horizon_total_impact,
        }
        assigned_by_id = {item.work_order_id: item for item in alternative_schedule.assignments}
        unserved_ids = {item.work_order_id for item in alternative_schedule.unassigned}
        dispositions: list[WorkOrderDisposition] = []
        for order in sorted(active_orders, key=lambda item: item.id):
            assignment = assigned_by_id.get(order.id)
            if assignment:
                dispositions.append(
                    WorkOrderDisposition(
                        work_order_id=order.id,
                        disposition="INTERNAL",
                        technician_id=assignment.technician_id,
                    )
                )
            elif order.id in external_ids:
                dispositions.append(
                    WorkOrderDisposition(
                        work_order_id=order.id,
                        disposition="EXTERNAL",
                        external_provider_id="EXTERNAL-PROVIDER",
                    )
                )
            elif order.id in unserved_ids:
                dispositions.append(WorkOrderDisposition(work_order_id=order.id, disposition="UNSERVED"))
        counterfactual_kpis = CapacityCounterfactualKPI(
            active_work_order_count=len(active_orders),
            internal_assignment_count=len(assigned_by_id),
            external_assignment_count=len(external_ids),
            unserved_count=unassigned_count,
            completion_rate=round(completion_rate, 4),
            sla_on_time_rate=round(sla_rate, 4),
            travel_minutes=travel_minutes,
            overtime_minutes=overtime_minutes,
        )
        options.append(
            CapacityOptionResult(
                option_id=option_id,
                name=CAPACITY_NAMES[option_id],
                assumption=assumption,
                option_applicable=applicable,
                schedule_feasible=verification.valid and not external_conditional,
                violations=violations,
                changed_inputs=changed_inputs,
                placement_mode=request.placement_mode,
                feasible=decision_valid,
                decision_status=decision_status,
                completion_rate=round(completion_rate, 4) if decision_valid else None,
                sla_on_time_rate=round(sla_rate, 4) if decision_valid else None,
                unassigned_count=unassigned_count if decision_valid else None,
                travel_minutes=travel_minutes if decision_valid else None,
                overtime_minutes=overtime_minutes if decision_valid else None,
                completion_improvement_percentage_points=(
                    diagnostic_metrics["completion_improvement_percentage_points"] if decision_valid else None
                ),
                sla_improvement_percentage_points=(
                    diagnostic_metrics["sla_improvement_percentage_points"] if decision_valid else None
                ),
                unassigned_delta=int(diagnostic_metrics["unassigned_delta"]) if decision_valid else None,
                travel_delta_minutes=(int(diagnostic_metrics["travel_delta_minutes"]) if decision_valid else None),
                overtime_delta_minutes=(int(diagnostic_metrics["overtime_delta_minutes"]) if decision_valid else None),
                fixed_capacity_cost_cents=charged_fixed_cost,
                fixed_cost_cadence=cadence,
                cost_unit_type=cost_unit_type,
                cost_units_per_day=cost_units,
                affected_entity_ids=affected_entity_ids,
                one_time_investment_cents=one_time_investment,
                daily_operating_delta_cents=daily_operating_delta if decision_valid else None,
                horizon_total_impact_cents=horizon_total_impact if decision_valid else None,
                economic_impact_offset_days=economic_impact_offset_days if decision_valid else None,
                cash_payback_days=cash_payback_days if decision_valid else None,
                break_even_days=economic_impact_offset_days if decision_valid else None,
                marginal_cost_cents=marginal_daily_impact if decision_valid else None,
                projected_total_cost_cents=projected_total if decision_valid else None,
                schedule_signature=signature,
                diagnostic_metrics=diagnostic_metrics,
                diagnostic_schedule=alternative_schedule,
                verification_report=verification,
                route_diff=_capacity_route_diff(base, alternative_schedule, external_ids),
                external_assignments=external_assignments,
                work_order_dispositions=dispositions,
                counterfactual_kpis=counterfactual_kpis if decision_valid else None,
                conditional_upper_bound_kpis=counterfactual_kpis if external_conditional else None,
                counterfactual_cost=alternative_cost if decision_valid else None,
                diagnostic_cost=alternative_cost,
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


def _keyed_draw(seed: int, trial: int, event_type: str, *entity_ids: object, modulo: int) -> int:
    if modulo <= 0:
        return 0
    digest = content_hash(
        {
            "version": "FIELD_SERVICE_KEYED_RANDOM_V1",
            "seed": seed,
            "trial": trial,
            "event_type": event_type,
            "entity_ids": entity_ids,
        }
    )
    return int(digest[:16], 16) % modulo


def build_simulation_scenario_set(
    scenario: ScheduleScenario,
    request: RiskSimulationRequest,
    seed: int,
    analysis_as_of_time: int = 0,
    active_work_order_ids: list[str] | None = None,
) -> dict[str, object]:
    technicians = {item.id: item for item in scenario.technicians}
    orders = sorted(scenario.work_orders, key=lambda item: item.id)
    if request.emergency_location_policy is EmergencyLocationPolicy.external_empirical_distribution:
        raise DecisionAnalysisError(
            "EXTERNAL_EMERGENCY_LOCATION_DISTRIBUTION_REQUIRED",
            "外部经验位置分布尚未配置，请选择当前需求、冻结位置代理或均匀服务区",
        )
    active_ids = set(active_work_order_ids or [])
    if request.emergency_location_policy is EmergencyLocationPolicy.active_demand_locations:
        location_orders = [item for item in orders if item.id in active_ids]
    elif request.emergency_location_policy is EmergencyLocationPolicy.all_frozen_locations_as_spatial_proxy:
        location_orders = orders
    else:
        location_orders = []
    service_area_points = [item.location for item in orders] + [item.start_location for item in technicians.values()]
    min_x = min((item.x for item in service_area_points), default=0)
    max_x = max((item.x for item in service_area_points), default=100)
    min_y = min((item.y for item in service_area_points), default=0)
    max_y = max((item.y for item in service_area_points), default=100)
    technician_ids = sorted(technicians)
    skills = sorted(
        {skill for technician in technicians.values() for skill in technician.skills}, key=lambda x: x.value
    )
    earliest_event = max(
        analysis_as_of_time,
        min((item.shift_start for item in technicians.values()), default=analysis_as_of_time),
    )
    latest_event = max((item.shift_end for item in technicians.values()), default=earliest_event)
    emergency_events: list[SimulationEmergencyEvent] = []
    for trial in range(request.trials):
        if (
            not technician_ids
            or not skills
            or earliest_event >= latest_event
            or (_keyed_draw(seed, trial, "emergency_event", modulo=10_000) >= request.emergency_order_basis_points)
        ):
            continue
        event_span = latest_event - earliest_event
        event_time = earliest_event + _keyed_draw(
            seed,
            trial,
            "emergency_time",
            modulo=event_span,
        )
        if location_orders:
            target_order = location_orders[_keyed_draw(seed, trial, "emergency_location", modulo=len(location_orders))]
            location = target_order.location
        elif request.emergency_location_policy is EmergencyLocationPolicy.uniform_service_area:
            x_span = max(1, int(round((max_x - min_x) * 1000)) + 1)
            y_span = max(1, int(round((max_y - min_y) * 1000)) + 1)
            location = Point(
                x=min_x + _keyed_draw(seed, trial, "emergency_location_x", modulo=x_span) / 1000,
                y=min_y + _keyed_draw(seed, trial, "emergency_location_y", modulo=y_span) / 1000,
            )
        else:
            technician = technicians[
                technician_ids[_keyed_draw(seed, trial, "emergency_location", modulo=len(technician_ids))]
            ]
            location = technician.start_location
        required_skill = skills[_keyed_draw(seed, trial, "emergency_skill", modulo=len(skills))]
        duration = 30 + _keyed_draw(seed, trial, "emergency_duration", modulo=61)
        emergency_events.append(
            SimulationEmergencyEvent(
                trial=trial,
                event_id=f"EMG-{trial}",
                event_time=event_time,
                location=location,
                duration_minutes=duration,
                required_skill=required_skill,
                sla_deadline=min(2760, event_time + 120),
            )
        )
    return {
        "policy_version": "FIELD_SERVICE_SIMULATION_SCENARIOS_V6",
        "keyed_random_version": "FIELD_SERVICE_KEYED_RANDOM_V1",
        "emergency_dispatch_policy": request.emergency_dispatch_policy.value,
        "emergency_responder_selection_policy": request.emergency_responder_selection_policy.value,
        "emergency_location_policy": request.emergency_location_policy.value,
        "emergency_location_work_order_ids": [item.id for item in location_orders],
        "scenario_snapshot_hash": content_hash(scenario),
        "seed": seed,
        "trials": request.trials,
        "analysis_as_of_time": analysis_as_of_time,
        "travel_delay_max_percent": request.travel_delay_max_percent,
        "service_duration_jitter_percent": request.service_duration_jitter_percent,
        "technician_absence_basis_points": request.technician_absence_basis_points,
        "emergency_order_basis_points": request.emergency_order_basis_points,
        "customer_no_show_basis_points": request.customer_no_show_basis_points,
        "technician_ids": technician_ids,
        "work_order_ids": [item.id for item in orders],
        "emergency_events": [item.model_dump(mode="json") for item in emergency_events],
    }


def simulate_plan_risk(
    plan: PlanVersion,
    request: RiskSimulationRequest,
    provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    *,
    context: DecisionAnalysisContext | None = None,
    expected_input_hash: str | None = None,
) -> RiskSimulationResult:
    context = context or default_analysis_context(
        request.analysis_scope
        or (
            DecisionAnalysisScope.publication_remaining_plan
            if plan.selected.kind == "replan" and plan.publication_planning_context is not None
            else DecisionAnalysisScope.frozen_full_plan
        )
    )
    frozen_scenario = _validate_analysis_input(plan, provider, context, "RISK")
    scenario, schedule = _analysis_work_view(plan, frozen_scenario, provider)
    seed = frozen_scenario.seed if request.seed is None else request.seed
    orders = {item.id: item for item in scenario.work_orders}
    technicians = {item.id: item for item in scenario.technicians}
    scenario_set_manifest = build_simulation_scenario_set(
        frozen_scenario,
        request,
        seed,
        context.analysis_as_of_time or 0,
        active_work_order_ids=sorted(orders),
    )
    event_payload = scenario_set_manifest["emergency_events"]
    emergency_events_by_trial = (
        {item.trial: item for item in (SimulationEmergencyEvent.model_validate(event) for event in event_payload)}
        if isinstance(event_payload, list)
        else {}
    )
    routes: dict[str, list[ScheduleAssignment]] = defaultdict(list)
    publication_context = plan.publication_planning_context
    route_entries = (
        {item.technician_id: item for item in publication_context.route_entries} if publication_context else {}
    )
    for assignment in schedule.assignments:
        routes[assignment.technician_id].append(assignment)
    for route in routes.values():
        route.sort(key=lambda item: item.sequence)
    active_count = sum(len(route) for route in routes.values()) + len(schedule.unassigned)
    initially_unserved = len(schedule.unassigned)
    sla_rates: list[float] = []
    all_demand_sla_rates: list[float] = []
    all_demand_late_totals: list[int] = []
    published_late_totals: list[int] = []
    emergency_late_totals: list[int] = []
    overtime_totals: list[int] = []
    unserved_totals: list[int] = []
    failed_trials = 0
    absence_event_trials = 0
    absence_caused_failure_trials = 0
    absence_caused_unserved_trials = 0
    absence_caused_sla_trials = 0
    absence_caused_overtime_trials = 0
    no_show_trials = 0
    window_failure_trials = 0
    overtime_failure_trials = 0
    emergency_event_trials = 0
    emergency_caused_failure_trials = 0
    emergency_caused_window_trials = 0
    emergency_caused_overtime_trials = 0
    emergency_caused_unserved_trials = 0
    emergency_caused_sla_trials = 0
    emergency_completed_trials = 0
    emergency_on_time_trials = 0
    emergency_incremental_late_totals: list[int] = []
    emergency_incremental_overtime_totals: list[int] = []
    emergency_incremental_unserved_totals: list[int] = []
    emergency_affected_order_totals: list[int] = []
    emergency_disposition_changed_totals: list[int] = []
    emergency_newly_unserved_totals: list[int] = []
    emergency_newly_late_totals: list[int] = []
    emergency_lateness_increased_totals: list[int] = []
    trial_metrics: list[RiskTrialMetric] = []

    def trial_outcome(
        trial: int,
        absent: set[str],
        emergency_event: SimulationEmergencyEvent | None,
    ) -> _RiskTrialOutcome:
        if request.emergency_dispatch_policy is not EmergencyDispatchPolicy.between_visits_only:
            raise DecisionAnalysisError(
                "UNSUPPORTED_EMERGENCY_DISPATCH_POLICY",
                "当前版本只支持在两次服务之间调度紧急工单",
            )
        if (
            request.emergency_responder_selection_policy
            is not EmergencyResponderSelectionPolicy.myopic_earliest_emergency_finish
        ):
            raise DecisionAnalysisError(
                "UNSUPPORTED_EMERGENCY_RESPONDER_SELECTION_POLICY",
                "当前版本只支持基于事件时点信息的紧急服务最早预计完成策略",
            )

        def new_state(technician_id: str) -> _RiskRouteState:
            technician = technicians[technician_id]
            route_entry = route_entries.get(technician_id)
            location = route_entry.location if route_entry else technician.start_location
            return _RiskRouteState(
                technician_id=technician_id,
                current=route_entry.available_at if route_entry else technician.shift_start,
                location=location,
                predecessor_id=(
                    f"ENTRY:{technician_id}:{location.x}:{location.y}" if route_entry else f"DEPOT:{technician_id}"
                ),
            )

        def order_timing(
            state: _RiskRouteState,
            assignment: ScheduleAssignment,
        ) -> tuple[int, int, int, bool]:
            order = orders[assignment.work_order_id]
            if state.ready_assignment_index == state.next_assignment_index:
                arrival = state.current
            else:
                delay_percent = _keyed_draw(
                    seed,
                    trial,
                    "travel",
                    state.predecessor_id,
                    order.id,
                    modulo=request.travel_delay_max_percent + 1,
                )
                planned_travel = provider.minutes(state.location, order.location, state.current)
                arrival = state.current + (planned_travel * (100 + delay_percent) + 99) // 100
            start = max(arrival, order.window_start, order.reported_at or 0)
            if request.execution_policy is RiskExecutionPolicy.follow_published_schedule:
                start = max(start, assignment.start_time)
            no_show = (
                _keyed_draw(seed, trial, "no_show", order.id, modulo=10_000) < request.customer_no_show_basis_points
            )
            if no_show:
                finish = start + 10
            else:
                jitter = request.service_duration_jitter_percent
                service_percent = (
                    100
                    - jitter
                    + _keyed_draw(
                        seed,
                        trial,
                        "service_duration",
                        order.id,
                        modulo=2 * jitter + 1,
                    )
                )
                finish = start + max(1, (order.service_duration * service_percent + 50) // 100)
            return arrival, start, finish, no_show

        def complete_assignment(
            state: _RiskRouteState,
            assignment: ScheduleAssignment,
            start: int,
            finish: int,
            no_show: bool,
        ) -> None:
            order = orders[assignment.work_order_id]
            state.ready_assignment_index = None
            state.current = finish
            state.location = order.location
            state.predecessor_id = order.id
            state.next_assignment_index += 1
            if no_show:
                state.unserved += 1
                state.no_show_failure = True
                state.work_order_late_minutes[order.id] = None
                state.work_order_outcomes[order.id] = SimulatedWorkOrderOutcome(
                    work_order_id=order.id,
                    disposition="NO_SHOW_UNSERVED",
                    technician_id=state.technician_id,
                )
                return
            late = max(0, finish - order.sla_deadline)
            state.total_late += late
            state.on_time += int(late == 0)
            state.work_order_late_minutes[order.id] = late
            state.work_order_outcomes[order.id] = SimulatedWorkOrderOutcome(
                work_order_id=order.id,
                disposition="ON_TIME" if late == 0 else "LATE",
                late_minutes=late,
                technician_id=state.technician_id,
            )
            state.window_failure = state.window_failure or start > order.window_end

        def finish_route(state: _RiskRouteState) -> None:
            route = routes.get(state.technician_id, [])
            while state.next_assignment_index < len(route):
                assignment = route[state.next_assignment_index]
                _arrival, start, finish, no_show = order_timing(state, assignment)
                complete_assignment(state, assignment, start, finish, no_show)
            if state.returned or (not route and not state.emergency_completed):
                return
            technician = technicians[state.technician_id]
            route_entry = route_entries.get(state.technician_id)
            return_location = route_entry.return_location if route_entry else technician.start_location
            return_minutes = provider.minutes(state.location, return_location, state.current)
            delay_percent = _keyed_draw(
                seed,
                trial,
                "return_travel",
                state.predecessor_id,
                f"DEPOT:{state.technician_id}",
                modulo=request.travel_delay_max_percent + 1,
            )
            state.current += (return_minutes * (100 + delay_percent) + 99) // 100
            state.location = return_location
            state.predecessor_id = f"DEPOT:{state.technician_id}"
            state.returned = True
            state.total_overtime = max(0, state.current - technician.shift_end)
            state.overtime_failure = state.total_overtime > technician.overtime_limit

        def advance_to_dispatch_checkpoint(
            state: _RiskRouteState,
            event: SimulationEmergencyEvent,
        ) -> None:
            """Advance the authoritative timeline without preempting travel or service."""
            route = routes.get(state.technician_id, [])
            while state.next_assignment_index < len(route):
                if event.event_time <= state.current:
                    return
                assignment = route[state.next_assignment_index]
                order = orders[assignment.work_order_id]
                arrival, start, finish, no_show = order_timing(state, assignment)
                if event.event_time <= arrival:
                    # No mid-travel diversion: arrive at the customer first.
                    state.current = arrival
                    state.location = order.location
                    state.predecessor_id = order.id
                    state.ready_assignment_index = state.next_assignment_index
                    return
                if event.event_time < start:
                    # Waiting at the next customer is a between-visits checkpoint.
                    state.current = event.event_time
                    state.location = order.location
                    state.predecessor_id = order.id
                    state.ready_assignment_index = state.next_assignment_index
                    return
                complete_assignment(state, assignment, start, finish, no_show)
                if event.event_time <= finish:
                    # Finish an active visit before dispatching the emergency.
                    return
            if event.event_time <= state.current:
                return
            technician = technicians[state.technician_id]
            route_entry = route_entries.get(state.technician_id)
            return_location = route_entry.return_location if route_entry else technician.start_location
            return_minutes = provider.minutes(state.location, return_location, state.current)
            delay_percent = _keyed_draw(
                seed,
                trial,
                "return_travel",
                state.predecessor_id,
                f"DEPOT:{state.technician_id}",
                modulo=request.travel_delay_max_percent + 1,
            )
            return_finish = state.current + (return_minutes * (100 + delay_percent) + 99) // 100
            route_return_finish = return_finish
            if event.event_time <= return_finish:
                # No mid-return diversion: reach the route terminal first.
                state.current = return_finish
            else:
                state.current = event.event_time
            state.location = return_location
            state.predecessor_id = f"DEPOT:{state.technician_id}"
            state.returned = True
            state.total_overtime = max(0, route_return_finish - technician.shift_end)
            state.overtime_failure = state.total_overtime > technician.overtime_limit

        def apply_emergency(state: _RiskRouteState, event: SimulationEmergencyEvent) -> int:
            depart_at = max(state.current, event.event_time)
            state.emergency_dispatch_time = depart_at
            state.emergency_dispatch_location = state.location.model_copy(deep=True)
            planned_travel = provider.minutes(state.location, event.location, depart_at)
            delay_percent = _keyed_draw(
                seed,
                trial,
                "emergency_travel",
                state.predecessor_id,
                event.event_id,
                modulo=request.travel_delay_max_percent + 1,
            )
            travel = (planned_travel * (100 + delay_percent) + 99) // 100
            state.current = depart_at + travel + event.duration_minutes
            state.emergency_finish_time = state.current
            state.emergency_late = max(0, state.current - event.sla_deadline)
            state.emergency_completed = True
            state.emergency_on_time = state.current <= event.sla_deadline
            state.location = event.location
            state.predecessor_id = f"EMERGENCY:{trial}:{state.technician_id}"
            state.ready_assignment_index = None
            state.returned = False
            return state.current

        def event_information_projection(event: SimulationEmergencyEvent, technician_id: str) -> _RiskRouteState:
            """Project the next dispatch checkpoint without exposing post-event outcomes."""
            state = new_state(technician_id)
            route = routes.get(technician_id, [])
            while state.next_assignment_index < len(route):
                if event.event_time <= state.current:
                    return state
                assignment = route[state.next_assignment_index]
                order = orders[assignment.work_order_id]
                arrival, start, finish, no_show = order_timing(state, assignment)
                if finish <= event.event_time:
                    # This outcome is history at the decision time and may be observed.
                    complete_assignment(state, assignment, start, finish, no_show)
                    continue
                if event.event_time <= arrival:
                    # The technician is still travelling. Only the fact that arrival has
                    # not happened is known; use the deterministic travel projection.
                    planned_arrival = state.current + provider.minutes(state.location, order.location, state.current)
                    state.current = max(event.event_time + 1, planned_arrival)
                    state.location = order.location
                    state.predecessor_id = order.id
                    state.ready_assignment_index = state.next_assignment_index
                    return state
                if event.event_time < start:
                    # Waiting at the customer is already a between-visits checkpoint.
                    state.current = event.event_time
                    state.location = order.location
                    state.predecessor_id = order.id
                    state.ready_assignment_index = state.next_assignment_index
                    return state
                # Service is observed to be in progress, but its eventual random finish
                # is not. Estimate only the deterministic remaining service duration.
                deterministic_remaining = max(1, order.service_duration - (event.event_time - start))
                state.current = event.event_time + deterministic_remaining
                state.location = order.location
                state.predecessor_id = order.id
                state.next_assignment_index += 1
                state.ready_assignment_index = None
                return state

            technician = technicians[technician_id]
            route_entry = route_entries.get(technician_id)
            return_location = route_entry.return_location if route_entry else technician.start_location
            if state.location != return_location:
                planned_return = state.current + provider.minutes(state.location, return_location, state.current)
                state.current = max(event.event_time + 1, planned_return)
                state.location = return_location
                state.predecessor_id = f"DEPOT:{technician_id}"
                state.returned = True
            else:
                state.current = max(state.current, event.event_time)
            return state

        def deterministic_candidate_projection(
            state: _RiskRouteState,
            event: SimulationEmergencyEvent,
        ) -> tuple[int, int]:
            """Estimate eligibility without reading any post-decision random draw."""
            technician = technicians[state.technician_id]
            depart_at = max(state.current, event.event_time)
            emergency_finish = (
                depart_at + provider.minutes(state.location, event.location, depart_at) + event.duration_minutes
            )
            current = emergency_finish
            location = event.location
            route = routes.get(state.technician_id, [])
            for assignment in route[state.next_assignment_index :]:
                order = orders[assignment.work_order_id]
                # This projection starts after the emergency visit. Even when the
                # technician was waiting at the next customer before dispatch,
                # they must travel back from the emergency location.
                arrival = current + provider.minutes(location, order.location, current)
                start = max(arrival, order.window_start, order.reported_at or 0)
                if request.execution_policy is RiskExecutionPolicy.follow_published_schedule:
                    start = max(start, assignment.start_time)
                current = start + order.service_duration
                location = order.location
            route_entry = route_entries.get(state.technician_id)
            return_location = route_entry.return_location if route_entry else technician.start_location
            terminal = current + provider.minutes(location, return_location, current)
            return emergency_finish, terminal

        states: dict[str, _RiskRouteState] = {}
        unserved = initially_unserved
        work_order_late_minutes: dict[str, int | None] = {}
        work_order_outcomes: dict[str, SimulatedWorkOrderOutcome] = {
            item.work_order_id: SimulatedWorkOrderOutcome(
                work_order_id=item.work_order_id,
                disposition="PLAN_UNSERVED",
            )
            for item in schedule.unassigned
        }
        for technician_id in sorted(technicians):
            if technician_id in absent:
                absent_route = routes.get(technician_id, [])
                unserved += len(absent_route)
                work_order_late_minutes.update({item.work_order_id: None for item in absent_route})
                work_order_outcomes.update(
                    {
                        item.work_order_id: SimulatedWorkOrderOutcome(
                            work_order_id=item.work_order_id,
                            disposition="ABSENCE_UNSERVED",
                            technician_id=technician_id,
                        )
                        for item in absent_route
                    }
                )
                continue
            states[technician_id] = new_state(technician_id)

        emergency_target: str | None = None
        decision_information_set: EmergencyDecisionInformationSet | None = None
        if emergency_event is not None:
            choices: list[tuple[int, str]] = []
            excluded: dict[str, str] = {}
            deterministic_dispatches: dict[str, int] = {}
            deterministic_finishes: dict[str, int] = {}
            deterministic_terminals: dict[str, int] = {}
            for technician_id in sorted(technicians):
                if technician_id in absent:
                    excluded[technician_id] = "TECHNICIAN_ABSENT"
                    continue
                technician = technicians[technician_id]
                if emergency_event.required_skill not in technician.skills:
                    excluded[technician_id] = "REQUIRED_SKILL_MISSING"
                    continue
                projected_state = event_information_projection(emergency_event, technician_id)
                deterministic_dispatches[technician_id] = projected_state.current
                emergency_finish, deterministic_terminal = deterministic_candidate_projection(
                    projected_state, emergency_event
                )
                deterministic_finishes[technician_id] = emergency_finish
                deterministic_terminals[technician_id] = deterministic_terminal
                if deterministic_terminal > technician.shift_end + technician.overtime_limit:
                    excluded[technician_id] = "DETERMINISTIC_MINIMUM_RETURN_EXCEEDS_OVERTIME_LIMIT"
                    continue
                choices.append((emergency_finish, technician_id))
            if choices:
                _finish, emergency_target = min(choices, key=lambda item: (item[0], item[1]))
                selected_state = states[emergency_target]
                advance_to_dispatch_checkpoint(selected_state, emergency_event)
                apply_emergency(selected_state, emergency_event)
                finish_route(selected_state)
            else:
                unserved += 1
            selected_state = states.get(emergency_target) if emergency_target else None
            decision_information_set = EmergencyDecisionInformationSet(
                event_time=emergency_event.event_time,
                decision_time=emergency_event.event_time,
                candidate_technician_ids=sorted(technicians),
                excluded_candidate_reasons=excluded,
                selected_technician_id=emergency_target,
                dispatch_time=selected_state.emergency_dispatch_time if selected_state else None,
                dispatch_location=selected_state.emergency_dispatch_location if selected_state else None,
                deterministic_dispatch_by_technician=deterministic_dispatches,
                deterministic_finish_by_technician=deterministic_finishes,
                deterministic_terminal_by_technician=deterministic_terminals,
            )

        for technician_id, state in states.items():
            if technician_id != emergency_target:
                finish_route(state)
            work_order_late_minutes.update(state.work_order_late_minutes)
            work_order_outcomes.update(state.work_order_outcomes)

        on_time = sum(state.on_time for state in states.values())
        published_total_late = sum(state.total_late for state in states.values())
        emergency_late = sum(state.emergency_late for state in states.values())
        total_late = published_total_late + emergency_late
        total_overtime = sum(state.total_overtime for state in states.values())
        unserved += sum(state.unserved for state in states.values())
        no_show_failure = any(state.no_show_failure for state in states.values())
        window_failure = any(state.window_failure for state in states.values())
        overtime_failure = any(state.overtime_failure for state in states.values())
        emergency_completed = bool(emergency_target and states[emergency_target].emergency_completed)
        emergency_on_time = bool(emergency_target and states[emergency_target].emergency_on_time)
        return {
            "on_time": on_time,
            "total_late": total_late,
            "published_total_late": published_total_late,
            "emergency_late": emergency_late,
            "total_overtime": total_overtime,
            "unserved": unserved,
            "no_show_failure": no_show_failure,
            "window_failure": window_failure,
            "overtime_failure": overtime_failure,
            "emergency_completed": emergency_completed,
            "emergency_on_time": emergency_on_time,
            "emergency_technician_id": emergency_target,
            "emergency_dispatch_time": states[emergency_target].emergency_dispatch_time if emergency_target else None,
            "emergency_finish_time": states[emergency_target].emergency_finish_time if emergency_target else None,
            "emergency_route_terminal_time": states[emergency_target].current if emergency_target else None,
            "emergency_dispatch_location": (
                states[emergency_target].emergency_dispatch_location if emergency_target else None
            ),
            "emergency_decision_information_set": decision_information_set,
            "emergency_sla_failure": emergency_event is not None
            and (emergency_target is None or not emergency_on_time),
            "work_order_late_minutes": work_order_late_minutes,
            "work_order_outcomes": work_order_outcomes,
        }

    for trial in range(request.trials):
        absent = {
            technician_id
            for technician_id in technicians
            if _keyed_draw(seed, trial, "absence", technician_id, modulo=10_000)
            < request.technician_absence_basis_points
        }
        emergency = emergency_events_by_trial.get(trial)
        emergency_event = emergency is not None
        baseline_outcome = trial_outcome(trial, absent, None)
        no_absence_outcome = trial_outcome(trial, set(), emergency)
        outcome = trial_outcome(trial, absent, emergency)
        absence_caused_unserved = int(outcome["unserved"]) > int(no_absence_outcome["unserved"])
        absence_caused_sla = int(outcome["on_time"]) < int(no_absence_outcome["on_time"]) or int(
            outcome["total_late"]
        ) > int(no_absence_outcome["total_late"])
        absence_caused_overtime = int(outcome["total_overtime"]) > int(no_absence_outcome["total_overtime"])
        absence_failure = absence_caused_unserved or absence_caused_sla or absence_caused_overtime
        no_show_failure = bool(outcome["no_show_failure"])
        window_failure = bool(outcome["window_failure"])
        overtime_failure = bool(outcome["overtime_failure"])
        emergency_caused_window = emergency_event and window_failure and not bool(baseline_outcome["window_failure"])
        emergency_caused_overtime = (
            emergency_event and overtime_failure and not bool(baseline_outcome["overtime_failure"])
        )
        emergency_caused_unserved = emergency_event and int(outcome["unserved"]) > int(baseline_outcome["unserved"])
        emergency_caused_sla = emergency_event and (
            int(outcome["on_time"]) < int(baseline_outcome["on_time"])
            or int(outcome["total_late"]) > int(baseline_outcome["total_late"])
            or bool(outcome["emergency_sla_failure"])
        )
        emergency_caused_failure = (
            emergency_caused_window or emergency_caused_overtime or emergency_caused_unserved or emergency_caused_sla
        )
        emergency_incremental_late = max(0, int(outcome["total_late"]) - int(baseline_outcome["total_late"]))
        emergency_incremental_overtime = max(
            0,
            int(outcome["total_overtime"]) - int(baseline_outcome["total_overtime"]),
        )
        emergency_incremental_unserved = max(
            0,
            int(outcome["unserved"]) - int(baseline_outcome["unserved"]),
        )
        baseline_dispositions = baseline_outcome["work_order_outcomes"]
        emergency_dispositions = outcome["work_order_outcomes"]
        outcome_ids = set(baseline_dispositions) | set(emergency_dispositions)
        served_dispositions = {"ON_TIME", "LATE"}
        emergency_disposition_changed = sum(
            1
            for work_order_id in outcome_ids
            if baseline_dispositions.get(work_order_id) is None
            or emergency_dispositions.get(work_order_id) is None
            or baseline_dispositions[work_order_id].disposition != emergency_dispositions[work_order_id].disposition
        )
        emergency_newly_unserved = sum(
            1
            for work_order_id in outcome_ids
            if baseline_dispositions.get(work_order_id) is not None
            and emergency_dispositions.get(work_order_id) is not None
            and baseline_dispositions[work_order_id].disposition in served_dispositions
            and emergency_dispositions[work_order_id].disposition not in served_dispositions
        )
        emergency_newly_late = sum(
            1
            for work_order_id in outcome_ids
            if baseline_dispositions.get(work_order_id) is not None
            and emergency_dispositions.get(work_order_id) is not None
            and baseline_dispositions[work_order_id].disposition == "ON_TIME"
            and emergency_dispositions[work_order_id].disposition == "LATE"
        )
        emergency_lateness_increased = sum(
            1
            for work_order_id in outcome_ids
            if baseline_dispositions.get(work_order_id) is not None
            and emergency_dispositions.get(work_order_id) is not None
            and baseline_dispositions[work_order_id].disposition == "LATE"
            and emergency_dispositions[work_order_id].disposition == "LATE"
            and (emergency_dispositions[work_order_id].late_minutes or 0)
            > (baseline_dispositions[work_order_id].late_minutes or 0)
        )
        emergency_affected_orders = sum(
            1
            for work_order_id in outcome_ids
            if baseline_dispositions.get(work_order_id) is None
            or emergency_dispositions.get(work_order_id) is None
            or baseline_dispositions[work_order_id].disposition != emergency_dispositions[work_order_id].disposition
            or (baseline_dispositions[work_order_id].late_minutes or 0)
            != (emergency_dispositions[work_order_id].late_minutes or 0)
        )
        published_sla_rate = int(outcome["on_time"]) / active_count if active_count else 1.0
        all_demand_count = active_count + int(emergency_event)
        all_demand_on_time = int(outcome["on_time"]) + int(bool(outcome["emergency_on_time"]))
        all_demand_sla_rate = all_demand_on_time / all_demand_count if all_demand_count else 1.0
        sla_rates.append(published_sla_rate)
        all_demand_sla_rates.append(all_demand_sla_rate)
        all_demand_late_totals.append(int(outcome["total_late"]))
        published_late_totals.append(int(outcome["published_total_late"]))
        if emergency_event and bool(outcome["emergency_completed"]):
            emergency_late_totals.append(int(outcome["emergency_late"]))
        overtime_totals.append(int(outcome["total_overtime"]))
        unserved_totals.append(int(outcome["unserved"]))
        if absence_failure or no_show_failure or window_failure or overtime_failure or emergency_caused_failure:
            failed_trials += 1
        absence_event_trials += int(bool(absent))
        absence_caused_failure_trials += int(absence_failure)
        absence_caused_unserved_trials += int(absence_caused_unserved)
        absence_caused_sla_trials += int(absence_caused_sla)
        absence_caused_overtime_trials += int(absence_caused_overtime)
        no_show_trials += int(no_show_failure)
        window_failure_trials += int(window_failure)
        overtime_failure_trials += int(overtime_failure)
        emergency_event_trials += int(emergency_event)
        emergency_caused_failure_trials += int(emergency_caused_failure)
        emergency_caused_window_trials += int(emergency_caused_window)
        emergency_caused_overtime_trials += int(emergency_caused_overtime)
        emergency_caused_unserved_trials += int(emergency_caused_unserved)
        emergency_caused_sla_trials += int(emergency_caused_sla)
        emergency_completed_trials += int(bool(outcome["emergency_completed"]))
        emergency_on_time_trials += int(bool(outcome["emergency_on_time"]))
        if emergency_event:
            emergency_incremental_late_totals.append(emergency_incremental_late)
            emergency_incremental_overtime_totals.append(emergency_incremental_overtime)
            emergency_incremental_unserved_totals.append(emergency_incremental_unserved)
            emergency_affected_order_totals.append(emergency_affected_orders)
            emergency_disposition_changed_totals.append(emergency_disposition_changed)
            emergency_newly_unserved_totals.append(emergency_newly_unserved)
            emergency_newly_late_totals.append(emergency_newly_late)
            emergency_lateness_increased_totals.append(emergency_lateness_increased)
        trial_metrics.append(
            RiskTrialMetric(
                trial=trial,
                sla_on_time_rate=published_sla_rate,
                published_commitment_sla_rate=published_sla_rate,
                all_demand_sla_rate=all_demand_sla_rate,
                emergency_event=emergency_event,
                emergency_completed=bool(outcome["emergency_completed"]),
                emergency_on_time=bool(outcome["emergency_on_time"]),
                emergency_technician_id=outcome["emergency_technician_id"],
                emergency_dispatch_time=outcome["emergency_dispatch_time"],
                emergency_finish_time=outcome["emergency_finish_time"],
                emergency_route_terminal_time=outcome["emergency_route_terminal_time"],
                emergency_dispatch_location=outcome["emergency_dispatch_location"],
                emergency_decision_information_set=outcome["emergency_decision_information_set"],
                emergency_incremental_late_minutes=emergency_incremental_late,
                emergency_incremental_overtime_minutes=emergency_incremental_overtime,
                emergency_incremental_unserved_orders=emergency_incremental_unserved,
                emergency_affected_work_order_count=emergency_affected_orders,
                emergency_disposition_changed_count=emergency_disposition_changed,
                emergency_newly_unserved_count=emergency_newly_unserved,
                emergency_newly_late_count=emergency_newly_late,
                emergency_lateness_increased_count=emergency_lateness_increased,
                published_work_total_late_minutes=int(outcome["published_total_late"]),
                all_demand_total_late_minutes=int(outcome["total_late"]),
                emergency_late_minutes=(
                    int(outcome["emergency_late"]) if emergency_event and bool(outcome["emergency_completed"]) else None
                ),
                work_order_outcomes=(
                    [outcome["work_order_outcomes"][item] for item in sorted(outcome["work_order_outcomes"])]
                    if request.artifact_detail_policy is RiskArtifactDetailPolicy.full_trial_detail
                    else []
                ),
                total_overtime_minutes=int(outcome["total_overtime"]),
                total_unserved_orders=int(outcome["unserved"]),
                disrupted=bool(
                    absence_failure or no_show_failure or window_failure or overtime_failure or emergency_caused_failure
                ),
            )
        )

    input_hash = expected_input_hash or canonical_decision_input_hash(
        plan, "RISK", {"request": request, "resolved_seed": seed}, context, provider
    )
    mean_sla = sum(sla_rates) / request.trials
    bootstrap_means = sorted(
        statistics.fmean(
            sla_rates[_keyed_draw(seed, bootstrap_sample, "single_plan_bootstrap", draw, modulo=request.trials)]
            for draw in range(request.trials)
        )
        for bootstrap_sample in range(2_000)
    )
    ci_low = bootstrap_means[int(0.025 * (len(bootstrap_means) - 1))]
    ci_high = bootstrap_means[int(0.975 * (len(bootstrap_means) - 1))]
    disruption_probability = round(failed_trials / request.trials, 4)
    expected_total_unserved = round(sum(unserved_totals) / request.trials, 2)
    all_demand_late_p50 = _percentile(all_demand_late_totals, 0.5)
    all_demand_late_p90 = _percentile(all_demand_late_totals, 0.9)
    all_demand_late_p95 = _percentile(all_demand_late_totals, 0.95)
    published_late_p50 = _percentile(published_late_totals, 0.5)
    published_late_p90 = _percentile(published_late_totals, 0.9)
    published_late_p95 = _percentile(published_late_totals, 0.95)
    scenario_set_hash = content_hash(scenario_set_manifest)
    emergency_event_probability = round(emergency_event_trials / request.trials, 4)
    emergency_caused_probability = round(emergency_caused_failure_trials / request.trials, 4)
    emergency_completion_rate = (
        round(emergency_completed_trials / emergency_event_trials, 4) if emergency_event_trials else None
    )
    emergency_on_time_rate = (
        round(emergency_on_time_trials / emergency_event_trials, 4) if emergency_event_trials else None
    )
    location_id_payload = scenario_set_manifest.get("emergency_location_work_order_ids")
    emergency_location_work_order_ids = (
        [str(item) for item in location_id_payload] if isinstance(location_id_payload, list) else []
    )
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
        emergency_dispatch_policy=request.emergency_dispatch_policy,
        emergency_responder_selection_policy=request.emergency_responder_selection_policy,
        emergency_location_policy=request.emergency_location_policy,
        emergency_location_work_order_ids=emergency_location_work_order_ids,
        artifact_detail_policy=request.artifact_detail_policy,
        analysis_code_version=__version__,
        algorithm_version=DECISION_ALGORITHM_VERSION,
        build_sha=decision_build_sha(),
        simulation_input_hash=input_hash,
        simulation_scenario_set_hash=scenario_set_hash,
        seed=seed,
        trials=request.trials,
        expected_sla_on_time_rate=round(mean_sla, 4),
        published_commitment_sla_rate=round(mean_sla, 4),
        all_demand_sla_rate=round(sum(all_demand_sla_rates) / request.trials, 4),
        emergency_event_count=emergency_event_trials,
        emergency_completion_rate=emergency_completion_rate,
        emergency_on_time_rate=emergency_on_time_rate,
        emergency_unserved_probability=(
            round((emergency_event_trials - emergency_completed_trials) / emergency_event_trials, 4)
            if emergency_event_trials
            else None
        ),
        emergency_incremental_late_minutes=(
            round(statistics.fmean(emergency_incremental_late_totals), 2) if emergency_incremental_late_totals else None
        ),
        emergency_incremental_overtime_minutes=(
            round(statistics.fmean(emergency_incremental_overtime_totals), 2)
            if emergency_incremental_overtime_totals
            else None
        ),
        emergency_incremental_unserved_orders=(
            round(statistics.fmean(emergency_incremental_unserved_totals), 2)
            if emergency_incremental_unserved_totals
            else None
        ),
        emergency_affected_work_order_count=(
            round(statistics.fmean(emergency_affected_order_totals), 2) if emergency_affected_order_totals else None
        ),
        emergency_disposition_changed_count=(
            round(statistics.fmean(emergency_disposition_changed_totals), 2)
            if emergency_disposition_changed_totals
            else None
        ),
        emergency_newly_unserved_count=(
            round(statistics.fmean(emergency_newly_unserved_totals), 2) if emergency_newly_unserved_totals else None
        ),
        emergency_newly_late_count=(
            round(statistics.fmean(emergency_newly_late_totals), 2) if emergency_newly_late_totals else None
        ),
        emergency_lateness_increased_count=(
            round(statistics.fmean(emergency_lateness_increased_totals), 2)
            if emergency_lateness_increased_totals
            else None
        ),
        emergency_metric_sample_count=emergency_event_trials,
        emergency_completed_sample_count=len(emergency_late_totals),
        monte_carlo_mean_ci_low=round(ci_low, 4),
        monte_carlo_mean_ci_high=round(ci_high, 4),
        sla_rate_ci_low=round(ci_low, 4),
        sla_rate_ci_high=round(ci_high, 4),
        published_work_total_late_minutes_p50=published_late_p50,
        published_work_total_late_minutes_p90=published_late_p90,
        published_work_total_late_minutes_p95=published_late_p95,
        all_demand_total_late_minutes_p50=all_demand_late_p50,
        all_demand_total_late_minutes_p90=all_demand_late_p90,
        all_demand_total_late_minutes_p95=all_demand_late_p95,
        emergency_late_minutes_mean=(
            round(statistics.fmean(emergency_late_totals), 2) if emergency_late_totals else None
        ),
        emergency_late_minutes_p50=(_percentile(emergency_late_totals, 0.5) if emergency_late_totals else None),
        emergency_late_minutes_p90=(_percentile(emergency_late_totals, 0.9) if emergency_late_totals else None),
        # Deprecated aliases retain their historical all-demand mapping.
        full_day_total_late_minutes_p50=all_demand_late_p50,
        full_day_total_late_minutes_p90=all_demand_late_p90,
        full_day_total_late_minutes_p95=all_demand_late_p95,
        scope_total_late_minutes_p50=all_demand_late_p50,
        scope_total_late_minutes_p90=all_demand_late_p90,
        scope_total_late_minutes_p95=all_demand_late_p95,
        late_minutes_p50=all_demand_late_p50,
        late_minutes_p90=all_demand_late_p90,
        late_minutes_p95=all_demand_late_p95,
        expected_overtime_minutes=round(sum(overtime_totals) / request.trials, 2),
        additional_disruption_probability=disruption_probability,
        technician_absence_event_probability=round(absence_event_trials / request.trials, 4),
        absence_caused_failure_probability=round(absence_caused_failure_trials / request.trials, 4),
        absence_caused_unserved_probability=round(absence_caused_unserved_trials / request.trials, 4),
        absence_caused_sla_degradation_probability=round(absence_caused_sla_trials / request.trials, 4),
        absence_caused_overtime_probability=round(absence_caused_overtime_trials / request.trials, 4),
        absence_disruption_probability=round(absence_caused_failure_trials / request.trials, 4),
        no_show_disruption_probability=round(no_show_trials / request.trials, 4),
        window_failure_probability=round(window_failure_trials / request.trials, 4),
        overtime_failure_probability=round(overtime_failure_trials / request.trials, 4),
        emergency_event_probability=emergency_event_probability,
        emergency_caused_failure_probability=emergency_caused_probability,
        emergency_failure_given_event_probability=(
            round(emergency_caused_failure_trials / emergency_event_trials, 4) if emergency_event_trials else None
        ),
        emergency_caused_window_failure_probability=round(
            emergency_caused_window_trials / request.trials,
            4,
        ),
        emergency_caused_overtime_probability=round(emergency_caused_overtime_trials / request.trials, 4),
        emergency_caused_unserved_probability=round(emergency_caused_unserved_trials / request.trials, 4),
        emergency_caused_sla_degradation_probability=round(
            emergency_caused_sla_trials / request.trials,
            4,
        ),
        emergency_capacity_disruption_probability=emergency_caused_probability,
        baseline_unserved_orders=initially_unserved,
        expected_total_unserved_orders=expected_total_unserved,
        plan_failure_probability=disruption_probability,
        expected_unserved_orders=expected_total_unserved,
        assumptions=[
            (
                "该记录以重排发布时的路线入口和执行水位分析历史剩余计划，不混入查询时的新执行事实。"
                if context.analysis_scope is DecisionAnalysisScope.publication_remaining_plan
                else "该记录分析完整冻结发布计划，不混入查询时的执行事实。"
            ),
            "外生随机数按 trial、事件类型和实体 ID 分流；同一场景与 seed 可做配对计划比较。",
            "技师缺勤会使其整条路线失效；客户不在场按 10 分钟现场处置后离开。",
            "突发事件发生概率与其实际造成窗口、加班、未服务或 SLA 恶化的概率分开列示。",
            {
                EmergencyLocationPolicy.active_demand_locations: "紧急需求位置只从本次分析范围内的当前待服务需求地点采样。",
                EmergencyLocationPolicy.all_frozen_locations_as_spatial_proxy: "紧急需求位置从全部冻结工单地点采样，并明确把历史地点作为服务区域空间代理。",
                EmergencyLocationPolicy.uniform_service_area: "紧急需求位置在冻结业务地点与技师出发点形成的矩形服务区内均匀采样。",
                EmergencyLocationPolicy.external_empirical_distribution: "紧急需求位置使用外部经验分布。",
            }[request.emergency_location_policy],
            "默认服从已发布开始时刻；只有显式 EARLIEST_FEASIBLE_EXECUTION 才按最早可行时刻执行。",
            "紧急工单采用 BETWEEN_VISITS_ONLY：不打断服务，也不在行驶途中改道；选人和后续路线共用同一条试验时间线。",
            "已知未分配需求与新增扰动概率分开列示，不把原有缺口称为随机失效。",
            "模拟均值抽样区间只描述 Monte Carlo 均值误差，不是现实业务参数的置信区间。",
            "发布工单、全需求和已完成紧急单的迟到分钟分别统计；旧 late_minutes_* 别名兼容映射到全需求总迟到。",
            "发布承诺 SLA 只覆盖原计划工单；全需求 SLA 另将每个实际发生的紧急需求计入分母。",
        ],
        trial_metrics=trial_metrics,
    )
