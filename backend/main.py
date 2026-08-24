from __future__ import annotations

import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import ortools
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ._version import __version__
from .decision import capacity_analysis, cost_analysis, simulate_plan_risk
from .execution import execution_context_for_planning
from .fixtures import get_fixture
from .hashing import content_hash
from .models import (
    ActivatePlanRequest,
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CloneScenarioRequest,
    Comparison,
    CostAnalysis,
    ExecutionSourceContext,
    ExperimentPublishRequest,
    FreezeReason,
    FrozenAssignment,
    LockedAssignment,
    LockRequest,
    OptimizeRequest,
    PlanningContext,
    PlanVersion,
    PlanVersionPatch,
    ReplanRequest,
    RestoreRequest,
    RiskSimulationRequest,
    RiskSimulationResult,
    RollbackPreview,
    ScenarioCreate,
    ScheduleArtifact,
    ScheduleCandidate,
    ScheduleResult,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleScenario,
    StrategyCandidate,
    StrategyExperiment,
    StrategyExperimentRequest,
    StrategyProfile,
    StrategyProfileCreate,
    Technician,
    TechnicianUpdate,
    WorkOrder,
    WorkOrderExecutionEvent,
    WorkOrderExecutionRequest,
    WorkOrderExecutionResult,
    WorkOrderStatus,
    WorkOrderUpdate,
)
from .normalization import normalize_schedule
from .report import build_report
from .scheduler import (
    baseline_schedule,
    build_solver_policy_snapshot,
    optimized_schedule,
    replan_schedule,
    scenario_for_profile,
)
from .storage import PublicationConflict, ScenarioRevisionConflict, Store
from .verification import verify_schedule

DB_PATH = Path(os.getenv("FIELDFLOW_DB", Path(__file__).resolve().parents[1] / "fieldflow.db"))
store: Store | None = None
experiment_executor: ThreadPoolExecutor | None = None
experiment_slots: threading.BoundedSemaphore | None = None
EXPERIMENT_QUEUE_CAPACITY = 4
router = APIRouter()


@asynccontextmanager
async def lifespan(application: FastAPI):
    global experiment_executor, experiment_slots, store
    store = application.state.store_override or Store(application.state.db_path)
    experiment_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fieldflow-strategy")
    experiment_slots = threading.BoundedSemaphore(EXPERIMENT_QUEUE_CAPACITY)
    try:
        yield
    finally:
        executor, experiment_executor = experiment_executor, None
        experiment_slots = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        store = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def require_store() -> Store:
    if store is None:
        raise RuntimeError("FieldFlow Store 尚未启动；请通过 FastAPI lifespan 运行应用")
    return store


def safe_filename_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:80] or "scenario"


def require_scenario(scenario_id: str) -> ScheduleScenario:
    scenario = require_store().get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, f"场景 {scenario_id} 不存在")
    return scenario


def require_profile(profile_id: str) -> StrategyProfile:
    profile = require_store().get_profile(profile_id)
    if not profile:
        raise HTTPException(404, f"策略 {profile_id} 不存在")
    return profile


def validate_result(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    source: ScheduleResult | None = None,
    planning_context: PlanningContext | None = None,
):
    try:
        execution_context = require_store().execution_source_context(scenario.id)
    except KeyError:
        execution_context = None
    return verify_schedule(
        scenario,
        result,
        source,
        planning_context,
        require_store().travel_provider,
        execution_context,
    )


def save_scenario_change(
    scenario: ScheduleScenario, reason: str, *, preserve_active_plan: bool = False
) -> ScheduleScenario:
    try:
        scenario = ScheduleScenario.model_validate(scenario.model_dump())
    except ValueError as error:
        raise HTTPException(422, detail={"message": "场景数据不完整", "error": str(error)}) from error
    current = require_store().get_scenario(scenario.id)
    if current and current.model_dump(mode="json") == scenario.model_dump(mode="json"):
        return current
    expected_revision = scenario.revision
    scenario.revision += 1
    preserve_active_plan = preserve_active_plan or any(
        order.status is not WorkOrderStatus.pending for order in scenario.work_orders
    )
    try:
        require_store().save_scenario(
            scenario, reason, expected_revision=expected_revision, preserve_active_plan=preserve_active_plan
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已被其他操作更新，请刷新后重试",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    return scenario


def normalize_order(order: WorkOrder) -> WorkOrder:
    if order.is_emergency:
        order.drop_penalty = max(order.drop_penalty, 8000)
    return order


def profile_for_request(strategy: str, profile_id: str | None, time_limit: float | None) -> StrategyProfile:
    profile = require_profile(profile_id or strategy).model_copy(deep=True)
    if time_limit is not None:
        profile.time_limit_seconds = time_limit
    return profile


def bind_solver_policy(
    result: ScheduleResult,
    effective_scenario: ScheduleScenario,
    profile: StrategyProfile | None,
    strategy_key: str,
    original_scenario: ScheduleScenario | None = None,
) -> ScheduleResult:
    result.solver_policy = build_solver_policy_snapshot(
        effective_scenario,
        original_scenario=original_scenario or effective_scenario,
        strategy=strategy_key,
        requested_time_limit_ms=result.requested_time_limit_ms,
        solver_name=result.solver_name,
        profile_id=profile.id if profile else strategy_key,
        profile_name=profile.name if profile else strategy_key,
        profile_snapshot=profile.model_dump(mode="json") if profile else {},
        unassigned_penalty_scale=profile.weights.unassigned_penalty_scale if profile else 1.0,
    )
    return result


def bind_replayed_solver_policy(
    result: ScheduleResult,
    scenario: ScheduleScenario,
    solver_name: str,
) -> ScheduleResult:
    """Describe a history replay as a replay, without pretending to rerun OR-Tools.

    The source policy remains useful provenance for the strategy weights and
    effective drop penalties.  Its routing time limit, however, belongs to the
    original solve and must not be attached to an activation or restore run.
    """
    source_policy = result.solver_policy
    source_solver_name = result.solver_name
    effective = scenario.model_copy(deep=True)
    if source_policy is not None:
        effective.solver_config = source_policy.solver_config.model_copy(deep=True)
        effective_penalties = source_policy.effective_drop_penalties
        for order in effective.work_orders:
            order.drop_penalty = effective_penalties.get(order.id, order.drop_penalty)
        profile_id = source_policy.profile_id
        profile_name = source_policy.profile_name
        profile_snapshot = source_policy.profile_snapshot
        penalty_scale = source_policy.unassigned_penalty_scale
    else:
        profile_id = result.strategy
        profile_name = result.strategy
        profile_snapshot = {}
        penalty_scale = 1.0
    result.solver_name = solver_name
    result.solver_version = "1"
    result.runtime_ms = 0
    result.requested_time_limit_ms = None
    result.effective_time_limit_ms = None
    result.solver_status_code = None
    result.termination_reason = "VERIFIED_PLAN_REPLAY"
    result.solver_objective_value = None
    result.solver_note = f"历史计划已按当前快照重新验证；本次操作未重新运行求解器。原方案由 {source_solver_name} 生成。"
    result.solver_config_hash = content_hash(effective.solver_config)
    result.solver_policy = build_solver_policy_snapshot(
        effective,
        original_scenario=scenario,
        strategy=result.strategy,
        requested_time_limit_ms=None,
        solver_name=solver_name,
        profile_id=profile_id,
        profile_name=profile_name,
        profile_snapshot=profile_snapshot,
        unassigned_penalty_scale=penalty_scale,
    )
    return result


def publication_retry(
    action: str,
    scenario: ScheduleScenario,
    idempotency_key: str | None,
    request_payload: object,
) -> tuple[str | None, str | None, PlanVersion | None]:
    if idempotency_key is None:
        return None, None, None
    key = idempotency_key.strip()
    if not 8 <= len(key) <= 120:
        raise HTTPException(422, "Idempotency-Key 长度必须为 8–120 个字符")
    namespaced_key = f"{scenario.id}:{action}:{key}"
    fingerprint = content_hash(
        {
            "scenario_id": scenario.id,
            "scenario_revision": scenario.revision,
            "scenario_snapshot_hash": content_hash(scenario),
            "action": action,
            "request": request_payload,
        }
    )
    try:
        existing = require_store().published_for_key(namespaced_key, fingerprint)
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error
    return namespaced_key, fingerprint, existing


def reserve_solve_command(publication_key: str | None, fingerprint: str | None) -> None:
    """Ensure one synchronous solver owns an idempotent request at a time."""
    if not publication_key or not fingerprint:
        return
    try:
        created = require_store().begin_command_record(
            "schedule-solve",
            publication_key,
            fingerprint,
            status="RUNNING",
            resource_type=None,
            resource_id=None,
            payload={"publication_key": publication_key},
        )
        if created:
            return
        command = require_store().get_command_record("schedule-solve", publication_key, fingerprint)
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error
    if command and command["status"] == "FAILED":
        payload = command["payload"]
        raise HTTPException(
            int(payload.get("http_status", 409)),
            detail=payload.get("detail", payload),
        )
    raise HTTPException(
        409,
        detail={
            "code": "IDEMPOTENT_REQUEST_IN_PROGRESS",
            "message": "相同请求正在求解，请稍后使用同一幂等键重试",
            "resource_type": command["resource_type"] if command else None,
            "resource_id": command["resource_id"] if command else None,
        },
    )


def update_solve_command(
    publication_key: str | None,
    fingerprint: str | None,
    *,
    status: str,
    resource_type: str | None,
    resource_id: str | None,
    payload: dict,
) -> None:
    if not publication_key or not fingerprint:
        return
    require_store().update_command_record(
        "schedule-solve",
        publication_key,
        fingerprint,
        status=status,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=payload,
    )


def fail_solve_command(
    publication_key: str | None,
    fingerprint: str | None,
    error: Exception,
    run: ScheduleRun | None = None,
) -> None:
    if not publication_key or not fingerprint:
        return
    status_code = error.status_code if isinstance(error, HTTPException) else 500
    detail = (
        error.detail
        if isinstance(error, HTTPException)
        else {
            "message": "求解失败",
            "error_type": type(error).__name__,
        }
    )
    try:
        update_solve_command(
            publication_key,
            fingerprint,
            status="FAILED",
            resource_type="schedule_run" if run else None,
            resource_id=run.id if run else None,
            payload={"http_status": status_code, "detail": detail},
        )
    except PublicationConflict:
        # A publication that already committed is authoritative; a late error
        # must never replace its successful terminal command state.
        return


def artifact(role: str, result: ScheduleResult, strategy: str) -> ScheduleArtifact:
    return ScheduleArtifact(id=f"ART-{uuid.uuid4().hex[:10]}", role=role, strategy=strategy, schedule=result)


def prepare_replan_run(
    scenario: ScheduleScenario,
    source: PlanVersion | None,
    request: ReplanRequest,
) -> tuple[
    ScheduleResult,
    list[ScheduleArtifact],
    ScheduleScenario,
    str,
    StrategyProfile,
    int,
    PlanningContext,
    ScheduleRun,
]:
    previous = source.selected if source else None
    internal: list[ScheduleArtifact] = []
    if not previous:
        if any(order.status == WorkOrderStatus.started for order in scenario.work_orders):
            raise HTTPException(409, "执行中的工单缺少可追溯的原方案，不能安全重排")
        balanced = require_profile("balanced")
        base_effective = scenario_for_profile(scenario, balanced)
        previous = optimized_schedule(
            base_effective,
            0,
            time_limit_seconds=balanced.time_limit_seconds,
            strategy="balanced",
            provider=require_store().travel_provider,
        )
        previous = bind_solver_policy(previous, base_effective, balanced, "balanced", scenario)
        internal.append(artifact("candidate", previous, "balanced"))
    profile = profile_for_request(request.strategy, request.profile_id, request.time_limit_seconds)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    planning_time = request.planning_time
    assert planning_time is not None
    execution_source_context = require_store().execution_source_context(scenario.id)
    planning_context = build_planning_context(
        scenario,
        source,
        planning_time,
        execution_source_context,
    )
    run = start_schedule_run(
        scenario,
        "replan",
        source=source,
        requested_time_limit_seconds=profile.time_limit_seconds,
        planning_context=planning_context,
        solver_config_hash=content_hash(effective.solver_config),
    )
    return (
        previous,
        internal,
        effective,
        strategy_key,
        profile,
        planning_time,
        planning_context,
        run,
    )


def start_schedule_run(
    scenario: ScheduleScenario,
    action: str,
    *,
    source: PlanVersion | None = None,
    requested_time_limit_seconds: float = 0,
    solver_name: str = "ortools-routing",
    planning_context: PlanningContext | None = None,
    solver_config_hash: str | None = None,
) -> ScheduleRun:
    requested_ms = int(round(requested_time_limit_seconds * 1000))
    run = ScheduleRun(
        id=f"RUN-{uuid.uuid4().hex[:12]}",
        scenario_id=scenario.id,
        action=action,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        source_plan_version_id=source.id if source else None,
        source_plan_snapshot_hash=source.scenario_snapshot_hash if source else None,
        solver_name=solver_name,
        solver_version="pending",
        solver_config_hash=solver_config_hash or content_hash(scenario.solver_config),
        requested_time_limit_ms=requested_ms,
        effective_time_limit_ms=requested_ms,
        status=ScheduleRunStatus.running,
        started_at=_now(),
        planning_context=planning_context,
        planning_context_hash=content_hash(planning_context) if planning_context else None,
    )
    return require_store().save_schedule_run(run)


def fail_schedule_run(run: ScheduleRun, reason: str) -> None:
    stored = require_store().get_schedule_run(run.id)
    if stored and stored.status not in {ScheduleRunStatus.queued, ScheduleRunStatus.running}:
        return
    failed = run.model_copy(deep=True)
    failed.status = ScheduleRunStatus.failed
    failed.termination_reason = reason
    failed.finished_at = _now()
    try:
        require_store().save_schedule_run(failed)
    except PublicationConflict:
        return


def run_status_for_result(result: ScheduleResult) -> ScheduleRunStatus:
    try:
        return ScheduleRunStatus(result.solver_status.value)
    except ValueError:
        return ScheduleRunStatus.failed


def build_planning_context(
    scenario: ScheduleScenario,
    source: PlanVersion | None,
    planning_time: int,
    execution_source_context: ExecutionSourceContext | None = None,
) -> PlanningContext:
    source_assignments = {item.work_order_id: item for item in source.selected.assignments} if source else {}
    execution_context, execution_warnings = (
        execution_context_for_planning(execution_source_context, planning_time)
        if execution_source_context
        else (None, [])
    )
    execution_sources = (
        {item.work_order_id: item for item in execution_context.started_assignments} if execution_context else {}
    )
    frozen: list[FrozenAssignment] = []
    inferred: list[str] = []
    for order in scenario.work_orders:
        assignment = source_assignments.get(order.id)
        if not assignment and order.status is WorkOrderStatus.started:
            raise HTTPException(
                409,
                detail={
                    "message": "执行状态缺少来源方案分配，不能安全重排",
                    "code": "STARTED_WORK_ORDER_SOURCE_ASSIGNMENT_MISSING",
                    "work_order_id": order.id,
                },
            )
        if not assignment:
            continue
        if order.status is WorkOrderStatus.started:
            execution_source = execution_sources.get(order.id)
            frozen.append(
                FrozenAssignment(
                    work_order_id=order.id,
                    technician_id=(execution_source.technician_id if execution_source else assignment.technician_id),
                    sequence=assignment.sequence,
                    start_time=(execution_source.planned_start_at if execution_source else assignment.start_time),
                    finish_time=(execution_source.planned_finish_at if execution_source else assignment.finish_time),
                    reason=FreezeReason(order.status.value.upper()),
                    source_sequence=(
                        execution_source.source_sequence
                        if execution_source and execution_source.source_sequence
                        else assignment.source_sequence or assignment.sequence
                    ),
                    source_assignment_hash=(
                        execution_source.source_assignment_hash
                        if execution_source
                        else assignment.source_assignment_hash
                    ),
                )
            )
        elif order.status is WorkOrderStatus.pending and (
            assignment.start_time <= planning_time
            or assignment.arrival_time - assignment.travel_minutes <= planning_time
        ):
            inferred.append(order.id)
    return PlanningContext(
        planning_time=planning_time,
        source_plan_version_id=source.id if source else None,
        source_plan_snapshot_hash=source.scenario_snapshot_hash if source else None,
        scenario_revision=scenario.revision,
        execution_source_context=execution_context,
        frozen_assignments=sorted(frozen, key=lambda item: item.work_order_id),
        inferred_departure_warnings=sorted(inferred),
        execution_warnings=sorted(execution_warnings),
    )


def publish_selected(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    action: str,
    *,
    artifacts: list[ScheduleArtifact],
    source: PlanVersion | None = None,
    relation: str = "new",
    label: str | None = None,
    run: ScheduleRun,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    replace_scenario: bool = False,
    expected_revision: int | None = None,
    planning_context: PlanningContext | None = None,
) -> ScheduleResult:
    source_schedule = source.selected if source and result.kind == "replan" else None
    verification = validate_result(scenario, result, source_schedule, planning_context)
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}",
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        source_plan_version_id=source.id if source else None,
        solver_config_hash=result.solver_config_hash,
        solver_policy_fingerprint=result.solver_policy.fingerprint if result.solver_policy else "",
        schedule=result,
        verification_report=verification,
        publishable=verification.publishable,
        created_at=_now(),
        planning_context=planning_context,
        planning_context_hash=content_hash(planning_context) if planning_context else None,
    )
    run.status = run_status_for_result(result)
    run.termination_reason = result.termination_reason
    run.solution_found = result.solution_found
    run.finished_at = _now()
    run.candidate_id = candidate.id
    run.solver_name = result.solver_name
    run.solver_version = result.solver_version
    run.solver_config_hash = result.solver_config_hash
    run.solver_policy_fingerprint = result.solver_policy.fingerprint if result.solver_policy else ""
    run.requested_time_limit_ms = result.requested_time_limit_ms or run.requested_time_limit_ms
    run.effective_time_limit_ms = result.effective_time_limit_ms or run.effective_time_limit_ms
    require_store().complete_schedule_run(run, candidate)
    if not candidate.publishable:
        active = require_store().active_plan_version(scenario.id)
        raise HTTPException(
            422,
            detail={
                "message": "候选方案未通过发布验证，当前正式方案保持不变",
                "run_id": run.id,
                "candidate_id": candidate.id,
                "solver_status": result.solver_status.value,
                "active_plan_version_id": active.id if active else None,
                "errors": [item.model_dump() for item in verification.errors],
            },
        )
    try:
        plan = require_store().publish_plan(
            scenario,
            result,
            action,
            artifacts=artifacts,
            source_version_id=source.id if source else None,
            relation=relation,
            label=label,
            replace_scenario=replace_scenario,
            expected_revision=scenario.revision if expected_revision is None else expected_revision,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            candidate_id=candidate.id,
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "求解期间业务数据已变化，结果未发布，请重新运行",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error
    return plan.selected


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fieldflow", "version": __version__}


@router.get("/api/scenarios", response_model=list[ScheduleScenario])
def list_scenarios() -> list[ScheduleScenario]:
    return require_store().list_scenarios()


@router.get("/api/scenarios/{scenario_id}", response_model=ScheduleScenario)
def get_scenario(scenario_id: str) -> ScheduleScenario:
    return require_scenario(scenario_id)


@router.post("/api/scenarios", response_model=ScheduleScenario)
def create_scenario(request: ScenarioCreate) -> ScheduleScenario:
    try:
        scenario = get_fixture(request.fixture_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    scenario.id = f"{request.fixture_id}-{uuid.uuid4().hex[:8]}"
    if request.name:
        scenario.name = request.name
    scenario.source_scenario_id = request.fixture_id
    try:
        require_store().save_scenario(scenario, "创建业务场景")
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409, detail={"message": "场景标识发生冲突，请重新创建", "current_revision": error.current}
        ) from error
    return scenario


@router.post("/api/scenarios/{scenario_id}/reset", response_model=ScheduleScenario)
def reset_scenario(scenario_id: str) -> ScheduleScenario:
    current = require_scenario(scenario_id)
    execution_events = require_store().list_execution_events(scenario_id)
    if execution_events:
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTION_HISTORY_PRESENT",
                "message": "场景已有执行记录，不能恢复初始数据；请保留审计事实并使用业务修正流程",
                "execution_event_ids": [event.id for event in execution_events],
            },
        )
    revisions = require_store().list_revisions(scenario_id)
    if not revisions:
        raise HTTPException(409, "该场景没有可恢复的初始业务数据")
    fresh = revisions[0].scenario.model_copy(deep=True)
    fresh.id = current.id
    fresh.name = current.name
    fresh.source_scenario_id = current.source_scenario_id
    fresh.revision = current.revision + 1
    try:
        require_store().save_scenario(fresh, "恢复初始业务数据", expected_revision=current.revision)
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已被其他操作更新，请刷新后重试",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    return fresh


@router.get("/api/technicians")
def get_technicians(scenario_id: str = Query("main")):
    return require_scenario(scenario_id).technicians


@router.get("/api/work-orders")
def get_work_orders(scenario_id: str = Query("main")):
    return require_scenario(scenario_id).work_orders


@router.post("/api/scenarios/{scenario_id}/work-orders", response_model=ScheduleScenario)
def create_work_order(scenario_id: str, work_order: WorkOrder) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    if any(item.id == work_order.id for item in scenario.work_orders):
        raise HTTPException(409, f"工单 {work_order.id} 已存在")
    scenario.work_orders.append(normalize_order(work_order))
    return save_scenario_change(scenario, f"新增工单 {work_order.id}")


@router.put("/api/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
def update_work_order(scenario_id: str, work_order_id: str, request: WorkOrderUpdate) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    index = next((i for i, item in enumerate(scenario.work_orders) if item.id == work_order_id), None)
    if index is None:
        raise HTTPException(404, f"工单 {work_order_id} 不存在")
    original = scenario.work_orders[index]
    updates = request.model_dump(exclude_unset=True)
    if updates.get("note") is None and "note" in updates:
        updates["note"] = ""
    requested_status = updates.get("status")
    if requested_status is not None and requested_status is not original.status:
        raise HTTPException(409, "工单状态只能通过开始服务或完成服务操作变更")
    if original.status.value in {"started", "completed"}:
        immutable_fields = {
            "customer_name",
            "title",
            "required_skills",
            "location",
            "service_duration",
            "window_start",
            "window_end",
            "sla_deadline",
            "priority",
            "drop_penalty",
            "vip",
            "is_emergency",
            "reported_at",
        }
        if any(key in updates and updates[key] != original.model_dump().get(key) for key in immutable_fields):
            raise HTTPException(409, "已开始或已完成的工单只能修改备注和状态")
    payload = original.model_dump()
    payload.update(updates)
    if updates.get("is_emergency") is False:
        payload["reported_at"] = None
        if original.is_emergency and "drop_penalty" not in updates:
            priority = payload["priority"].value if hasattr(payload["priority"], "value") else payload["priority"]
            payload["drop_penalty"] = {"urgent": 8500, "high": 4800, "normal": 2500, "low": 1400}[priority]
    try:
        scenario.work_orders[index] = normalize_order(WorkOrder.model_validate(payload))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return save_scenario_change(scenario, f"更新工单 {work_order_id}")


def execute_work_order_transition(
    scenario_id: str,
    work_order_id: str,
    action: str,
    request: WorkOrderExecutionRequest,
) -> WorkOrderExecutionResult:
    fingerprint = content_hash(
        {
            "scenario_id": scenario_id,
            "work_order_id": work_order_id,
            "action": action,
            "request": request,
        }
    )
    try:
        return require_store().transition_work_order(
            scenario_id,
            work_order_id,
            action,
            request,
            request_fingerprint=fingerprint,
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise HTTPException(
            409,
            detail={"message": str(error), "code": error.code, **error.details},
        ) from error


@router.post("/api/scenarios/{scenario_id}/work-orders/{work_order_id}/start", response_model=WorkOrderExecutionResult)
def start_work_order(
    scenario_id: str, work_order_id: str, request: WorkOrderExecutionRequest
) -> WorkOrderExecutionResult:
    return execute_work_order_transition(scenario_id, work_order_id, "start", request)


@router.post(
    "/api/scenarios/{scenario_id}/work-orders/{work_order_id}/complete", response_model=WorkOrderExecutionResult
)
def complete_work_order(
    scenario_id: str, work_order_id: str, request: WorkOrderExecutionRequest
) -> WorkOrderExecutionResult:
    return execute_work_order_transition(scenario_id, work_order_id, "complete", request)


@router.get("/api/scenarios/{scenario_id}/execution-events", response_model=list[WorkOrderExecutionEvent])
def list_execution_events(scenario_id: str) -> list[WorkOrderExecutionEvent]:
    require_scenario(scenario_id)
    return require_store().list_execution_events(scenario_id)


@router.delete("/api/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
def delete_work_order(scenario_id: str, work_order_id: str) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    order = next((item for item in scenario.work_orders if item.id == work_order_id), None)
    if not order:
        raise HTTPException(404, f"工单 {work_order_id} 不存在")
    if order.status.value in {"started", "completed"}:
        raise HTTPException(409, "已开始或已完成的工单不能删除")
    scenario.work_orders = [item for item in scenario.work_orders if item.id != work_order_id]
    scenario.locked_assignments = [item for item in scenario.locked_assignments if item.work_order_id != work_order_id]
    return save_scenario_change(scenario, f"删除工单 {work_order_id}")


@router.post("/api/scenarios/{scenario_id}/technicians", response_model=ScheduleScenario)
def create_technician(scenario_id: str, technician: Technician) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    if any(item.id == technician.id for item in scenario.technicians):
        raise HTTPException(409, f"技师 {technician.id} 已存在")
    scenario.technicians.append(technician)
    return save_scenario_change(scenario, f"新增技师 {technician.id}")


@router.put("/api/scenarios/{scenario_id}/technicians/{technician_id}", response_model=ScheduleScenario)
def update_technician(scenario_id: str, technician_id: str, request: TechnicianUpdate) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    index = next((i for i, item in enumerate(scenario.technicians) if item.id == technician_id), None)
    if index is None:
        raise HTTPException(404, f"技师 {technician_id} 不存在")
    payload = scenario.technicians[index].model_dump()
    payload.update(request.model_dump(exclude_unset=True))
    try:
        scenario.technicians[index] = Technician.model_validate(payload)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return save_scenario_change(scenario, f"更新技师 {technician_id}")


@router.post("/api/scenarios/{scenario_id}/lock", response_model=ScheduleScenario)
def lock_assignment(scenario_id: str, request: LockRequest) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    order = next((o for o in scenario.work_orders if o.id == request.work_order_id), None)
    tech = next((t for t in scenario.technicians if t.id == request.technician_id), None)
    if not order:
        raise HTTPException(404, f"工单 {request.work_order_id} 不存在")
    if not tech:
        raise HTTPException(404, f"技师 {request.technician_id} 不存在")
    if not set(order.required_skills).issubset(set(tech.skills)):
        raise HTTPException(422, f"{tech.name} 不具备该工单要求的全部技能")
    if order.status is not WorkOrderStatus.pending:
        raise HTTPException(409, "已开始或已完成工单不能更改锁定关系")
    scenario.locked_assignments = [
        item for item in scenario.locked_assignments if item.work_order_id != request.work_order_id
    ]
    if request.locked:
        scenario.locked_assignments.append(
            LockedAssignment(work_order_id=request.work_order_id, technician_id=request.technician_id)
        )
    return save_scenario_change(scenario, ("锁定" if request.locked else "解除锁定") + f"工单 {request.work_order_id}")


@router.get("/api/scenarios/{scenario_id}/schedules", response_model=list[ScheduleResult])
def list_schedule_versions(scenario_id: str) -> list[ScheduleResult]:
    require_scenario(scenario_id)
    return require_store().list_schedules(scenario_id)


@router.get("/api/scenarios/{scenario_id}/schedule-runs", response_model=list[ScheduleRun])
def list_schedule_runs(scenario_id: str) -> list[ScheduleRun]:
    require_scenario(scenario_id)
    return require_store().list_schedule_runs(scenario_id)


@router.get("/api/scenarios/{scenario_id}/schedule-runs/{run_id}", response_model=ScheduleRun)
def get_schedule_run(scenario_id: str, run_id: str) -> ScheduleRun:
    run = require_store().get_schedule_run(run_id)
    if not run or run.scenario_id != scenario_id:
        raise HTTPException(404, "求解记录不存在")
    return run


@router.get("/api/scenarios/{scenario_id}/schedule-candidates/{candidate_id}", response_model=ScheduleCandidate)
def get_schedule_candidate(scenario_id: str, candidate_id: str) -> ScheduleCandidate:
    candidate = require_store().get_schedule_candidate(candidate_id)
    if not candidate or candidate.scenario_id != scenario_id:
        raise HTTPException(404, "候选方案不存在")
    return candidate


@router.post("/api/scenarios/{scenario_id}/baseline", response_model=ScheduleResult)
def run_baseline(
    scenario_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    if any(order.status is not WorkOrderStatus.pending for order in scenario.work_orders):
        raise HTTPException(409, "场景已有执行记录，请使用局部重排延续当前执行位置和容量")
    publication_key, fingerprint, existing = publication_retry("baseline", scenario, idempotency_key, {})
    if existing:
        return existing.selected
    reserve_solve_command(publication_key, fingerprint)
    run: ScheduleRun | None = None
    try:
        run = start_schedule_run(scenario, "baseline", solver_name="fieldflow-greedy")
        update_solve_command(
            publication_key,
            fingerprint,
            status="RUNNING",
            resource_type="schedule_run",
            resource_id=run.id,
            payload={"run_id": run.id},
        )
        result = normalize_schedule(
            scenario,
            baseline_schedule(scenario, 0, provider=require_store().travel_provider),
            provider=require_store().travel_provider,
        )
        published = publish_selected(
            scenario,
            result,
            "baseline",
            artifacts=[artifact("selected", result, "baseline")],
            run=run,
            idempotency_key=publication_key,
            request_fingerprint=fingerprint,
        )
        plan = require_store().active_plan_version(scenario.id)
        update_solve_command(
            publication_key,
            fingerprint,
            status="COMPLETED",
            resource_type="plan_version",
            resource_id=plan.id if plan else None,
            payload={"plan_version_id": plan.id if plan else None, "schedule_id": published.id},
        )
        return published
    except Exception as error:
        if run:
            fail_schedule_run(run, f"{type(error).__name__}: {error}")
        fail_solve_command(publication_key, fingerprint, error, run)
        raise


@router.post("/api/scenarios/{scenario_id}/optimize", response_model=ScheduleResult)
def run_optimize(
    scenario_id: str,
    request: OptimizeRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    if any(order.status is not WorkOrderStatus.pending for order in scenario.work_orders):
        raise HTTPException(409, "场景已有执行记录，请使用局部重排延续当前执行位置和容量")
    request = request or OptimizeRequest()
    profile = profile_for_request(request.strategy, request.profile_id, request.time_limit_seconds)
    publication_key, fingerprint, existing = publication_retry(
        "optimize",
        scenario,
        idempotency_key,
        {"request": request, "profile": profile},
    )
    if existing:
        return existing.selected
    reserve_solve_command(publication_key, fingerprint)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    source = require_store().active_plan_version(scenario_id) or require_store().latest_plan_version(scenario_id)
    run: ScheduleRun | None = None
    try:
        run = start_schedule_run(
            scenario,
            "optimize",
            source=source,
            requested_time_limit_seconds=profile.time_limit_seconds,
            solver_config_hash=content_hash(effective.solver_config),
        )
        update_solve_command(
            publication_key,
            fingerprint,
            status="RUNNING",
            resource_type="schedule_run",
            resource_id=run.id,
            payload={"run_id": run.id},
        )
        solver_baseline = baseline_schedule(effective, 0, strategy_key, provider=require_store().travel_provider)
        result = optimized_schedule(
            effective,
            0,
            previous=solver_baseline,
            time_limit_seconds=profile.time_limit_seconds,
            strategy=strategy_key,
            provider=require_store().travel_provider,
        )
        solver_baseline = bind_solver_policy(
            solver_baseline,
            effective,
            profile,
            strategy_key,
            scenario,
        )
        result = bind_solver_policy(result, effective, profile, strategy_key, scenario)
        baseline = normalize_schedule(
            scenario,
            solver_baseline,
            provider=require_store().travel_provider,
            solver_config_hash=content_hash(effective.solver_config),
        )
        result = normalize_schedule(
            scenario,
            result,
            provider=require_store().travel_provider,
            solver_config_hash=content_hash(effective.solver_config),
        )
        source_matches = bool(
            source
            and source.data_revision == scenario.revision
            and (
                source.scenario_snapshot_hash
                or (content_hash(source.scenario_snapshot) if source.scenario_snapshot else "")
            )
            == content_hash(scenario)
        )
        relation = "optimized_from" if source_matches else "fresh_after_data_change" if source else "new"
        published = publish_selected(
            scenario,
            result,
            "optimize",
            artifacts=[artifact("baseline", baseline, strategy_key), artifact("selected", result, strategy_key)],
            source=source,
            relation=relation,
            label=profile.name,
            run=run,
            idempotency_key=publication_key,
            request_fingerprint=fingerprint,
        )
        plan = require_store().active_plan_version(scenario.id)
        update_solve_command(
            publication_key,
            fingerprint,
            status="COMPLETED",
            resource_type="plan_version",
            resource_id=plan.id if plan else None,
            payload={"plan_version_id": plan.id if plan else None, "schedule_id": published.id},
        )
        return published
    except Exception as error:
        if run:
            fail_schedule_run(run, f"{type(error).__name__}: {error}")
        fail_solve_command(publication_key, fingerprint, error, run)
        raise


@router.post("/api/scenarios/{scenario_id}/replan", response_model=ScheduleResult)
def run_replan(scenario_id: str, request: ReplanRequest) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    incoming = request.emergency_order
    idempotency_key = request.idempotency_key or (f"emergency-replan:{scenario_id}:{incoming.id}" if incoming else None)
    request_fingerprint = None
    if idempotency_key:
        fingerprint_payload: dict[str, object] = {
            "scenario_id": scenario_id,
            "request": request.model_dump(mode="json"),
        }
        # A normal replan is meaningful only for the exact aggregate it read.
        # Emergency retries deliberately keep the request-only fingerprint so
        # the already committed intake can be replayed after it increments D.
        if not incoming:
            fingerprint_payload.update(
                {
                    "scenario_revision": scenario.revision,
                    "scenario_snapshot_hash": content_hash(scenario),
                }
            )
        request_fingerprint = content_hash(fingerprint_payload)
    command_namespace = f"{scenario_id}:replan"
    solve_publication_key = f"{command_namespace}:{idempotency_key}" if idempotency_key else None
    intake_namespace = f"{scenario_id}:emergency-intake"
    intake_key = request.intake_idempotency_key or (f"emergency-intake:{incoming.id}" if incoming else None)
    if not incoming and idempotency_key and request_fingerprint:
        try:
            existing_publication = require_store().published_for_key(
                f"{command_namespace}:{idempotency_key}",
                request_fingerprint,
            )
        except PublicationConflict as error:
            raise HTTPException(409, str(error)) from error
        if existing_publication:
            return existing_publication.selected
    if incoming and idempotency_key and request_fingerprint:
        try:
            existing_command = require_store().get_command_record(
                command_namespace, idempotency_key, request_fingerprint
            )
        except PublicationConflict as error:
            raise HTTPException(409, str(error)) from error
        if existing_command and existing_command["status"] == "COMPLETED":
            plan = require_store().get_plan_version(scenario_id, existing_command["resource_id"] or "")
            if plan:
                return plan.selected
        if existing_command and existing_command["status"] == "FAILED":
            payload = existing_command["payload"]
            raise HTTPException(int(payload.get("http_status", 422)), detail=payload.get("detail", payload))
    if incoming:
        normalized = normalize_order(incoming.model_copy(deep=True))
        assert idempotency_key and request_fingerprint and intake_key
        intake_fingerprint = content_hash({"scenario_id": scenario_id, "work_order": normalized})
        try:
            scenario, _ = require_store().intake_emergency_work_order(
                scenario_id,
                normalized,
                namespace=intake_namespace,
                idempotency_key=intake_key,
                request_fingerprint=intake_fingerprint,
            )
            command_created = require_store().begin_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="INTAKE_COMMITTED",
                resource_type="work_order",
                resource_id=normalized.id,
                payload={
                    "work_order_id": normalized.id,
                    "scenario_revision": scenario.revision,
                    "intake_key": intake_key,
                },
            )
            if not command_created:
                command = require_store().get_command_record(command_namespace, idempotency_key, request_fingerprint)
                if command and command["status"] == "COMPLETED":
                    plan = require_store().get_plan_version(scenario_id, command["resource_id"] or "")
                    if plan:
                        return plan.selected
                if command and command["status"] == "FAILED":
                    payload = command["payload"]
                    raise HTTPException(
                        int(payload.get("http_status", 422)),
                        detail=payload.get("detail", payload),
                    )
                raise HTTPException(
                    409,
                    detail={
                        "code": "IDEMPOTENT_REQUEST_IN_PROGRESS",
                        "message": "相同突发重排请求正在处理，请稍后重试",
                        "resource_id": command["resource_id"] if command else None,
                    },
                )
        except PublicationConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, detail={"message": "突发工单数据不完整", "error": str(error)}) from error
    elif solve_publication_key and request_fingerprint:
        reserve_solve_command(solve_publication_key, request_fingerprint)
    source = require_store().active_plan_version(scenario_id) or require_store().latest_plan_version(scenario_id)
    try:
        (
            previous,
            internal,
            effective,
            strategy_key,
            profile,
            planning_time,
            planning_context,
            run,
        ) = prepare_replan_run(scenario, source, request)
    except HTTPException as error:
        if incoming and idempotency_key and request_fingerprint:
            detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
            detail.update(
                {
                    "message": "突发工单已保存，但局部重排无法启动",
                    "emergency_work_order_persisted": True,
                    "scenario_revision": scenario.revision,
                    "coverage_status": "PARTIAL_NEW_DEMAND",
                }
            )
            require_store().update_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="FAILED",
                resource_type="work_order",
                resource_id=incoming.id,
                payload={"http_status": error.status_code, "detail": detail},
            )
            raise HTTPException(error.status_code, detail=detail) from error
        fail_solve_command(solve_publication_key, request_fingerprint, error)
        raise
    except Exception as error:
        if incoming and idempotency_key and request_fingerprint:
            detail = {
                "message": "突发工单已保存，但局部重排准备失败",
                "emergency_work_order_persisted": True,
                "scenario_revision": scenario.revision,
                "coverage_status": "PARTIAL_NEW_DEMAND",
            }
            require_store().update_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="FAILED",
                resource_type="work_order",
                resource_id=incoming.id,
                payload={"http_status": 500, "detail": detail},
            )
            raise HTTPException(500, detail=detail) from error
        fail_solve_command(solve_publication_key, request_fingerprint, error)
        raise
    if incoming and idempotency_key and request_fingerprint:
        require_store().update_command_record(
            command_namespace,
            idempotency_key,
            request_fingerprint,
            status="REPLAN_RUNNING",
            resource_type="schedule_run",
            resource_id=run.id,
            payload={"run_id": run.id, "scenario_revision": scenario.revision},
        )
    elif solve_publication_key and request_fingerprint:
        update_solve_command(
            solve_publication_key,
            request_fingerprint,
            status="RUNNING",
            resource_type="schedule_run",
            resource_id=run.id,
            payload={"run_id": run.id, "scenario_revision": scenario.revision},
        )
    try:
        result = replan_schedule(
            effective,
            0,
            previous,
            planning_time,
            profile.time_limit_seconds,
            strategy_key,
            planning_context=planning_context,
            provider=require_store().travel_provider,
        )
        result = bind_solver_policy(result, effective, profile, strategy_key, scenario)
        result = normalize_schedule(
            scenario,
            result,
            previous,
            provider=require_store().travel_provider,
            solver_config_hash=content_hash(effective.solver_config),
            planning_context=planning_context,
        )
    except Exception as error:
        fail_schedule_run(run, f"{type(error).__name__}: {error}")
        if incoming and idempotency_key and request_fingerprint:
            detail = {
                "message": "突发工单已保存，但局部重排运行失败",
                "run_id": run.id,
                "error_type": type(error).__name__,
                "emergency_work_order_persisted": True,
                "scenario_revision": scenario.revision,
                "coverage_status": "PARTIAL_NEW_DEMAND",
            }
            require_store().update_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="FAILED",
                resource_type="schedule_run",
                resource_id=run.id,
                payload={"http_status": 500, "detail": detail},
            )
            raise HTTPException(500, detail=detail) from error
        fail_solve_command(solve_publication_key, request_fingerprint, error, run)
        raise
    internal.append(artifact("selected", result, strategy_key))
    source_matches = bool(
        source
        and source.data_revision == scenario.revision
        and (
            source.scenario_snapshot_hash
            or (content_hash(source.scenario_snapshot) if source.scenario_snapshot else "")
        )
        == content_hash(scenario)
    )
    relation = "replanned_from" if source_matches else "fresh_after_data_change" if source else "new"
    try:
        published = publish_selected(
            scenario,
            result,
            "replan",
            artifacts=internal,
            source=source,
            relation=relation,
            label=profile.name,
            run=run,
            idempotency_key=f"{command_namespace}:{idempotency_key}" if idempotency_key else None,
            request_fingerprint=request_fingerprint,
            expected_revision=scenario.revision,
            planning_context=planning_context,
        )
    except HTTPException as error:
        if incoming and idempotency_key and request_fingerprint:
            detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
            detail["message"] = "突发工单已保存，但局部重排没有生成可发布方案；最后发布方案仍保留"
            detail["emergency_work_order_persisted"] = True
            detail["scenario_revision"] = scenario.revision
            detail["coverage_status"] = "PARTIAL_NEW_DEMAND"
            require_store().update_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="FAILED",
                resource_type="schedule_candidate",
                resource_id=detail.get("candidate_id"),
                payload={"http_status": error.status_code, "detail": detail},
            )
            raise HTTPException(error.status_code, detail=detail) from error
        fail_solve_command(solve_publication_key, request_fingerprint, error, run)
        raise
    if incoming and idempotency_key and request_fingerprint:
        plan = require_store().active_plan_version(scenario_id)
        require_store().update_command_record(
            command_namespace,
            idempotency_key,
            request_fingerprint,
            status="COMPLETED",
            resource_type="plan_version",
            resource_id=plan.id if plan else None,
            payload={"plan_version_id": plan.id if plan else None, "schedule_id": published.id},
        )
    elif solve_publication_key and request_fingerprint:
        plan = require_store().active_plan_version(scenario_id)
        update_solve_command(
            solve_publication_key,
            request_fingerprint,
            status="COMPLETED",
            resource_type="plan_version",
            resource_id=plan.id if plan else None,
            payload={"plan_version_id": plan.id if plan else None, "schedule_id": published.id},
        )
    return published


@router.get("/api/scenarios/{scenario_id}/plan-versions", response_model=list[PlanVersion])
def list_plan_versions(scenario_id: str) -> list[PlanVersion]:
    require_scenario(scenario_id)
    return require_store().list_plan_versions(scenario_id)


@router.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}", response_model=PlanVersion)
def get_plan_version(scenario_id: str, version_id: str) -> PlanVersion:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return plan


@router.patch("/api/scenarios/{scenario_id}/plan-versions/{version_id}", response_model=PlanVersion)
def rename_plan_version(scenario_id: str, version_id: str, request: PlanVersionPatch) -> PlanVersion:
    require_scenario(scenario_id)
    plan = require_store().rename_plan_version(scenario_id, version_id, request.label)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return plan


@router.get(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/cost-analysis",
    response_model=CostAnalysis,
)
def get_cost_analysis(scenario_id: str, version_id: str) -> CostAnalysis:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    try:
        return cost_analysis(plan)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/capacity-analysis",
    response_model=CapacityAnalysis,
)
def post_capacity_analysis(
    scenario_id: str,
    version_id: str,
    request: CapacityAnalysisRequest,
) -> CapacityAnalysis:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    try:
        return capacity_analysis(plan, request, require_store().travel_provider)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/risk-simulation",
    response_model=RiskSimulationResult,
)
def post_risk_simulation(
    scenario_id: str,
    version_id: str,
    request: RiskSimulationRequest,
) -> RiskSimulationResult:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    try:
        return simulate_plan_risk(plan, request)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


def schedule_change_rows(
    before: ScheduleResult,
    after: ScheduleResult,
) -> list[dict[str, str | int | None]]:
    before_by_id = {item.work_order_id: item for item in before.assignments}
    after_by_id = {item.work_order_id: item for item in after.assignments}
    changed: list[dict[str, str | int | None]] = []
    for order_id in sorted(set(before_by_id) | set(after_by_id)):
        old = before_by_id.get(order_id)
        new = after_by_id.get(order_id)
        if (
            not old
            or not new
            or old.technician_id != new.technician_id
            or old.sequence != new.sequence
            or old.start_time != new.start_time
        ):
            changed.append(
                {
                    "work_order_id": order_id,
                    "before_technician": old.technician_id if old else None,
                    "after_technician": new.technician_id if new else None,
                    "before_start": old.start_time if old else None,
                    "after_start": new.start_time if new else None,
                    "reason": "技师、顺序或到场时间发生变化" if old and new else "工单进入或离开可执行计划",
                }
            )
    return changed


def build_rollback_preview(
    current: ScheduleScenario,
    source: PlanVersion,
    current_plan: PlanVersion | None = None,
    execution_events: list[WorkOrderExecutionEvent] | None = None,
) -> RollbackPreview:
    assert source.scenario_snapshot is not None
    target = source.scenario_snapshot
    current_orders = {item.id: item for item in current.work_orders}
    target_orders = {item.id: item for item in target.work_orders}
    added = sorted(set(target_orders) - set(current_orders))
    removed = sorted(set(current_orders) - set(target_orders))
    modified = sorted(
        order_id
        for order_id in set(current_orders) & set(target_orders)
        if current_orders[order_id].model_dump(mode="json") != target_orders[order_id].model_dump(mode="json")
    )
    reopened = sorted(
        order_id
        for order_id, order in current_orders.items()
        if order.status is WorkOrderStatus.completed
        and (order_id not in target_orders or target_orders[order_id].status is not WorkOrderStatus.completed)
    )
    started_reopened = sorted(
        order_id
        for order_id, order in current_orders.items()
        if order.status is WorkOrderStatus.started
        and (order_id not in target_orders or target_orders[order_id].status is not WorkOrderStatus.started)
    )
    events = execution_events or []
    executed_deleted = sorted({event.work_order_id for event in events if event.work_order_id not in target_orders})
    affected_event_ids = sorted(
        event.id
        for event in events
        if (
            event.work_order_id not in target_orders
            or current_orders.get(event.work_order_id) is None
            or current_orders[event.work_order_id].status != target_orders[event.work_order_id].status
        )
    )
    current_technicians = {item.id: item.model_dump(mode="json") for item in current.technicians}
    target_technicians = {item.id: item.model_dump(mode="json") for item in target.technicians}
    technician_changes = sorted(
        technician_id
        for technician_id in set(current_technicians) | set(target_technicians)
        if current_technicians.get(technician_id) != target_technicians.get(technician_id)
    )
    current_locks = {item.work_order_id: item.technician_id for item in current.locked_assignments}
    target_locks = {item.work_order_id: item.technician_id for item in target.locked_assignments}
    lock_changes = sorted(
        work_order_id
        for work_order_id in set(current_locks) | set(target_locks)
        if current_locks.get(work_order_id) != target_locks.get(work_order_id)
    )
    token = content_hash(
        {
            "scenario_id": current.id,
            "expected_revision": current.revision,
            "current_hash": content_hash(current),
            "source_version_id": source.id,
            "source_hash": source.scenario_snapshot_hash or content_hash(target),
            "current_plan_version_id": current_plan.id if current_plan else None,
            "execution_event_ids": [event.id for event in events],
        }
    )
    return RollbackPreview(
        scenario_id=current.id,
        source_version_id=source.id,
        expected_revision=current.revision,
        confirmation_token=token,
        current_plan_version_id=current_plan.id if current_plan else None,
        current_plan_number=current_plan.number if current_plan else None,
        changed_plan_work_orders=[
            str(item["work_order_id"]) for item in schedule_change_rows(current_plan.selected, source.selected)
        ]
        if current_plan
        else [],
        added_work_orders=added,
        removed_work_orders=removed,
        modified_work_orders=modified,
        completed_work_orders_reopened=reopened,
        started_work_orders_reopened=started_reopened,
        executed_work_orders_deleted=executed_deleted,
        affected_execution_event_ids=affected_event_ids,
        technician_changes=technician_changes,
        lock_changes=lock_changes,
    )


@router.post("/api/scenarios/{scenario_id}/plan-versions/{version_id}/activate", response_model=PlanVersion)
def activate_plan_version(
    scenario_id: str,
    version_id: str,
    request: ActivatePlanRequest,
) -> PlanVersion:
    current = require_scenario(scenario_id)
    if any(order.status is WorkOrderStatus.started for order in current.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTION_CONTEXT_REQUIRED",
                "message": "存在服务中的工单，不能重新激活历史计划；请使用局部重排",
            },
        )
    if current.revision != request.expected_revision:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": request.expected_revision,
                "current_revision": current.revision,
            },
        )
    source = require_store().get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    current_fingerprint = content_hash(current.model_dump(exclude={"revision"}))
    source_fingerprint = content_hash(source.scenario_snapshot.model_dump(exclude={"revision"}))
    if current_fingerprint != source_fingerprint:
        store = require_store()
        current_plan = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
        preview = build_rollback_preview(current, source, current_plan)
        raise HTTPException(
            409,
            detail={
                "message": "历史计划使用的业务数据与当前场景不同，不能直接激活",
                "added_work_orders": preview.added_work_orders,
                "removed_work_orders": preview.removed_work_orders,
                "modified_work_orders": preview.modified_work_orders,
            },
        )
    publication_key, fingerprint, existing = publication_retry("activate", current, request.idempotency_key, request)
    if existing:
        return existing
    selected = source.selected.model_copy(deep=True)
    selected.id = f"SCH-{scenario_id}-activate-{uuid.uuid4().hex[:8]}"
    selected.created_at = _now()
    selected.source_schedule_id = source.selected.id
    selected.scenario_revision = current.revision
    selected.solution_found = True
    selected = bind_replayed_solver_policy(selected, current, "plan-activation")
    selected = normalize_schedule(
        current,
        selected,
        source.selected if selected.kind == "replan" else None,
        provider=require_store().travel_provider,
    )
    run = start_schedule_run(
        current,
        "activate",
        source=source,
        solver_name="plan-activation",
        solver_config_hash=selected.solver_config_hash,
    )
    published = publish_selected(
        current,
        selected,
        "activate",
        artifacts=[artifact("selected", selected, selected.strategy)],
        source=source,
        relation="reactivated_from",
        label=f"重新激活 V{source.number:03d} · {source.label}"[:60].strip(),
        run=run,
        idempotency_key=publication_key,
        request_fingerprint=fingerprint,
    )
    plan = require_store().active_plan_version(scenario_id)
    if not plan or plan.selected.id != published.id:
        raise HTTPException(500, "计划已发布但无法读取新版本")
    return plan


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/clone-scenario",
    response_model=ScheduleScenario,
    status_code=201,
)
def clone_plan_scenario(
    scenario_id: str,
    version_id: str,
    request: CloneScenarioRequest,
) -> ScheduleScenario:
    require_scenario(scenario_id)
    source = require_store().get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    clone = source.scenario_snapshot.model_copy(deep=True)
    clone.id = f"clone-{scenario_id}-{uuid.uuid4().hex[:8]}"
    clone.name = request.name
    clone.source_scenario_id = scenario_id
    clone.revision = 0
    for order in clone.work_orders:
        if order.status in {WorkOrderStatus.started, WorkOrderStatus.completed}:
            order.status = WorkOrderStatus.pending
    namespace = f"{scenario_id}:clone-scenario"
    fingerprint = content_hash({"source_version_id": source.id, "name": request.name})
    try:
        return require_store().clone_scenario_idempotently(
            clone,
            namespace=namespace,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            source_version_id=source.id,
        )
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error


@router.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}/rollback-preview", response_model=RollbackPreview)
def rollback_plan_preview(scenario_id: str, version_id: str) -> RollbackPreview:
    current = require_scenario(scenario_id)
    store = require_store()
    source = store.get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    current_plan = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
    return build_rollback_preview(
        current,
        source,
        current_plan,
        store.list_execution_events(scenario_id),
    )


@router.post("/api/scenarios/{scenario_id}/plan-versions/{version_id}/restore", response_model=PlanVersion)
def restore_plan_version(scenario_id: str, version_id: str, request: RestoreRequest) -> PlanVersion:
    current = require_scenario(scenario_id)
    if any(order.status is WorkOrderStatus.started for order in current.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTION_CONTEXT_REQUIRED",
                "message": "存在服务中的工单，不能回滚业务快照",
            },
        )
    source = require_store().get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    publication_key = f"{scenario_id}:rollback:{request.idempotency_key}"
    fingerprint = content_hash(
        {
            "scenario_id": scenario_id,
            "action": "rollback",
            "source_version_id": source.id,
            "request": request,
        }
    )
    try:
        existing = require_store().published_for_key(publication_key, fingerprint)
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error
    if existing:
        return existing
    if current.revision != request.expected_revision:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已变化，请刷新后重新确认恢复",
                "expected_revision": request.expected_revision,
                "current_revision": current.revision,
            },
        )
    store = require_store()
    current_plan = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
    preview = build_rollback_preview(
        current,
        source,
        current_plan,
        store.list_execution_events(scenario_id),
    )
    if request.confirmation_token != preview.confirmation_token:
        raise HTTPException(409, "回滚确认已过期，请重新查看差异")
    if preview.completed_work_orders_reopened:
        raise HTTPException(
            409,
            detail={
                "message": "回滚会重新打开已有执行事件的已完成工单，禁止执行",
                "completed_work_orders_reopened": preview.completed_work_orders_reopened,
            },
        )
    if preview.started_work_orders_reopened:
        raise HTTPException(
            409,
            detail={
                "message": "回滚会重新打开服务中的工单，禁止执行",
                "started_work_orders_reopened": preview.started_work_orders_reopened,
            },
        )
    if preview.executed_work_orders_deleted:
        raise HTTPException(
            409,
            detail={
                "message": "回滚会删除已有执行记录的工单，禁止执行",
                "executed_work_orders_deleted": preview.executed_work_orders_deleted,
                "affected_execution_event_ids": preview.affected_execution_event_ids,
            },
        )
    if preview.removed_work_orders and not request.allow_delete_new_orders:
        raise HTTPException(
            409,
            detail={
                "message": "回滚会删除历史版本之后新增的工单，默认禁止",
                "removed_work_orders": preview.removed_work_orders,
            },
        )
    restored = source.scenario_snapshot.model_copy(deep=True)
    restored.id = scenario_id
    restored.revision = current.revision + 1
    selected = source.selected.model_copy(deep=True)
    selected.id = f"SCH-{scenario_id}-restore-{uuid.uuid4().hex[:8]}"
    selected.created_at = _now()
    selected.source_schedule_id = source.selected.id
    selected.scenario_revision = restored.revision
    selected.solution_found = True
    selected = bind_replayed_solver_policy(selected, restored, "plan-restore")
    selected = normalize_schedule(restored, selected, provider=require_store().travel_provider)
    run = start_schedule_run(
        restored,
        "restore",
        source=source,
        solver_name="plan-restore",
        solver_config_hash=selected.solver_config_hash,
    )
    verification = validate_result(restored, selected)
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}",
        run_id=run.id,
        scenario_id=scenario_id,
        scenario_revision=restored.revision,
        scenario_snapshot_hash=content_hash(restored),
        source_plan_version_id=source.id,
        solver_config_hash=selected.solver_config_hash,
        solver_policy_fingerprint=selected.solver_policy.fingerprint if selected.solver_policy else "",
        schedule=selected,
        verification_report=verification,
        publishable=verification.publishable,
        created_at=_now(),
    )
    run.status = run_status_for_result(selected)
    run.solution_found = selected.solution_found
    run.termination_reason = "RESTORED_FROM_VERIFIED_PLAN"
    run.finished_at = _now()
    run.candidate_id = candidate.id
    run.solver_name = "plan-restore"
    run.solver_version = "1"
    run.solver_policy_fingerprint = selected.solver_policy.fingerprint if selected.solver_policy else ""
    require_store().complete_schedule_run(run, candidate)
    if not candidate.publishable:
        raise HTTPException(
            422,
            detail={
                "message": "历史方案未通过当前发布验证",
                "run_id": run.id,
                "candidate_id": candidate.id,
                "errors": [item.model_dump() for item in verification.errors],
            },
        )
    rollback_label = f"业务回滚自 V{source.number:03d} · {source.label} · {request.reason}"[:60].strip()
    try:
        return require_store().publish_plan(
            restored,
            selected,
            "restore",
            artifacts=[artifact("selected", selected, selected.strategy)],
            source_version_id=source.id,
            relation="restored_from",
            label=rollback_label,
            replace_scenario=True,
            expected_revision=request.expected_revision,
            idempotency_key=publication_key,
            request_fingerprint=fingerprint,
            candidate_id=candidate.id,
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已变化，请刷新后重新确认恢复",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error


def build_comparison(
    scenario_id: str,
    before: ScheduleResult,
    after: ScheduleResult,
    before_snapshot: ScheduleScenario | None = None,
    after_snapshot: ScheduleScenario | None = None,
) -> Comparison:
    changed = schedule_change_rows(before, after)
    before_orders = {item.id: item for item in before_snapshot.work_orders} if before_snapshot else {}
    after_orders = {item.id: item for item in after_snapshot.work_orders} if after_snapshot else {}
    added = sorted(set(after_orders) - set(before_orders))
    removed = sorted(set(before_orders) - set(after_orders))
    modified = sorted(
        order_id
        for order_id in set(before_orders) & set(after_orders)
        if before_orders[order_id].model_dump(mode="json") != after_orders[order_id].model_dump(mode="json")
    )
    same_snapshot = bool(
        before_snapshot
        and after_snapshot
        and content_hash(before_snapshot.model_dump(exclude={"revision"}))
        == content_hash(after_snapshot.model_dump(exclude={"revision"}))
    )
    common_technicians = (
        sorted({item.id for item in before_snapshot.technicians} & {item.id for item in after_snapshot.technicians})
        if before_snapshot and after_snapshot
        else []
    )
    b, a = before.kpis, after.kpis
    return Comparison(
        scenario_id=scenario_id,
        before=before,
        after=after,
        delta={
            "objective": round(after.objective - before.objective, 2)
            if same_snapshot and after.strategy == before.strategy
            else None,
            "sla_late_count": a.sla_late_count - b.sla_late_count,
            "travel_minutes": a.total_travel_minutes - b.total_travel_minutes,
            "overtime_minutes": a.total_overtime_minutes - b.total_overtime_minutes,
            "unassigned_count": a.unassigned_count - b.unassigned_count,
            "completion_rate": round(a.completion_rate - b.completion_rate, 4),
            "stability_rate": a.stability_rate,
        },
        changed_orders=changed,
        comparable=same_snapshot,
        same_scenario_snapshot=same_snapshot,
        common_work_order_count=len(set(before_orders) & set(after_orders)),
        added_work_orders=added,
        removed_work_orders=removed,
        modified_work_orders=modified,
        common_technicians=common_technicians,
    )


@router.get("/api/scenarios/{scenario_id}/comparison", response_model=Comparison)
def comparison(scenario_id: str, before: str | None = None, after: str | None = None) -> Comparison:
    require_scenario(scenario_id)
    store = require_store()
    if before and not after:
        raise HTTPException(422, "指定比较起点时也必须指定终点")
    if after:
        after_plan = store.get_plan_version(scenario_id, after)
        before_plan = store.get_plan_version(scenario_id, before) if before else None
        if before and not before_plan:
            raise HTTPException(404, "用于比较的起点方案版本不存在")
        if not after_plan:
            raise HTTPException(404, "用于比较的终点方案版本不存在")
        if before_plan:
            return build_comparison(
                scenario_id,
                before_plan.selected,
                after_plan.selected,
                before_plan.scenario_snapshot,
                after_plan.scenario_snapshot,
            )
        internal_baseline = next(
            (item.schedule for item in after_plan.artifacts if item.role == "baseline"),
            None,
        )
        baseline_plan = next(
            (
                item
                for item in reversed(store.list_plan_versions(scenario_id, include_snapshots=True))
                if item.action == "baseline"
                and item.number < after_plan.number
                and item.data_revision == after_plan.data_revision
            ),
            None,
        )
        before_result = internal_baseline or (baseline_plan.selected if baseline_plan else None)
        if not before_result:
            raise HTTPException(409, "指定方案没有可比较的基线")
        return build_comparison(
            scenario_id,
            before_result,
            after_plan.selected,
            after_plan.scenario_snapshot,
            after_plan.scenario_snapshot,
        )
    plans = store.list_plan_versions(scenario_id, include_snapshots=True)
    if not plans:
        raise HTTPException(409, "请先生成至少一个方案")
    after_plan = store.active_plan_version(scenario_id) or next(
        (item for item in reversed(plans) if item.action != "baseline"),
        plans[-1],
    )
    internal_baseline = next((item.schedule for item in after_plan.artifacts if item.role == "baseline"), None)
    before_result = internal_baseline or next(
        (item.selected for item in reversed(plans) if item.action == "baseline" and item.number < after_plan.number),
        None,
    )
    if not before_result:
        raise HTTPException(409, "当前方案没有可比较的基线")
    return build_comparison(
        scenario_id,
        before_result,
        after_plan.selected,
        after_plan.scenario_snapshot,
        after_plan.scenario_snapshot,
    )


@router.get("/api/strategy-profiles", response_model=list[StrategyProfile])
def list_strategy_profiles(include_stable: bool = True) -> list[StrategyProfile]:
    return require_store().list_profiles(include_stable)


@router.post("/api/strategy-profiles", response_model=StrategyProfile, status_code=201)
def create_strategy_profile(request: StrategyProfileCreate) -> StrategyProfile:
    return require_store().save_profile(request)


@router.put("/api/strategy-profiles/{profile_id}", response_model=StrategyProfile)
def update_strategy_profile(profile_id: str, request: StrategyProfileCreate) -> StrategyProfile:
    if not require_store().get_profile(profile_id):
        raise HTTPException(404, "策略不存在")
    try:
        return require_store().save_profile(request, profile_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.delete("/api/strategy-profiles/{profile_id}", status_code=204)
def delete_strategy_profile(profile_id: str) -> Response:
    try:
        deleted = require_store().delete_profile(profile_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not deleted:
        raise HTTPException(404, "策略不存在")
    return Response(status_code=204)


def common_evaluation_score(scenario: ScheduleScenario, result: ScheduleResult) -> float:
    order_map = {order.id: order for order in scenario.work_orders}
    total_shift = max(1, sum(item.shift_end - item.shift_start for item in scenario.technicians))
    total_service = max(
        1, sum(item.service_duration for item in scenario.work_orders if item.status != WorkOrderStatus.completed)
    )
    total_penalty = max(
        1, sum(item.drop_penalty for item in scenario.work_orders if item.status != WorkOrderStatus.completed)
    )
    unassigned = sum(
        order_map[item.work_order_id].drop_penalty for item in result.unassigned if item.work_order_id in order_map
    )
    changes = sum(1 for item in result.assignments if item.changed)
    active_count = max(1, len([item for item in scenario.work_orders if item.status != WorkOrderStatus.completed]))
    normalized = (
        result.kpis.total_travel_minutes / total_shift * 0.20
        + result.kpis.total_late_minutes / total_service * 0.25
        + result.kpis.total_overtime_minutes / total_shift * 0.15
        + result.kpis.normalized_workload_range * 0.10
        + unassigned / total_penalty * 0.25
        + changes / active_count * 0.05
    )
    return round(normalized * 1000, 2)


def mark_pareto_candidates(candidates: list[StrategyCandidate]) -> None:
    def values(candidate: StrategyCandidate) -> tuple[float, ...]:
        kpis = candidate.schedule.kpis
        return (
            1 - kpis.completion_rate,
            1 - kpis.committed_on_time_rate,
            float(kpis.total_travel_minutes),
            float(kpis.total_overtime_minutes),
            kpis.normalized_workload_range,
        )

    tolerance = 1e-9
    for candidate in candidates:
        dominated_by: list[str] = []
        candidate_values = values(candidate)
        for other in candidates:
            if other.id == candidate.id:
                continue
            other_values = values(other)
            no_worse = all(
                left <= right + tolerance for left, right in zip(other_values, candidate_values, strict=False)
            )
            strictly_better = any(
                left < right - tolerance for left, right in zip(other_values, candidate_values, strict=False)
            )
            if no_worse and strictly_better:
                dominated_by.append(other.id)
        candidate.dominated_by = dominated_by
        candidate.pareto_optimal = not dominated_by


def _run_experiment(experiment_id: str, override_limit: float | None) -> None:
    experiment = require_store().get_experiment(experiment_id)
    if not experiment or not experiment.scenario_snapshot:
        return
    try:
        if experiment.status == "CANCEL_REQUESTED":
            experiment.status = "CANCELLED"
            experiment.error = "实验已由用户取消"
            experiment.finished_at = _now()
            require_store().save_experiment(experiment)
            return
        scenario = experiment.scenario_snapshot
        experiment.status = "RUNNING"
        require_store().save_experiment(experiment)
        profiles = experiment.profile_snapshots
        candidates: list[StrategyCandidate] = []
        total = max(1, len(profiles))
        for index, frozen_profile in enumerate(profiles):
            latest = require_store().get_experiment(experiment_id)
            if latest and latest.status == "CANCEL_REQUESTED":
                experiment.status = "CANCELLED"
                experiment.cancel_requested_at = latest.cancel_requested_at
                experiment.finished_at = _now()
                experiment.error = "实验已由用户取消"
                experiment.candidates = candidates
                require_store().save_experiment(experiment)
                return
            profile = frozen_profile.model_copy(deep=True)
            if override_limit is not None:
                profile.time_limit_seconds = override_limit
            effective = scenario_for_profile(scenario, profile)
            strategy_key = profile.id if profile.builtin else "custom"
            run = start_schedule_run(
                scenario,
                "experiment",
                requested_time_limit_seconds=profile.time_limit_seconds,
                solver_config_hash=content_hash(effective.solver_config),
            )
            try:
                baseline = baseline_schedule(
                    effective,
                    0,
                    strategy_key,
                    provider=require_store().travel_provider,
                )
                result = optimized_schedule(
                    effective,
                    0,
                    previous=baseline,
                    time_limit_seconds=profile.time_limit_seconds,
                    strategy=strategy_key,
                    provider=require_store().travel_provider,
                )
                result = bind_solver_policy(result, effective, profile, strategy_key, scenario)
                result = normalize_schedule(
                    scenario,
                    result,
                    provider=require_store().travel_provider,
                    solver_config_hash=content_hash(effective.solver_config),
                )
                verification = verify_schedule(scenario, result, provider=require_store().travel_provider)
                schedule_candidate = ScheduleCandidate(
                    id=f"CAND-{uuid.uuid4().hex[:12]}",
                    run_id=run.id,
                    scenario_id=scenario.id,
                    scenario_revision=scenario.revision,
                    scenario_snapshot_hash=content_hash(scenario),
                    solver_config_hash=result.solver_config_hash,
                    schedule=result,
                    solver_policy_fingerprint=result.solver_policy.fingerprint if result.solver_policy else "",
                    verification_report=verification,
                    publishable=verification.publishable,
                    created_at=_now(),
                )
                run.status = run_status_for_result(result)
                run.termination_reason = result.termination_reason
                run.solution_found = result.solution_found
                run.finished_at = _now()
                run.candidate_id = schedule_candidate.id
                run.solver_name = result.solver_name
                run.solver_version = result.solver_version
                run.solver_config_hash = result.solver_config_hash
                run.solver_policy_fingerprint = result.solver_policy.fingerprint if result.solver_policy else ""
                run.requested_time_limit_ms = result.requested_time_limit_ms or run.requested_time_limit_ms
                run.effective_time_limit_ms = result.effective_time_limit_ms or run.effective_time_limit_ms
                require_store().complete_schedule_run(run, schedule_candidate)
                candidate = StrategyCandidate(
                    id=f"SC-{uuid.uuid4().hex[:10]}",
                    profile_id=profile.id,
                    profile_name=profile.name,
                    schedule=result,
                    evaluation_score=common_evaluation_score(scenario, result),
                    publishable=schedule_candidate.publishable,
                    schedule_candidate_id=schedule_candidate.id,
                    verification_report=verification,
                )
                candidates.append(candidate)
                if not schedule_candidate.publishable:
                    codes = ", ".join(item.code for item in verification.errors) or "NOT_PUBLISHABLE"
                    experiment.candidate_errors[profile.id] = f"候选未通过发布校验：{codes}"
            except Exception as error:
                fail_schedule_run(run, f"{type(error).__name__}: {error}")
                experiment.candidate_errors[profile.id] = f"{type(error).__name__}: {error}"
            experiment.candidates = candidates
            experiment.progress = round((index + 1) / total * 90)
            latest = require_store().get_experiment(experiment_id)
            if latest and latest.status == "CANCEL_REQUESTED":
                experiment.status = "CANCELLED"
                experiment.cancel_requested_at = latest.cancel_requested_at
                experiment.finished_at = _now()
                experiment.error = "实验已由用户取消"
                require_store().save_experiment(experiment)
                return
            require_store().save_experiment(experiment)
        publishable_candidates = [item for item in candidates if item.publishable]
        if publishable_candidates:
            mark_pareto_candidates(publishable_candidates)
            for candidate in candidates:
                if not candidate.publishable:
                    candidate.pareto_optimal = False
            metrics = {
                "计划覆盖最佳": max(item.schedule.kpis.completion_rate for item in publishable_candidates),
                "最准时": max(item.schedule.kpis.committed_on_time_rate for item in publishable_candidates),
                "最短行程": min(item.schedule.kpis.total_travel_minutes for item in publishable_candidates),
                "最少加班": min(item.schedule.kpis.total_overtime_minutes for item in publishable_candidates),
                "最公平": min(item.schedule.kpis.normalized_workload_range for item in publishable_candidates),
                "对比得分最低": min(item.evaluation_score for item in publishable_candidates),
            }
            for candidate in publishable_candidates:
                values = {
                    "计划覆盖最佳": candidate.schedule.kpis.completion_rate,
                    "最准时": candidate.schedule.kpis.committed_on_time_rate,
                    "最短行程": candidate.schedule.kpis.total_travel_minutes,
                    "最少加班": candidate.schedule.kpis.total_overtime_minutes,
                    "最公平": candidate.schedule.kpis.normalized_workload_range,
                    "对比得分最低": candidate.evaluation_score,
                }
                candidate.advantages = [label for label, value in values.items() if abs(value - metrics[label]) <= 1e-9]
        if not publishable_candidates:
            experiment.status = "FAILED"
            experiment.error = "所有策略均未产生可发布候选"
        elif experiment.candidate_errors:
            experiment.status = "COMPLETED_WITH_ERRORS"
            experiment.error = "部分策略失败，可继续评估已成功候选"
        else:
            experiment.status = "COMPLETED"
        experiment.progress = 100
        experiment.candidates = candidates
        experiment.finished_at = _now()
    except Exception as error:  # keep the local UI actionable instead of losing the job
        experiment.status = "FAILED"
        experiment.error = str(error)
        experiment.progress = 100
        experiment.finished_at = _now()
    require_store().save_experiment(experiment)


def _run_experiment_with_slot(
    experiment_id: str,
    override_limit: float | None,
    slot: threading.BoundedSemaphore,
) -> None:
    try:
        _run_experiment(experiment_id, override_limit)
    finally:
        slot.release()


@router.post(
    "/api/scenarios/{scenario_id}/strategy-experiments",
    response_model=StrategyExperiment,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_strategy_experiment(scenario_id: str, request: StrategyExperimentRequest) -> StrategyExperiment:
    target_id = scenario_id if request.dataset == "current" else request.dataset
    scenario = require_scenario(target_id)
    if any(order.status is not WorkOrderStatus.pending for order in scenario.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTION_CONTEXT_REQUIRED",
                "message": "场景已有执行记录，策略实验暂不支持普通优化；请使用局部重排",
            },
        )
    profile_ids = request.profile_ids or [profile.id for profile in require_store().list_profiles(include_stable=False)]
    if len(profile_ids) > 8:
        raise HTTPException(422, "一次实验最多选择 8 个策略")
    profiles = [require_profile(profile_id).model_copy(deep=True) for profile_id in profile_ids]
    scenario_snapshot_hash = content_hash(scenario)
    experiment_fingerprint = content_hash(
        {
            "scenario_snapshot_hash": scenario_snapshot_hash,
            "profiles": [profile.model_dump(mode="json") for profile in profiles],
            "time_limit_seconds": request.time_limit_seconds,
            "solver_version": ortools.__version__,
            "travel_model_version": require_store().travel_provider.version,
            "travel_model_fingerprint": require_store().travel_provider.fingerprint,
            "score_policy_version": "FIELD_SERVICE_SCORE_V2",
            "seed": scenario.seed,
        }
    )
    experiment = StrategyExperiment(
        id=f"EXP-{uuid.uuid4().hex[:10]}",
        scenario_id=target_id,
        dataset=request.dataset,
        data_revision=scenario.revision,
        status="QUEUED",
        progress=0,
        created_at=_now(),
        profile_ids=profile_ids,
        requested_time_limit_seconds=request.time_limit_seconds,
        scenario_snapshot=scenario.model_copy(deep=True),
        profile_snapshots=profiles,
        fingerprint=experiment_fingerprint,
        scenario_snapshot_hash=scenario_snapshot_hash,
        score_policy_version="FIELD_SERVICE_SCORE_V2",
        travel_model_version=require_store().travel_provider.version,
        travel_model_fingerprint=require_store().travel_provider.fingerprint,
        solver_version=ortools.__version__,
    )
    if experiment_executor is None or experiment_slots is None:
        raise HTTPException(503, "策略实验执行器未启动")
    existing = require_store().active_experiment_by_fingerprint(target_id, experiment_fingerprint)
    if existing:
        return existing.model_copy(update={"scenario_snapshot": None})
    slot = experiment_slots
    if not slot.acquire(blocking=False):
        raise HTTPException(429, f"策略实验队列已满（最多 {EXPERIMENT_QUEUE_CAPACITY} 个），请稍后再试")
    queued, created = require_store().queue_experiment(experiment)
    if not created:
        slot.release()
        return queued.model_copy(update={"scenario_snapshot": None})
    try:
        experiment_executor.submit(
            _run_experiment_with_slot,
            queued.id,
            request.time_limit_seconds,
            slot,
        )
    except Exception as error:
        slot.release()
        queued.status = "FAILED"
        queued.error = f"实验排队失败：{error}"
        queued.progress = 100
        queued.finished_at = _now()
        require_store().save_experiment(queued)
        raise HTTPException(503, queued.error) from error
    return queued.model_copy(update={"scenario_snapshot": None})


@router.get("/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}", response_model=StrategyExperiment)
def get_strategy_experiment(scenario_id: str, experiment_id: str) -> StrategyExperiment:
    experiment = require_store().get_experiment(experiment_id)
    if not experiment or experiment.scenario_id != scenario_id:
        raise HTTPException(404, "策略实验不存在")
    return experiment.model_copy(update={"scenario_snapshot": None})


@router.post(
    "/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}/cancel",
    response_model=StrategyExperiment,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_strategy_experiment(scenario_id: str, experiment_id: str) -> StrategyExperiment:
    experiment = require_store().get_experiment(experiment_id)
    if not experiment or experiment.scenario_id != scenario_id:
        raise HTTPException(404, "策略实验不存在")
    cancelled = require_store().request_experiment_cancel(experiment_id)
    assert cancelled is not None
    return cancelled.model_copy(update={"scenario_snapshot": None})


@router.post("/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}/publish", response_model=PlanVersion)
def publish_strategy_candidate(scenario_id: str, experiment_id: str, request: ExperimentPublishRequest) -> PlanVersion:
    scenario = require_scenario(scenario_id)
    if any(order.status is not WorkOrderStatus.pending for order in scenario.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTION_CONTEXT_REQUIRED",
                "message": "存在服务中的工单，不能发布普通策略实验候选",
            },
        )
    experiment = require_store().get_experiment(experiment_id)
    if not experiment or experiment.scenario_id != scenario_id:
        raise HTTPException(404, "策略实验不存在")
    if experiment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise HTTPException(409, "策略实验尚未完成")
    if experiment.winner_candidate_id and experiment.winner_candidate_id != request.candidate_id:
        raise HTTPException(409, "该实验已发布其他候选，一次实验只能选定一个方案")
    if scenario.revision != request.expected_revision or experiment.data_revision != scenario.revision:
        raise HTTPException(
            409,
            detail={
                "message": "实验完成后业务数据已变化，请重新运行",
                "experiment_revision": experiment.data_revision,
                "current_revision": scenario.revision,
            },
        )
    candidate = next((item for item in experiment.candidates if item.id == request.candidate_id), None)
    if not candidate:
        raise HTTPException(404, "候选方案不存在")
    if not candidate.publishable:
        raise HTTPException(409, "该候选没有可发布的可行方案")
    verification = validate_result(scenario, candidate.schedule)
    if not verification.publishable:
        raise HTTPException(
            409,
            detail={
                "message": "候选方案未通过当前发布验证",
                "errors": [item.model_dump() for item in verification.errors],
            },
        )
    schedule_candidate = (
        require_store().get_schedule_candidate(candidate.schedule_candidate_id)
        if candidate.schedule_candidate_id
        else None
    )
    if not schedule_candidate:
        raise HTTPException(409, "候选方案缺少可追溯的求解记录，请重新运行实验")
    source = require_store().active_plan_version(scenario_id)
    try:
        published = require_store().publish_plan(
            scenario,
            candidate.schedule,
            "experiment_publish",
            artifacts=[artifact("selected", candidate.schedule, candidate.profile_id)],
            source_version_id=source.id if source else None,
            relation="published_from_experiment",
            label=candidate.profile_name,
            expected_revision=request.expected_revision,
            idempotency_key=f"strategy-experiment:{experiment.id}",
            request_fingerprint=candidate.id,
            candidate_id=schedule_candidate.id,
            experiment_id=experiment.id,
            experiment_candidate_id=candidate.id,
        )
        return published
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "message": "业务数据已变化，请重新运行策略实验",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error


@router.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}/report", response_class=HTMLResponse)
def version_report(scenario_id: str, version_id: str) -> HTMLResponse:
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan or not plan.scenario_snapshot:
        raise HTTPException(404, "方案报告不存在")
    safe_scenario_id = safe_filename_component(scenario_id)
    return HTMLResponse(
        build_report(plan.scenario_snapshot, plan.selected),
        headers={"Content-Disposition": f'inline; filename="fieldflow-{safe_scenario_id}-V{plan.number:03d}.html"'},
    )


@router.get("/api/scenarios/{scenario_id}/report", response_class=HTMLResponse)
def report(scenario_id: str, schedule_id: str | None = None) -> HTMLResponse:
    scenario = require_scenario(scenario_id)
    if schedule_id:
        result = require_store().get_schedule(schedule_id)
        plan = next(
            (
                item
                for item in require_store().list_plan_versions(scenario_id, include_snapshots=True)
                if item.selected.id == schedule_id
            ),
            None,
        )
    else:
        plan = require_store().active_plan_version(scenario_id)
        result = plan.selected if plan else None
    if not result or result.scenario_id != scenario_id:
        raise HTTPException(404, "当前没有可导出的方案")
    snapshot = plan.scenario_snapshot if plan and plan.scenario_snapshot else scenario
    safe_scenario_id = safe_filename_component(scenario_id)
    return HTMLResponse(
        build_report(snapshot, result),
        headers={"Content-Disposition": f'inline; filename="fieldflow-{safe_scenario_id}-V{result.version:03d}.html"'},
    )


FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}


def browser_origin_allowed(
    request: Request,
    origin: str,
    configured_origins: set[str],
) -> bool:
    normalized = origin.rstrip("/")
    if normalized in configured_origins:
        return True
    parsed = urlsplit(normalized)
    request_host = request.headers.get("host", "").lower()
    return (
        parsed.scheme in {"http", "https"}
        and parsed.scheme == request.url.scheme
        and parsed.netloc.lower() == request_host
    )


def create_app(
    *,
    db_path: str | Path | None = None,
    store_override: Store | None = None,
) -> FastAPI:
    application = FastAPI(
        title="FieldFlow API",
        description="本地现场服务排程接口",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.db_path = Path(db_path) if db_path is not None else DB_PATH
    application.state.store_override = store_override
    allowed_hosts = [
        item.strip()
        for item in os.getenv("FIELDFLOW_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
        if item.strip()
    ]
    configured_origins = {
        item.strip().rstrip("/")
        for item in os.getenv("FIELDFLOW_ALLOWED_ORIGINS", ",".join(sorted(ALLOWED_ORIGINS))).split(",")
        if item.strip()
    }
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(configured_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def validate_browser_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and not browser_origin_allowed(request, origin, configured_origins)
        ):
            return Response("不允许的请求来源", status_code=403)
        return await call_next(request)

    application.include_router(router)
    if FRONTEND_DIST.exists():
        application.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return application


app = create_app()
