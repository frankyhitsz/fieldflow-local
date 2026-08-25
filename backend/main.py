from __future__ import annotations

import os
import random
import re
import statistics
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit

import ortools
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ._version import __version__
from .decision import (
    DecisionAnalysisError,
    build_simulation_scenario_set,
    capacity_analysis,
    cost_analysis,
    schedule_signature,
    simulate_plan_risk,
    validate_frozen_plan_integrity,
)
from .execution import execution_context_for_planning
from .fixtures import get_fixture
from .hashing import content_hash
from .models import (
    ActivatePlanRequest,
    AnalysisIntegrityStatus,
    CapacityAnalysis,
    CapacityAnalysisRequest,
    CapacityCounterfactualArtifact,
    CapacityDecisionStatus,
    CloneScenarioRequest,
    Comparison,
    CostAnalysis,
    DecisionAnalysisArtifact,
    DecisionAnalysisContext,
    DecisionAnalysisRun,
    DecisionAnalysisRunRequest,
    DecisionAnalysisScope,
    ExecutionSourceContext,
    ExperimentPublishRequest,
    FieldImpact,
    FreezeReason,
    FrozenAssignment,
    LockedAssignment,
    LockRequest,
    ManualReassignmentRequest,
    ManualReassignmentResult,
    OptimizeRequest,
    PairedMetricSummary,
    PlanningContext,
    PlanningReservation,
    PlanUseCase,
    PlanVersion,
    PlanVersionPatch,
    PublicationPlanningContext,
    ReattestationMode,
    ReattestPlanRequest,
    ReplanRequest,
    RestoreRequest,
    RiskComparisonResult,
    RiskComparisonRun,
    RiskSimulationRequest,
    RiskSimulationResult,
    RiskTrialOutcomeArtifact,
    RollbackPreview,
    ScenarioCreate,
    ScenarioOperationalView,
    ScheduleArtifact,
    ScheduleCandidate,
    ScheduleResult,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleScenario,
    SimulationEmergencyEvent,
    SimulationScenarioSetArtifact,
    StrategyCandidate,
    StrategyExperiment,
    StrategyExperimentRequest,
    StrategyProfile,
    StrategyProfileCreate,
    Technician,
    TechnicianCreate,
    TechnicianUpdate,
    WorkOrder,
    WorkOrderCreate,
    WorkOrderExecutionEvent,
    WorkOrderExecutionRequest,
    WorkOrderExecutionResult,
    WorkOrderStatus,
    WorkOrderUpdate,
)
from .normalization import normalize_schedule
from .planning import plan_scoped_assignment_feasibility_payload
from .provenance import (
    DECISION_ALGORITHM_VERSION,
    build_decision_input_manifest,
    decision_build_sha,
    decision_runtime_manifest,
    release_manifest,
)
from .report import build_report
from .scheduler import (
    baseline_schedule,
    build_solver_policy_snapshot,
    optimized_schedule,
    recompute_business_result,
    replan_schedule,
    scenario_for_profile,
)
from .storage import (
    ActivePlanConflict,
    DecisionAnalysisIntegrityError,
    PublicationConflict,
    ScenarioRevisionConflict,
    Store,
)
from .verification import verify_schedule

DB_PATH = Path(os.getenv("FIELDFLOW_DB", Path(__file__).resolve().parents[1] / "fieldflow.db"))
store: Store | None = None
experiment_executor: ThreadPoolExecutor | None = None
experiment_slots: threading.BoundedSemaphore | None = None
manual_reassignment_locks: dict[str, tuple[threading.RLock, int]] = {}
manual_reassignment_locks_guard = threading.Lock()
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


def publication_conflict_detail(error: PublicationConflict) -> dict[str, object]:
    details: dict[str, object] = {str(key): value for key, value in error.details.items()}
    if "expected_active_plan_version_id" in details and "expected_active_plan_id" not in details:
        details["expected_active_plan_id"] = details.pop("expected_active_plan_version_id")
    if "current_active_plan_version_id" in details and "current_active_plan_id" not in details:
        details["current_active_plan_id"] = details.pop("current_active_plan_version_id")
    refresh_codes = {
        "ACTIVE_PLAN_CHANGED",
        "ACTIVE_PLAN_CHANGED_DURING_COMMAND",
        "ACTIVE_PLAN_VERSION_CONFLICT",
        "SCENARIO_CHANGED_DURING_COMMAND",
        "SCENARIO_REVISION_CONFLICT",
        "PLANNING_RESERVATION_SCENARIO_CONFLICT",
    }
    retryable_codes = {
        "ACTIVE_PLAN_CHANGED_DURING_COMMAND",
        "SCENARIO_CHANGED_DURING_COMMAND",
        "EXECUTION_CONTEXT_CHANGED_DURING_COMMAND",
        "IDEMPOTENT_REQUEST_IN_PROGRESS",
    }
    return {
        "code": error.code,
        "message": str(error),
        "retryable": error.code in retryable_codes,
        "refresh_required": error.code in refresh_codes,
        **details,
    }


def publication_conflict_to_http(error: PublicationConflict, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail=publication_conflict_detail(error))


def safe_filename_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:80] or "scenario"


def require_scenario(scenario_id: str) -> ScheduleScenario:
    scenario = require_store().get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, f"场景 {scenario_id} 不存在")
    return scenario


def require_plan_for_use(scenario_id: str, version_id: str, use_case: PlanUseCase) -> PlanVersion:
    try:
        return require_store().require_plan_for_use(scenario_id, version_id, use_case)
    except PublicationConflict as error:
        status_code = 404 if error.code == "PLAN_NOT_FOUND" else 409
        raise HTTPException(
            status_code,
            detail={"code": error.code, "message": str(error), **error.details},
        ) from error


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
    scenario: ScheduleScenario,
    reason: str,
    *,
    preserve_active_plan: bool = False,
    impact: FieldImpact = FieldImpact.assignment_feasibility,
    invalid_assignment_ids: list[str] | None = None,
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
    active_plan = require_store().active_plan_version(scenario.id)
    preserve_active_plan = (
        preserve_active_plan
        or active_plan is not None
        or impact
        in {
            FieldImpact.metadata_only,
            FieldImpact.commercial_only,
            FieldImpact.planning_objective,
            FieldImpact.planning_constraint,
            FieldImpact.new_demand,
            FieldImpact.capacity_added,
            FieldImpact.removed_unassigned_demand,
        }
        or any(order.status is not WorkOrderStatus.pending for order in scenario.work_orders)
    )
    try:
        require_store().save_scenario(
            scenario,
            reason,
            expected_revision=expected_revision,
            preserve_active_plan=preserve_active_plan,
            mark_plan_stale=impact is not FieldImpact.metadata_only,
            change_impact=impact,
            invalid_assignment_ids=invalid_assignment_ids,
            expected_active_plan_id=active_plan.id if active_plan else None,
            check_active_plan=True,
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_REVISION_CONFLICT",
                "message": "业务数据已被其他操作更新，请刷新后重试",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except ActivePlanConflict as error:
        raise HTTPException(
            409,
            detail={
                "code": "ACTIVE_PLAN_CHANGED",
                "message": "活动方案已变化，本次数据修改未应用，请刷新后重试",
                "expected_active_plan_id": error.expected,
                "current_active_plan_id": error.current,
            },
        ) from error
    return scenario


def normalize_order(order: WorkOrder) -> WorkOrder:
    if order.is_emergency:
        order.drop_penalty = max(order.drop_penalty, 8000)
    return order


def validate_if_match_revision(
    scenario: ScheduleScenario,
    if_match: str | None,
    *,
    required: bool = False,
) -> None:
    """Require an explicit aggregate revision for dispatcher data edits."""
    if if_match is None:
        if not required:
            return
        raise HTTPException(
            428,
            detail={
                "code": "PRECONDITION_REQUIRED",
                "message": "此写操作必须携带 If-Match 数据修订号，例如 D003",
            },
        )
    token = if_match.strip().removeprefix("W/").strip('"')
    match = re.fullmatch(r"(?:D)?(\d+)", token, flags=re.IGNORECASE)
    if not match:
        raise HTTPException(
            422,
            detail={"code": "INVALID_IF_MATCH", "message": "If-Match 应为数据修订号，例如 D003"},
        )
    expected_revision = int(match.group(1))
    if scenario.revision != expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_REVISION_CONFLICT",
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": expected_revision,
                "current_revision": scenario.revision,
            },
        )


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
        raise HTTPException(
            409,
            detail={"code": error.code, "message": str(error), **error.details},
        ) from error
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
            publication_key=publication_key,
        )
        if created:
            return
        command = require_store().get_command_record("schedule-solve", publication_key, fingerprint)
    except PublicationConflict as error:
        raise publication_conflict_to_http(error) from error
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


def artifact(
    role: Literal["baseline", "selected", "candidate"], result: ScheduleResult, strategy: str
) -> ScheduleArtifact:
    return ScheduleArtifact(id=f"ART-{uuid.uuid4().hex[:10]}", role=role, strategy=strategy, schedule=result)


def prepare_replan_run(
    scenario: ScheduleScenario,
    request: ReplanRequest,
    *,
    run_id: str | None = None,
    run_started_at: str | None = None,
    command_fingerprint: str | None = None,
) -> tuple[
    PlanningReservation,
    PlanVersion | None,
    ScheduleResult,
    list[ScheduleArtifact],
    ScheduleScenario,
    str,
    StrategyProfile,
    int,
    PlanningContext,
    ScheduleRun,
]:
    profile = profile_for_request(request.strategy, request.profile_id, request.time_limit_seconds)
    preliminary_effective = scenario_for_profile(scenario, profile)
    reservation, run = start_schedule_run(
        scenario,
        "replan",
        source_mode="ACTIVE_OR_LATEST",
        requested_time_limit_seconds=profile.time_limit_seconds,
        solver_config_hash=content_hash(preliminary_effective.solver_config),
        run_id=run_id,
        started_at=run_started_at,
        expected_active_plan_version_id=request.expected_active_plan_version_id,
        check_active_plan="expected_active_plan_version_id" in request.model_fields_set,
        command_fingerprint=command_fingerprint,
    )
    scenario = reservation.scenario_snapshot
    source = reservation.source_plan
    previous = source.selected if source else None
    internal: list[ScheduleArtifact] = []
    if not previous:
        if any(order.status == WorkOrderStatus.started for order in scenario.work_orders):
            raise HTTPException(
                409,
                detail={
                    "code": "REPLAN_SOURCE_PLAN_REQUIRED",
                    "message": "执行中的工单缺少可追溯的原方案，不能安全重排",
                },
            )
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
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    planning_time = request.planning_time
    assert planning_time is not None
    planning_context = build_planning_context(
        scenario,
        source,
        planning_time,
        reservation.execution_context,
    )
    run = require_store().bind_schedule_run_context(run, reservation, planning_context)
    return (
        reservation,
        source,
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
    action: Literal["baseline", "optimize", "replan", "activate", "restore", "reattest", "experiment"],
    *,
    source: PlanVersion | None = None,
    requested_time_limit_seconds: float = 0,
    solver_name: str = "ortools-routing",
    planning_context: PlanningContext | None = None,
    solver_config_hash: str | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
    source_mode: Literal["NONE", "ACTIVE_OR_LATEST", "EXPLICIT"] | None = None,
    expected_active_plan_version_id: str | None = None,
    check_active_plan: bool = False,
    command_fingerprint: str | None = None,
) -> tuple[PlanningReservation, ScheduleRun]:
    resolved_source_mode = source_mode or ("EXPLICIT" if source else "NONE")
    reservation, run = require_store().reserve_plan_command(
        scenario.id,
        action,
        expected_revision=scenario.revision,
        expected_active_plan_version_id=expected_active_plan_version_id,
        check_active_plan=check_active_plan,
        source_mode=resolved_source_mode,
        source_plan_version_id=source.id if source else None,
        source_use_case=(PlanUseCase.audit_view if action == "reattest" else PlanUseCase.replay),
        command_fingerprint=command_fingerprint
        or content_hash(
            {
                "scenario_id": scenario.id,
                "action": action,
                "scenario_revision": scenario.revision,
                "source_plan_version_id": source.id if source else None,
                "solver_config_hash": solver_config_hash or content_hash(scenario.solver_config),
                "requested_time_limit_seconds": requested_time_limit_seconds,
            }
        ),
        requested_time_limit_seconds=requested_time_limit_seconds,
        solver_name=solver_name,
        solver_config_hash=solver_config_hash or content_hash(scenario.solver_config),
        run_id=run_id,
        started_at=started_at,
    )
    if reservation.scenario_snapshot_hash != content_hash(scenario):
        raise PublicationConflict(
            "命令读取的业务快照已变化",
            code="SCENARIO_CHANGED_DURING_COMMAND",
            details={"reservation_id": reservation.id},
        )
    if planning_context is not None:
        run = require_store().bind_schedule_run_context(run, reservation, planning_context)
    return reservation, run


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
        execution_context_for_planning(
            execution_source_context,
            planning_time,
            scenario.solver_config.active_service_default_remaining_minutes,
        )
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


def rebind_publication_planning_context(
    context: PublicationPlanningContext,
    scenario_revision: int,
) -> PublicationPlanningContext:
    rebound = context.model_copy(deep=True)
    rebound.scenario_revision = scenario_revision
    rebound.context_fingerprint = content_hash(rebound.model_dump(exclude={"context_fingerprint"}, mode="json"))
    return rebound


def publish_selected(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    action: Literal["baseline", "optimize", "replan", "activate", "restore", "experiment_publish", "reattest"],
    *,
    artifacts: list[ScheduleArtifact],
    source: PlanVersion | None = None,
    stability_baseline: PlanVersion | None = None,
    relation: Literal[
        "new",
        "optimized_from",
        "replanned_from",
        "reactivated_from",
        "restored_from",
        "published_from_experiment",
        "reattested_from",
        "fresh_after_data_change",
    ] = "new",
    label: str | None = None,
    run: ScheduleRun,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
    replace_scenario: bool = False,
    expected_revision: int | None = None,
    planning_context: PlanningContext | None = None,
    reattestation_mode: ReattestationMode | None = None,
) -> ScheduleResult:
    source_schedule = (
        stability_baseline.selected
        if stability_baseline and result.kind == "replan"
        else source.selected
        if source and result.kind == "replan"
        else None
    )
    verification = validate_result(scenario, result, source_schedule, planning_context)
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}",
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        source_plan_version_id=source.id if source else None,
        expected_active_plan_version_id=run.expected_active_plan_version_id,
        reservation_id=run.reservation_id,
        reservation_hash=run.reservation_hash,
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
            publication_planning_context_override=(
                rebind_publication_planning_context(
                    source.publication_planning_context,
                    scenario.revision,
                )
                if source
                and result.kind == "replan"
                and action in {"activate", "restore", "reattest"}
                and source.publication_planning_context
                else None
            ),
            reattestation_mode=reattestation_mode,
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_CHANGED_DURING_COMMAND",
                "message": "求解期间业务数据已变化，结果未发布，请重新运行",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise publication_conflict_to_http(error) from error
    return plan.selected


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fieldflow", "version": __version__}


@router.get("/api/scenarios", response_model=list[ScheduleScenario])
def list_scenarios(response: Response) -> list[ScheduleScenario]:
    scenarios, warnings = require_store().list_scenarios_with_warnings()
    if warnings:
        response.headers["X-FieldFlow-Skipped-Records"] = str(len(warnings))
    return scenarios


@router.get("/api/integrity-issues", response_model=list[dict[str, str]])
def list_integrity_issues() -> list[dict[str, str]]:
    """Return the quarantine ledger without exposing the copied payloads."""
    return require_store().list_integrity_issues()


@router.get("/api/scenarios/{scenario_id}", response_model=ScheduleScenario)
def get_scenario(scenario_id: str, response: Response) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    response.headers["ETag"] = f'"D{scenario.revision}"'
    return scenario


@router.get(
    "/api/scenarios/{scenario_id}/operational-view",
    response_model=ScenarioOperationalView,
)
def get_operational_view(scenario_id: str, response: Response) -> ScenarioOperationalView:
    view = require_store().operational_view(scenario_id)
    response.headers["ETag"] = f'"D{view.scenario_revision}"'
    return view


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
            409,
            detail={
                "code": "SCENARIO_ID_CONFLICT",
                "message": "场景标识发生冲突，请重新创建",
                "current_revision": error.current,
            },
        ) from error
    return scenario


@router.post("/api/scenarios/{scenario_id}/reset", response_model=ScheduleScenario, deprecated=True)
@router.post("/api/v2/scenarios/{scenario_id}/reset", response_model=ScheduleScenario)
def reset_scenario(
    scenario_id: str,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    current = require_scenario(scenario_id)
    validate_if_match_revision(current, if_match, required=http_request.url.path.startswith("/api/v2/"))
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
        raise HTTPException(
            409,
            detail={"code": "SCENARIO_INITIAL_REVISION_MISSING", "message": "该场景没有可恢复的初始业务数据"},
        )
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
                "code": "SCENARIO_REVISION_CONFLICT",
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


@router.post("/api/scenarios/{scenario_id}/work-orders", response_model=ScheduleScenario, deprecated=True)
@router.post("/api/v2/scenarios/{scenario_id}/work-orders", response_model=ScheduleScenario)
def create_work_order(
    scenario_id: str,
    request: WorkOrderCreate,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    work_order = request.to_work_order()
    if any(item.id == work_order.id for item in scenario.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "WORK_ORDER_ALREADY_EXISTS",
                "message": f"工单 {work_order.id} 已存在",
                "resource_id": work_order.id,
            },
        )
    scenario.work_orders.append(normalize_order(work_order))
    return save_scenario_change(scenario, f"新增工单 {work_order.id}", impact=FieldImpact.new_demand)


@router.put(
    "/api/scenarios/{scenario_id}/work-orders/{work_order_id}",
    response_model=ScheduleScenario,
    deprecated=True,
)
@router.put("/api/v2/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
def update_work_order(
    scenario_id: str,
    work_order_id: str,
    request: WorkOrderUpdate,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    index = next((i for i, item in enumerate(scenario.work_orders) if item.id == work_order_id), None)
    if index is None:
        raise HTTPException(404, f"工单 {work_order_id} 不存在")
    original = scenario.work_orders[index]
    updates = request.model_dump(exclude_unset=True)
    if updates.get("note") is None and "note" in updates:
        updates["note"] = ""
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
            raise HTTPException(
                409,
                detail={
                    "code": "EXECUTION_STATUS_EVENT_REQUIRED",
                    "message": "已开始或已完成的工单只能修改备注",
                    "resource_id": work_order_id,
                },
            )
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
    changed_fields = {key for key, value in updates.items() if value != original.model_dump().get(key)}
    feasibility_fields = {
        "required_skills",
        "location",
        "service_duration",
        "window_start",
        "window_end",
        "reported_at",
        "is_emergency",
    }
    objective_fields = {"sla_deadline", "priority", "drop_penalty", "vip"}
    impact = (
        FieldImpact.assignment_feasibility
        if changed_fields & feasibility_fields
        else FieldImpact.planning_objective
        if changed_fields & objective_fields
        else FieldImpact.metadata_only
    )
    return save_scenario_change(
        scenario,
        f"更新工单 {work_order_id}",
        impact=impact,
        invalid_assignment_ids=[work_order_id] if impact is FieldImpact.assignment_feasibility else None,
    )


def execute_work_order_transition(
    scenario_id: str,
    work_order_id: str,
    action: Literal["start", "complete"],
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
                "code": "SCENARIO_REVISION_CONFLICT",
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


@router.delete(
    "/api/scenarios/{scenario_id}/work-orders/{work_order_id}",
    response_model=ScheduleScenario,
    deprecated=True,
)
@router.delete("/api/v2/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
def delete_work_order(
    scenario_id: str,
    work_order_id: str,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    order = next((item for item in scenario.work_orders if item.id == work_order_id), None)
    if not order:
        raise HTTPException(404, f"工单 {work_order_id} 不存在")
    if order.status.value in {"started", "completed"}:
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTED_WORK_ORDER_DELETE_FORBIDDEN",
                "message": "已开始或已完成的工单不能删除",
                "resource_id": work_order_id,
            },
        )
    active_plan = require_store().active_plan_version(scenario_id)
    is_unassigned_in_active_plan = bool(
        active_plan and any(item.work_order_id == work_order_id for item in active_plan.selected.unassigned)
    )
    scenario.work_orders = [item for item in scenario.work_orders if item.id != work_order_id]
    scenario.locked_assignments = [item for item in scenario.locked_assignments if item.work_order_id != work_order_id]
    return save_scenario_change(
        scenario,
        f"删除工单 {work_order_id}",
        impact=(
            FieldImpact.removed_unassigned_demand
            if is_unassigned_in_active_plan
            else FieldImpact.assignment_feasibility
        ),
        invalid_assignment_ids=[] if is_unassigned_in_active_plan else [work_order_id],
    )


@router.post("/api/scenarios/{scenario_id}/technicians", response_model=ScheduleScenario, deprecated=True)
@router.post("/api/v2/scenarios/{scenario_id}/technicians", response_model=ScheduleScenario)
def create_technician(
    scenario_id: str,
    technician: TechnicianCreate,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    if any(item.id == technician.id for item in scenario.technicians):
        raise HTTPException(
            409,
            detail={
                "code": "TECHNICIAN_ALREADY_EXISTS",
                "message": f"技师 {technician.id} 已存在",
                "resource_id": technician.id,
            },
        )
    scenario.technicians.append(Technician.model_validate(technician.model_dump(mode="python")))
    return save_scenario_change(scenario, f"新增技师 {technician.id}", impact=FieldImpact.capacity_added)


@router.put(
    "/api/scenarios/{scenario_id}/technicians/{technician_id}",
    response_model=ScheduleScenario,
    deprecated=True,
)
@router.put("/api/v2/scenarios/{scenario_id}/technicians/{technician_id}", response_model=ScheduleScenario)
def update_technician(
    scenario_id: str,
    technician_id: str,
    request: TechnicianUpdate,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    index = next((i for i, item in enumerate(scenario.technicians) if item.id == technician_id), None)
    if index is None:
        raise HTTPException(404, f"技师 {technician_id} 不存在")
    original = scenario.technicians[index]
    payload = original.model_dump()
    payload.update(request.model_dump(exclude_unset=True))
    try:
        scenario.technicians[index] = Technician.model_validate(payload)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    updates = request.model_dump(exclude_unset=True)
    changed_fields = {key for key, value in updates.items() if value != original.model_dump().get(key)}
    impact = (
        FieldImpact.assignment_feasibility
        if changed_fields & {"skills", "shift_start", "shift_end", "start_location", "overtime_limit"}
        else FieldImpact.commercial_only
        if "cost_per_minute_cents" in changed_fields
        else FieldImpact.metadata_only
    )
    active_plan = require_store().active_plan_version(scenario_id)
    invalid_assignments = (
        [item.work_order_id for item in active_plan.selected.assignments if item.technician_id == technician_id]
        if active_plan and impact is FieldImpact.assignment_feasibility
        else None
    )
    return save_scenario_change(
        scenario,
        f"更新技师 {technician_id}",
        impact=impact,
        invalid_assignment_ids=invalid_assignments,
    )


def _lock_assignment(
    scenario_id: str,
    request: LockRequest,
    *,
    expected_revision: int | None = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    active_plan = require_store().active_plan_version(scenario_id)
    if expected_revision is not None and scenario.revision != expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_REVISION_CONFLICT",
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": expected_revision,
                "current_revision": scenario.revision,
            },
        )
    order = next((o for o in scenario.work_orders if o.id == request.work_order_id), None)
    tech = next((t for t in scenario.technicians if t.id == request.technician_id), None)
    if not order:
        raise HTTPException(404, f"工单 {request.work_order_id} 不存在")
    if not tech:
        raise HTTPException(404, f"技师 {request.technician_id} 不存在")
    if not set(order.required_skills).issubset(set(tech.skills)):
        raise HTTPException(422, f"{tech.name} 不具备该工单要求的全部技能")
    if order.status is not WorkOrderStatus.pending:
        raise HTTPException(
            409,
            detail={
                "code": "EXECUTED_WORK_ORDER_LOCK_FORBIDDEN",
                "message": "已开始或已完成工单不能更改锁定关系",
                "resource_id": order.id,
            },
        )
    active_assignment = next(
        (
            item
            for item in (active_plan.selected.assignments if active_plan else [])
            if item.work_order_id == request.work_order_id
        ),
        None,
    )
    route_compatible = (
        not request.locked or active_assignment is None or active_assignment.technician_id == request.technician_id
    )
    scenario.locked_assignments = [
        item for item in scenario.locked_assignments if item.work_order_id != request.work_order_id
    ]
    if request.locked:
        scenario.locked_assignments.append(
            LockedAssignment(work_order_id=request.work_order_id, technician_id=request.technician_id)
        )
    return save_scenario_change(
        scenario,
        ("锁定" if request.locked else "解除锁定") + f"工单 {request.work_order_id}",
        preserve_active_plan=True,
        impact=(FieldImpact.planning_constraint if route_compatible else FieldImpact.assignment_feasibility),
        invalid_assignment_ids=[] if route_compatible else [request.work_order_id],
    )


@router.post("/api/scenarios/{scenario_id}/lock", response_model=ScheduleScenario, deprecated=True)
@router.post("/api/v2/scenarios/{scenario_id}/lock", response_model=ScheduleScenario)
def lock_assignment(
    scenario_id: str,
    request: LockRequest,
    http_request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    validate_if_match_revision(scenario, if_match, required=http_request.url.path.startswith("/api/v2/"))
    return _lock_assignment(scenario_id, request, expected_revision=scenario.revision)


def replay_manual_reassignment_terminal(
    scenario_id: str,
    command: dict,
) -> ManualReassignmentResult:
    """Rebuild a terminal saga response from verified resources, never its cached response body."""
    current = require_scenario(scenario_id)
    active = require_store().active_plan_version(scenario_id)
    if command["status"] == "COMPLETED":
        publication_key = command.get("publication_key")
        published = require_store().plan_for_publication_key(publication_key) if publication_key else None
        if not published and command.get("resource_type") == "plan_version" and command.get("resource_id"):
            published = require_store().get_plan_version(scenario_id, command["resource_id"])
        if not published:
            raise HTTPException(
                409,
                detail={
                    "code": "MANUAL_REASSIGNMENT_RESOURCE_MISSING",
                    "message": "人工改派记录对应的方案不存在，不能安全重放结果",
                },
            )
        verified = require_plan_for_use(scenario_id, published.id, PlanUseCase.replay)
        return ManualReassignmentResult(
            lock_persisted=any(
                item.work_order_id == command.get("payload", {}).get("work_order_id")
                and item.technician_id == command.get("payload", {}).get("target_technician_id")
                for item in current.locked_assignments
            ),
            replan_status="COMPLETED",
            active_plan_preserved=active is not None,
            scenario=current,
            schedule=verified.selected,
        )
    return ManualReassignmentResult(
        lock_persisted=any(
            item.work_order_id == command.get("payload", {}).get("work_order_id")
            and item.technician_id == command.get("payload", {}).get("target_technician_id")
            for item in current.locked_assignments
        ),
        replan_status="FAILED",
        active_plan_preserved=active is not None,
        scenario=current,
        error={
            "code": "REPLAN_FAILED_AFTER_LOCK",
            "message": "人工锁定已保存，但局部重排运行失败",
        },
    )


def _manual_reassignment(scenario_id: str, request: ManualReassignmentRequest) -> ManualReassignmentResult:
    """Recoverable saga: persist the lock once, then idempotently publish replan."""
    scenario = require_scenario(scenario_id)
    namespace = f"{scenario_id}:manual-reassignment"
    publication_key = f"{scenario_id}:replan:{request.idempotency_key}"
    fingerprint = content_hash({"scenario_id": scenario_id, "request": request})
    try:
        existing = require_store().get_command_record(namespace, request.idempotency_key, fingerprint)
    except PublicationConflict as error:
        raise publication_conflict_to_http(error) from error
    if existing and existing["status"] in {"COMPLETED", "FAILED_AFTER_LOCK"}:
        return replay_manual_reassignment_terminal(scenario_id, existing)
    if existing and existing["status"] in {"FAILED", "FAILED_CONTEXT_CHANGED"}:
        current = require_scenario(scenario_id)
        code = (
            "MANUAL_REASSIGNMENT_CONTEXT_CHANGED"
            if existing["status"] == "FAILED_CONTEXT_CHANGED"
            else "MANUAL_REASSIGNMENT_FAILED"
        )
        stored_detail = existing.get("payload", {}).get("detail")
        if isinstance(stored_detail, dict) and stored_detail.get("code") == code:
            raise HTTPException(409, detail=stored_detail)
        raise HTTPException(
            409,
            detail={
                "code": code,
                "message": (
                    "锁定提交后的业务数据又发生变化，不能静默恢复原重排"
                    if existing["status"] == "FAILED_CONTEXT_CHANGED"
                    else "人工改派未提交，请刷新当前数据后重试"
                ),
                "current_revision": current.revision,
            },
        )
    if not existing:
        if scenario.revision != request.expected_revision:
            raise HTTPException(
                409,
                detail={
                    "code": "SCENARIO_REVISION_CONFLICT",
                    "message": "业务数据已变化，请刷新后重试",
                    "expected_revision": request.expected_revision,
                    "current_revision": scenario.revision,
                },
            )
        try:
            created = require_store().begin_command_record(
                namespace,
                request.idempotency_key,
                fingerprint,
                status="RESERVED",
                resource_type="work_order",
                resource_id=request.work_order_id,
                publication_key=publication_key,
                payload={
                    "phase": "RESERVED",
                    "base_revision": request.expected_revision,
                    "work_order_id": request.work_order_id,
                    "target_technician_id": request.technician_id,
                    "planning_time": request.planning_time,
                },
            )
            if not created:
                raise HTTPException(
                    409,
                    detail={
                        "code": "IDEMPOTENT_REQUEST_IN_PROGRESS",
                        "message": "相同人工改派请求正在处理，请稍后重试",
                    },
                )
        except PublicationConflict as error:
            raise publication_conflict_to_http(error) from error
        existing = require_store().get_command_record(namespace, request.idempotency_key, fingerprint)
        assert existing

    payload = dict(existing["payload"])
    published = require_store().plan_for_publication_key(publication_key)
    if published:
        current = require_scenario(scenario_id)
        result = ManualReassignmentResult(
            lock_persisted=True,
            replan_status="COMPLETED",
            active_plan_preserved=True,
            scenario=current,
            schedule=published.selected,
        )
        require_store().update_command_record(
            namespace,
            request.idempotency_key,
            fingerprint,
            status="COMPLETED",
            resource_type="plan_version",
            resource_id=published.id,
            publication_key=publication_key,
            payload={
                **payload,
                "phase": "COMPLETED",
                "plan_version_id": published.id,
            },
        )
        return result

    if existing["status"] == "RESERVED":
        scenario = require_scenario(scenario_id)
        exact_lock = next(
            (
                item
                for item in scenario.locked_assignments
                if item.work_order_id == request.work_order_id and item.technician_id == request.technician_id
            ),
            None,
        )
        if exact_lock and scenario.revision in {request.expected_revision, request.expected_revision + 1}:
            locked_scenario = scenario
        else:
            try:
                locked_scenario = _lock_assignment(
                    scenario_id,
                    LockRequest(
                        work_order_id=request.work_order_id,
                        technician_id=request.technician_id,
                        locked=True,
                    ),
                    expected_revision=request.expected_revision,
                )
            except HTTPException as error:
                detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
                require_store().update_command_record(
                    namespace,
                    request.idempotency_key,
                    fingerprint,
                    status="FAILED",
                    resource_type="work_order",
                    resource_id=request.work_order_id,
                    publication_key=publication_key,
                    payload={**payload, "phase": "FAILED", "http_status": error.status_code, "detail": detail},
                )
                raise
        payload.update(
            {
                "phase": "LOCK_COMMITTED",
                "lock_revision": locked_scenario.revision,
                "run_id": f"RUN-MR-{content_hash({'namespace': namespace, 'key': request.idempotency_key, 'fingerprint': fingerprint})[:12]}",
                "run_started_at": _now(),
            }
        )
        require_store().update_command_record(
            namespace,
            request.idempotency_key,
            fingerprint,
            status="LOCK_COMMITTED",
            resource_type="work_order",
            resource_id=request.work_order_id,
            publication_key=publication_key,
            payload=payload,
        )
    else:
        lock_revision = int(payload.get("lock_revision", -1))
        current = require_scenario(scenario_id)
        exact_lock = any(
            item.work_order_id == request.work_order_id and item.technician_id == request.technician_id
            for item in current.locked_assignments
        )
        if not exact_lock or current.revision != lock_revision:
            detail = {
                "code": "MANUAL_REASSIGNMENT_CONTEXT_CHANGED",
                "message": "锁定提交后的业务数据又发生变化，不能静默恢复原重排",
                "lock_revision": lock_revision,
                "current_revision": current.revision,
            }
            require_store().update_command_record(
                namespace,
                request.idempotency_key,
                fingerprint,
                status="FAILED_CONTEXT_CHANGED",
                resource_type="work_order",
                resource_id=request.work_order_id,
                publication_key=publication_key,
                payload={
                    **payload,
                    "phase": "FAILED_CONTEXT_CHANGED",
                    "http_status": 409,
                    "detail": detail,
                    "failed_at": _now(),
                },
            )
            raise HTTPException(
                409,
                detail=detail,
            )
        if not payload.get("run_id") or not payload.get("run_started_at"):
            payload.update(
                {
                    "phase": "LOCK_COMMITTED",
                    "run_id": f"RUN-MR-{content_hash({'namespace': namespace, 'key': request.idempotency_key, 'fingerprint': fingerprint})[:12]}",
                    "run_started_at": _now(),
                }
            )
            require_store().update_command_record(
                namespace,
                request.idempotency_key,
                fingerprint,
                status="LOCK_COMMITTED",
                resource_type="work_order",
                resource_id=request.work_order_id,
                publication_key=publication_key,
                payload=payload,
            )

    run_id = str(payload["run_id"])
    run_started_at = str(payload["run_started_at"])

    def record_replan_created(run: ScheduleRun) -> None:
        if run.id != run_id:
            raise PublicationConflict("人工改派恢复了错误的求解记录")
        payload.update({"phase": "REPLAN_CREATED", "run_id": run.id})
        require_store().update_command_record(
            namespace,
            request.idempotency_key,
            fingerprint,
            status="REPLAN_CREATED",
            resource_type="schedule_run",
            resource_id=run.id,
            publication_key=publication_key,
            payload=payload,
        )

    try:
        replan_payload: dict[str, object] = {
            "planning_time": request.planning_time,
            "strategy": "stable",
            "idempotency_key": request.idempotency_key,
        }
        if "expected_active_plan_version_id" in request.model_fields_set:
            replan_payload["expected_active_plan_version_id"] = request.expected_active_plan_version_id
        schedule = _run_replan(
            scenario_id,
            ReplanRequest.model_validate(replan_payload),
            run_identity=(run_id, run_started_at),
            on_run_created=record_replan_created,
        )
        current = require_scenario(scenario_id)
        published_plan = require_store().active_plan_version(scenario_id)
        if not published_plan or published_plan.selected.id != schedule.id:
            raise PublicationConflict("局部重排返回结果未绑定活动方案")
        result = ManualReassignmentResult(
            lock_persisted=True,
            replan_status="COMPLETED",
            active_plan_preserved=True,
            scenario=current,
            schedule=schedule,
        )
        payload.update({"phase": "PLAN_PUBLISHED", "plan_version_id": published_plan.id})
        require_store().update_command_record(
            namespace,
            request.idempotency_key,
            fingerprint,
            status="PLAN_PUBLISHED",
            resource_type="plan_version",
            resource_id=published_plan.id,
            publication_key=publication_key,
            payload=payload,
        )
    except HTTPException as error:
        if isinstance(error.detail, dict) and error.detail.get("code") == "IDEMPOTENT_REQUEST_IN_PROGRESS":
            raise
        current = require_scenario(scenario_id)
        result = ManualReassignmentResult(
            lock_persisted=True,
            replan_status="FAILED",
            active_plan_preserved=require_store().active_plan_version(scenario_id) is not None,
            scenario=current,
            error={
                "code": "REPLAN_FAILED_AFTER_LOCK",
                "message": "人工锁定已保存，但局部重排运行失败",
            },
        )
    except Exception:
        current = require_scenario(scenario_id)
        result = ManualReassignmentResult(
            lock_persisted=True,
            replan_status="FAILED",
            active_plan_preserved=require_store().active_plan_version(scenario_id) is not None,
            scenario=current,
            error={
                "code": "REPLAN_FAILED_AFTER_LOCK",
                "message": "人工锁定已保存，但局部重排运行失败",
            },
        )
    result_plan = require_store().plan_for_publication_key(publication_key) if result.schedule else None
    require_store().update_command_record(
        namespace,
        request.idempotency_key,
        fingerprint,
        status="COMPLETED" if result.schedule else "FAILED_AFTER_LOCK",
        resource_type="plan_version" if result.schedule else "work_order",
        resource_id=(result_plan.id if result_plan else request.work_order_id),
        publication_key=publication_key,
        payload={
            **payload,
            "phase": "COMPLETED" if result.schedule else "FAILED_AFTER_LOCK",
            "plan_version_id": result_plan.id if result_plan else None,
        },
    )
    return result


@router.post(
    "/api/scenarios/{scenario_id}/manual-reassignment",
    response_model=ManualReassignmentResult,
)
def manual_reassignment(scenario_id: str, request: ManualReassignmentRequest) -> ManualReassignmentResult:
    """Serialize one local idempotency key while the recoverable saga advances."""
    lock_key = f"{scenario_id}:{request.idempotency_key}"
    with manual_reassignment_locks_guard:
        command_lock, users = manual_reassignment_locks.get(lock_key, (threading.RLock(), 0))
        manual_reassignment_locks[lock_key] = (command_lock, users + 1)
    try:
        with command_lock:
            return _manual_reassignment(scenario_id, request)
    finally:
        with manual_reassignment_locks_guard:
            current_lock, current_users = manual_reassignment_locks.get(lock_key, (command_lock, 1))
            if current_lock is command_lock and current_users <= 1:
                manual_reassignment_locks.pop(lock_key, None)
            elif current_lock is command_lock:
                manual_reassignment_locks[lock_key] = (command_lock, current_users - 1)


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
        raise HTTPException(
            409,
            detail={
                "code": "BASELINE_EXECUTION_CONTEXT_CONFLICT",
                "message": "场景已有执行记录，请使用局部重排延续当前执行位置和容量",
            },
        )
    publication_key, fingerprint, existing = publication_retry("baseline", scenario, idempotency_key, {})
    if existing:
        return existing.selected
    reserve_solve_command(publication_key, fingerprint)
    run: ScheduleRun | None = None
    try:
        _reservation, run = start_schedule_run(
            scenario,
            "baseline",
            solver_name="fieldflow-greedy",
            command_fingerprint=fingerprint,
        )
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
        raise HTTPException(
            409,
            detail={
                "code": "OPTIMIZE_EXECUTION_CONTEXT_CONFLICT",
                "message": "场景已有执行记录，请使用局部重排延续当前执行位置和容量",
            },
        )
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
    run: ScheduleRun | None = None
    try:
        reservation, run = start_schedule_run(
            scenario,
            "optimize",
            source_mode="ACTIVE_OR_LATEST",
            requested_time_limit_seconds=profile.time_limit_seconds,
            solver_config_hash=content_hash(effective.solver_config),
            expected_active_plan_version_id=request.expected_active_plan_version_id,
            check_active_plan="expected_active_plan_version_id" in request.model_fields_set,
            command_fingerprint=fingerprint,
        )
        scenario = reservation.scenario_snapshot
        source = reservation.source_plan
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


def _run_replan(
    scenario_id: str,
    request: ReplanRequest,
    *,
    run_identity: tuple[str, str] | None = None,
    on_run_created: Callable[[ScheduleRun], None] | None = None,
) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    incoming = request.emergency_order.to_work_order() if request.emergency_order else None
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
            raise publication_conflict_to_http(error) from error
        if existing_publication:
            return existing_publication.selected
    if incoming and idempotency_key and request_fingerprint:
        try:
            existing_command = require_store().get_command_record(
                command_namespace, idempotency_key, request_fingerprint
            )
        except PublicationConflict as error:
            raise publication_conflict_to_http(error) from error
        if existing_command and existing_command["status"] == "COMPLETED":
            plan = require_plan_for_use(
                scenario_id,
                existing_command["resource_id"] or "",
                PlanUseCase.replay,
            )
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
                publication_key=solve_publication_key,
            )
            if not command_created:
                command = require_store().get_command_record(command_namespace, idempotency_key, request_fingerprint)
                if command and command["status"] == "COMPLETED":
                    plan = require_plan_for_use(
                        scenario_id,
                        command["resource_id"] or "",
                        PlanUseCase.replay,
                    )
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
            raise publication_conflict_to_http(error) from error
        except ValueError as error:
            raise HTTPException(422, detail={"message": "突发工单数据不完整", "error": str(error)}) from error
    elif solve_publication_key and request_fingerprint:
        reserve_solve_command(solve_publication_key, request_fingerprint)
    try:
        (
            reservation,
            source,
            previous,
            internal,
            effective,
            strategy_key,
            profile,
            planning_time,
            planning_context,
            run,
        ) = prepare_replan_run(
            scenario,
            request,
            run_id=run_identity[0] if run_identity else None,
            run_started_at=run_identity[1] if run_identity else None,
            command_fingerprint=request_fingerprint,
        )
        scenario = reservation.scenario_snapshot
        if on_run_created:
            on_run_created(run)
    except HTTPException as error:
        if incoming and idempotency_key and request_fingerprint:
            detail: dict[str, object] = (
                {str(key): value for key, value in error.detail.items()}
                if isinstance(error.detail, dict)
                else {"message": str(error.detail)}
            )
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
            detail: dict[str, object] = (
                {str(key): value for key, value in error.detail.items()}
                if isinstance(error.detail, dict)
                else {"message": str(error.detail)}
            )
            detail["message"] = "突发工单已保存，但局部重排没有生成可发布方案；最后发布方案仍保留"
            detail["emergency_work_order_persisted"] = True
            detail["scenario_revision"] = scenario.revision
            detail["coverage_status"] = "PARTIAL_NEW_DEMAND"
            candidate_resource_id = detail.get("candidate_id")
            require_store().update_command_record(
                command_namespace,
                idempotency_key,
                request_fingerprint,
                status="FAILED",
                resource_type="schedule_candidate",
                resource_id=candidate_resource_id if isinstance(candidate_resource_id, str) else None,
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


@router.post("/api/scenarios/{scenario_id}/replan", response_model=ScheduleResult)
def run_replan(scenario_id: str, request: ReplanRequest) -> ScheduleResult:
    return _run_replan(scenario_id, request)


@router.get("/api/scenarios/{scenario_id}/plan-versions", response_model=list[PlanVersion])
def list_plan_versions(scenario_id: str, response: Response) -> list[PlanVersion]:
    require_scenario(scenario_id)
    plans, warnings = require_store().list_plan_versions_with_warnings(scenario_id)
    if warnings:
        response.headers["X-FieldFlow-Skipped-Records"] = str(len(warnings))
    return plans


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


def resolve_decision_analysis_context(
    scenario_id: str,
    plan: PlanVersion,
    requested_scope: DecisionAnalysisScope | None,
) -> DecisionAnalysisContext:
    require_scenario(scenario_id)
    publication_context = plan.publication_planning_context
    expected_scope = (
        DecisionAnalysisScope.publication_remaining_plan
        if plan.selected.kind == "replan" and publication_context is not None
        else DecisionAnalysisScope.frozen_full_plan
    )
    if requested_scope is None or requested_scope is DecisionAnalysisScope.ex_ante_frozen_plan:
        scope = expected_scope
    else:
        scope = requested_scope
    if scope is not expected_scope:
        raise HTTPException(
            422,
            detail={
                "code": "ANALYSIS_SCOPE_MISMATCH",
                "message": "请求范围与方案类型不一致；普通方案使用完整冻结范围，重排方案使用发布时剩余范围",
                "requested_scope": scope.value,
                "supported_scopes": [expected_scope.value],
            },
        )
    active_booking_ids = (
        sorted(
            {
                str(item.booking_id or item.source_assignment_hash)
                for item in publication_context.frozen_booking_identities
                if item.booking_id or item.source_assignment_hash
            }
        )
        if publication_context
        else []
    )
    return DecisionAnalysisContext(
        analysis_scope=scope,
        current_execution_watermark=(publication_context.execution_event_sequence if publication_context else 0),
        analysis_as_of_time=(publication_context.planning_time if publication_context else None),
        execution_context_hash=(publication_context.context_fingerprint if publication_context else None),
        actual_execution_included=False,
        active_booking_ids=active_booking_ids,
    )


def raise_decision_analysis_http(error: DecisionAnalysisError) -> NoReturn:
    status_code = 422 if error.code.endswith("NOT_SUPPORTED") else 409
    raise HTTPException(
        status_code,
        detail={"code": error.code, "message": error.message, **error.details},
    ) from error


def require_frozen_plan_integrity(plan: PlanVersion) -> None:
    try:
        validate_frozen_plan_integrity(plan, require_store().travel_provider)
    except DecisionAnalysisError as error:
        raise_decision_analysis_http(error)


@router.get(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/cost-analysis",
    response_model=CostAnalysis,
    deprecated=True,
)
def get_cost_analysis(
    scenario_id: str,
    version_id: str,
    response: Response,
    analysis_scope: Annotated[DecisionAnalysisScope | None, Query()] = None,
) -> CostAnalysis:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    run_request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(
        {"analysis_type": "COST", "analysis_scope": analysis_scope, "request": {}}
    )
    run = execute_decision_analysis_run(scenario_id, plan, run_request, response)
    response.status_code = status.HTTP_200_OK
    if run.status != "COMPLETED" or not isinstance(run.result, CostAnalysis):
        raise HTTPException(409, detail=run.error or {"code": "ANALYSIS_FAILED", "message": "成本分析失败"})
    return run.result


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/capacity-analysis",
    response_model=CapacityAnalysis,
    deprecated=True,
)
def post_capacity_analysis(
    scenario_id: str,
    version_id: str,
    request: CapacityAnalysisRequest,
    response: Response,
) -> CapacityAnalysis:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    payload = request.model_dump(mode="json", exclude={"analysis_scope"})
    run_request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(
        {"analysis_type": "CAPACITY", "analysis_scope": request.analysis_scope, "request": payload}
    )
    run = execute_decision_analysis_run(scenario_id, plan, run_request, response)
    response.status_code = status.HTTP_200_OK
    if run.status != "COMPLETED" or not isinstance(run.result, CapacityAnalysis):
        raise HTTPException(409, detail=run.error or {"code": "ANALYSIS_FAILED", "message": "容量分析失败"})
    return run.result


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/risk-simulation",
    response_model=RiskSimulationResult,
    deprecated=True,
)
def post_risk_simulation(
    scenario_id: str,
    version_id: str,
    request: RiskSimulationRequest,
    response: Response,
) -> RiskSimulationResult:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    payload = request.model_dump(mode="json", exclude={"analysis_scope"})
    run_request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(
        {"analysis_type": "RISK", "analysis_scope": request.analysis_scope, "request": payload}
    )
    run = execute_decision_analysis_run(scenario_id, plan, run_request, response)
    response.status_code = status.HTTP_200_OK
    if run.status != "COMPLETED" or not isinstance(run.result, RiskSimulationResult):
        raise HTTPException(409, detail=run.error or {"code": "ANALYSIS_FAILED", "message": "风险分析失败"})
    return run.result


@router.post(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/analysis-runs",
    response_model=DecisionAnalysisRun,
    status_code=201,
    responses={
        200: {"description": "已完成分析的幂等重放"},
        202: {"description": "相同输入的分析仍在运行"},
        409: {"description": "失败或中断记录需要显式重试，或冻结输入冲突"},
    },
)
def create_decision_analysis_run(
    scenario_id: str,
    version_id: str,
    request: DecisionAnalysisRunRequest,
    response: Response,
) -> DecisionAnalysisRun:
    require_scenario(scenario_id)
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return execute_decision_analysis_run(scenario_id, plan, request, response)


def execute_decision_analysis_run(
    scenario_id: str,
    plan: PlanVersion,
    request: DecisionAnalysisRunRequest,
    response: Response,
    *,
    retry_of: DecisionAnalysisRun | None = None,
    context_override: DecisionAnalysisContext | None = None,
    force_new: bool = False,
    supersedes_analysis_id: str | None = None,
    on_reserved: Callable[[DecisionAnalysisRun], None] | None = None,
) -> DecisionAnalysisRun:
    plan = require_plan_for_use(scenario_id, plan.id, PlanUseCase.analyze)
    context = context_override or resolve_decision_analysis_context(scenario_id, plan, request.analysis_scope)
    provider = require_store().travel_provider
    if request.analysis_type == "COST":
        cost_parameters = request.request
        policy_version = cost_parameters.cost_policy.policy_version
        policy_snapshot = {
            "cost_policy": cost_parameters.cost_policy.model_dump(mode="json"),
            "analysis_horizon": cost_parameters.analysis_horizon.model_dump(mode="json"),
        }
    elif request.analysis_type == "CAPACITY":
        capacity_parameters = request.request
        capacity_request = CapacityAnalysisRequest.model_validate(
            {**capacity_parameters.model_dump(mode="json"), "analysis_scope": context.analysis_scope}
        )
        policy_version = capacity_request.capacity_policy.policy_version
        policy_snapshot = capacity_request.model_dump(mode="json")
    else:
        risk_parameters = request.request
        risk_request = RiskSimulationRequest.model_validate(
            {**risk_parameters.model_dump(mode="json"), "analysis_scope": context.analysis_scope}
        )
        policy_version = "FIELD_SERVICE_SIMULATION_V6"
        policy_snapshot = risk_request.model_dump(mode="json")
    request_snapshot = request.model_dump(mode="json")
    runtime_manifest = decision_runtime_manifest()
    input_manifest = build_decision_input_manifest(
        analysis_type=request.analysis_type,
        request_snapshot=request_snapshot,
        policy_snapshot=policy_snapshot,
        analysis_context=context,
        plan_manifest_hash=plan.publication_manifest_hash,
        runtime_manifest=runtime_manifest,
        scenario_snapshot_hash=plan.scenario_snapshot_hash,
        schedule_hash=schedule_signature(plan.selected),
        travel_model_fingerprint=provider.fingerprint,
    )
    input_hash = input_manifest.semantic_input_hash
    reserved, created = require_store().reserve_decision_analysis_run(
        DecisionAnalysisRun(
            id="pending",
            scenario_id=scenario_id,
            number=0,
            plan_version_id=plan.id,
            plan_number=plan.number,
            analysis_type=request.analysis_type,
            analysis_scope=context.analysis_scope,
            scenario_snapshot_hash=plan.scenario_snapshot_hash,
            schedule_hash=schedule_signature(plan.selected),
            current_execution_watermark=context.current_execution_watermark,
            analysis_as_of_time=context.analysis_as_of_time,
            execution_context_hash=context.execution_context_hash,
            actual_execution_included=context.actual_execution_included,
            active_booking_ids=context.active_booking_ids,
            travel_model_fingerprint=provider.fingerprint,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            code_version=__version__,
            algorithm_version=DECISION_ALGORITHM_VERSION,
            build_sha=decision_build_sha(),
            runtime_manifest=runtime_manifest,
            release_manifest=release_manifest(),
            input_hash=input_hash,
            input_manifest=input_manifest,
            request_snapshot=request_snapshot,
            logical_analysis_id=retry_of.logical_analysis_id if retry_of else "",
            retry_of_analysis_id=retry_of.id if retry_of else None,
            attempt_number=(retry_of.attempt_number + 1) if retry_of else 1,
            supersedes_analysis_id=supersedes_analysis_id,
            status="RUNNING",
            created_at=_now(),
        ),
        force_new=retry_of is not None or force_new,
    )
    if created:
        response.status_code = status.HTTP_201_CREATED
        if on_reserved:
            on_reserved(reserved)
    elif reserved.integrity_status is AnalysisIntegrityStatus.failed:
        raise HTTPException(
            409,
            detail={
                "code": "ANALYSIS_INTEGRITY_FAILED",
                "message": "已有经营分析的证明校验失败，不能重放或用于业务计算",
                "analysis_id": reserved.id,
            },
        )
    elif reserved.status == "RUNNING":
        response.status_code = status.HTTP_202_ACCEPTED
    elif reserved.status in {"FAILED", "INTERRUPTED"}:
        raise HTTPException(
            409,
            {
                "code": "ANALYSIS_EXPLICIT_RETRY_REQUIRED",
                "message": "相同输入已有失败或中断记录；请显式重试并保留原 A 记录",
                "analysis_id": reserved.id,
                "analysis_number": reserved.number,
                "analysis_status": reserved.status,
                "retry_endpoint": f"/api/scenarios/{scenario_id}/analysis-runs/{reserved.id}/retry",
            },
        )
    else:
        response.status_code = status.HTTP_200_OK
    if not created or reserved.status != "RUNNING":
        return reserved
    artifacts: list[DecisionAnalysisArtifact] = []
    try:
        if request.analysis_type == "COST":
            result = cost_analysis(
                plan,
                cost_parameters.cost_policy,
                provider,
                context=context,
                horizon=cost_parameters.analysis_horizon,
                expected_input_hash=input_hash,
            )
            result_hash = result.analysis_input_hash
        elif request.analysis_type == "CAPACITY":
            result = capacity_analysis(
                plan,
                capacity_request,
                provider,
                context=context,
                expected_input_hash=input_hash,
            )
            result_hash = result.analysis_input_hash
            for option in result.options:
                if option.diagnostic_schedule is None or option.verification_report is None:
                    continue
                artifact = CapacityCounterfactualArtifact(
                    id=f"DAA-{reserved.id}-{option.option_id}",
                    scenario_id=scenario_id,
                    analysis_run_id=reserved.id,
                    option_id=option.option_id,
                    decision_status=option.decision_status,
                    formal_result_available=(
                        option.decision_status
                        in {CapacityDecisionStatus.internal_verified, CapacityDecisionStatus.external_confirmed}
                        and option.verification_report.valid
                    ),
                    schedule=option.diagnostic_schedule,
                    verification_report=option.verification_report,
                    structural_verification=option.verification_report,
                    commercial_verification_status=(
                        "UNVERIFIED"
                        if option.decision_status is CapacityDecisionStatus.external_conditional
                        else "VERIFIED"
                        if option.decision_status is CapacityDecisionStatus.external_confirmed
                        else "NOT_APPLICABLE"
                    ),
                    conditional_assumptions=(
                        [option.assumption, "外部供应商容量、服务时刻和 SLA 承诺尚未验证"]
                        if option.decision_status is CapacityDecisionStatus.external_conditional
                        else []
                    ),
                    conditional_upper_bound_kpis=option.conditional_upper_bound_kpis,
                    route_diff=option.route_diff,
                    changed_inputs=option.changed_inputs,
                    external_assignments=option.external_assignments,
                    work_order_dispositions=option.work_order_dispositions,
                    counterfactual_kpis=option.counterfactual_kpis,
                    created_at=_now(),
                )
                artifact.artifact_hash = content_hash(
                    artifact.model_dump(
                        exclude={
                            "artifact_hash",
                            "integrity_status",
                            "self_integrity",
                            "parent_analysis_integrity",
                            "effective_integrity",
                            "attestation_requirement",
                        },
                        mode="json",
                    )
                )
                artifact.integrity_status = AnalysisIntegrityStatus.verified
                artifacts.append(artifact)
                option.artifact_id = artifact.id
                option.artifact_hash = artifact.artifact_hash
                option.diagnostic_schedule = None
                option.verification_report = None
                option.route_diff = []
                option.external_assignments = []
                option.work_order_dispositions = []
                option.counterfactual_kpis = None
        else:
            result = simulate_plan_risk(
                plan,
                risk_request,
                provider,
                context=context,
                expected_input_hash=input_hash,
            )
            result_hash = result.simulation_input_hash
            if plan.scenario_snapshot is None:
                raise DecisionAnalysisError("PLAN_SNAPSHOT_MISSING", "方案缺少业务快照")
            scenario_set_manifest = build_simulation_scenario_set(
                plan.scenario_snapshot,
                risk_request,
                result.seed,
                context.analysis_as_of_time or 0,
                active_work_order_ids=result.emergency_location_work_order_ids,
            )
            if content_hash(scenario_set_manifest) != result.simulation_scenario_set_hash:
                raise DecisionAnalysisError(
                    "SIMULATION_SCENARIO_SET_HASH_MISMATCH",
                    "风险结果与持久化共同随机场景集不一致",
                )
            event_payload = scenario_set_manifest["emergency_events"]
            emergency_events = (
                [SimulationEmergencyEvent.model_validate(item) for item in event_payload]
                if isinstance(event_payload, list)
                else []
            )
            scenario_set_artifact = SimulationScenarioSetArtifact(
                id=f"DAA-{reserved.id}-scenario-set",
                scenario_id=scenario_id,
                analysis_run_id=reserved.id,
                emergency_dispatch_policy=risk_request.emergency_dispatch_policy,
                emergency_responder_selection_policy=risk_request.emergency_responder_selection_policy,
                emergency_location_policy=risk_request.emergency_location_policy,
                emergency_location_work_order_ids=result.emergency_location_work_order_ids,
                scenario_snapshot_hash=plan.scenario_snapshot_hash,
                seed=result.seed,
                trials=result.trials,
                technician_ids=sorted(item.id for item in plan.scenario_snapshot.technicians),
                work_order_ids=sorted(item.id for item in plan.scenario_snapshot.work_orders),
                exogenous_parameters={
                    "travel_delay_max_percent": risk_request.travel_delay_max_percent,
                    "service_duration_jitter_percent": risk_request.service_duration_jitter_percent,
                    "technician_absence_basis_points": risk_request.technician_absence_basis_points,
                    "emergency_order_basis_points": risk_request.emergency_order_basis_points,
                    "customer_no_show_basis_points": risk_request.customer_no_show_basis_points,
                    "analysis_as_of_time": context.analysis_as_of_time or 0,
                },
                emergency_events=emergency_events,
                scenario_set_hash=result.simulation_scenario_set_hash,
                created_at=_now(),
            )
            scenario_set_artifact.artifact_hash = content_hash(
                scenario_set_artifact.model_dump(
                    exclude={
                        "artifact_hash",
                        "integrity_status",
                        "self_integrity",
                        "parent_analysis_integrity",
                        "effective_integrity",
                        "attestation_requirement",
                    },
                    mode="json",
                )
            )
            scenario_set_artifact.integrity_status = AnalysisIntegrityStatus.verified
            artifacts.append(scenario_set_artifact)
            result.scenario_set_artifact_id = scenario_set_artifact.id
            trial_outcome_artifact = RiskTrialOutcomeArtifact(
                id=f"DAA-{reserved.id}-trial-outcomes",
                scenario_id=scenario_id,
                analysis_run_id=reserved.id,
                scenario_set_hash=result.simulation_scenario_set_hash,
                detail_policy=risk_request.artifact_detail_policy,
                metrics=result.trial_metrics,
                created_at=_now(),
            )
            trial_outcome_artifact.artifact_hash = content_hash(
                trial_outcome_artifact.model_dump(
                    exclude={
                        "artifact_hash",
                        "integrity_status",
                        "self_integrity",
                        "parent_analysis_integrity",
                        "effective_integrity",
                        "attestation_requirement",
                    },
                    mode="json",
                )
            )
            trial_outcome_artifact.integrity_status = AnalysisIntegrityStatus.verified
            artifacts.append(trial_outcome_artifact)
            result.trial_outcome_artifact_id = trial_outcome_artifact.id
        if result_hash != input_hash:
            raise DecisionAnalysisError(
                "ANALYSIS_INPUT_HASH_MISMATCH",
                "经营分析的预留输入与计算输入不一致",
            )
    except DecisionAnalysisError as error:
        failed = reserved.model_copy(deep=True)
        failed.status = "FAILED"
        failed.error = {"code": error.code, "message": error.message, **error.details}
        failed.finished_at = _now()
        return require_store().finish_decision_analysis_run(failed)
    except Exception as error:
        failed = reserved.model_copy(deep=True)
        failed.status = "FAILED"
        failed.error = {
            "code": "ANALYSIS_FAILED",
            "message": "经营分析计算失败",
            "error_type": type(error).__name__,
        }
        failed.finished_at = _now()
        return require_store().finish_decision_analysis_run(failed)
    completed = reserved.model_copy(deep=True)
    completed.status = "COMPLETED"
    completed.result = result
    completed.finished_at = _now()
    return require_store().finish_decision_analysis_run(completed, artifacts=artifacts)


@router.post(
    "/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}/retry",
    response_model=DecisionAnalysisRun,
    status_code=201,
)
def retry_decision_analysis_run(
    scenario_id: str,
    analysis_id: str,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> DecisionAnalysisRun:
    original = require_store().get_decision_analysis_run(scenario_id, analysis_id)
    if not original:
        raise HTTPException(404, "经营分析记录不存在")
    if original.status not in {"FAILED", "INTERRUPTED"}:
        raise HTTPException(
            409,
            {
                "code": "ANALYSIS_RETRY_NOT_ALLOWED",
                "message": "只有失败或中断的经营分析可以显式重试",
                "analysis_status": original.status,
            },
        )
    if not original.request_snapshot:
        raise HTTPException(
            409,
            {"code": "ANALYSIS_REQUEST_SNAPSHOT_MISSING", "message": "旧分析缺少请求快照，无法安全重试"},
        )
    plan = require_store().get_plan_version(scenario_id, original.plan_version_id)
    if not plan:
        raise HTTPException(409, {"code": "ANALYSIS_PLAN_MISSING", "message": "原分析引用的方案已不存在"})
    if original.integrity_status == "FAILED":
        raise HTTPException(
            409,
            {"code": "ANALYSIS_INTEGRITY_FAILED", "message": "原分析记录完整性校验失败，不能精确重试"},
        )
    if original.input_manifest is None or original.runtime_manifest is None:
        raise HTTPException(
            409,
            {
                "code": "ANALYSIS_EXACT_RETRY_MANIFEST_MISSING",
                "message": "原分析没有完整输入和运行时清单，不能声称精确重试；请按当前上下文重跑",
            },
        )
    provider = require_store().travel_provider
    mismatch: list[str] = []
    if original.build_sha != decision_build_sha():
        mismatch.append("build_sha")
    if content_hash(original.runtime_manifest) != content_hash(decision_runtime_manifest()):
        mismatch.append("runtime_manifest")
    if original.travel_model_fingerprint != provider.fingerprint:
        mismatch.append("travel_model_fingerprint")
    if original.scenario_snapshot_hash != plan.scenario_snapshot_hash:
        mismatch.append("scenario_snapshot_hash")
    if original.schedule_hash != schedule_signature(plan.selected):
        mismatch.append("schedule_hash")
    if mismatch:
        raise HTTPException(
            409,
            {
                "code": "ANALYSIS_EXACT_RETRY_CONTEXT_CHANGED",
                "message": "原分析的冻结输入或运行版本已变化，不能冒充精确重试；请改用按当前上下文重跑",
                "changed_fields": mismatch,
                "rerun_current_endpoint": f"/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}/rerun-current",
            },
        )
    request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(original.request_snapshot)
    retry_context = DecisionAnalysisContext(
        analysis_scope=original.analysis_scope,
        current_execution_watermark=original.current_execution_watermark,
        analysis_as_of_time=original.analysis_as_of_time,
        execution_context_hash=original.execution_context_hash,
        actual_execution_included=original.actual_execution_included,
        active_booking_ids=original.active_booking_ids,
    )
    key = idempotency_key
    if not 8 <= len(key) <= 120:
        raise HTTPException(422, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "幂等键长度须为 8–120"})
    namespace = f"{scenario_id}:analysis-retry"
    fingerprint = content_hash(
        {"original_analysis_id": original.id, "input_hash": original.input_hash, "request": request}
    )
    try:
        existing = require_store().get_command_record(namespace, key, fingerprint)
    except PublicationConflict as error:
        raise HTTPException(409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": str(error)}) from error
    retry_parent = original
    if existing and existing["status"] != "FAILED_RETRYABLE":
        if existing["resource_id"]:
            replay = require_store().get_decision_analysis_run(scenario_id, existing["resource_id"])
            if replay:
                response.status_code = status.HTTP_202_ACCEPTED if replay.status == "RUNNING" else status.HTTP_200_OK
                return replay
        raise HTTPException(
            409,
            detail={"code": "IDEMPOTENT_REQUEST_IN_PROGRESS", "message": "相同精确重试正在执行"},
        )
    if existing and existing["status"] == "FAILED_RETRYABLE" and existing["resource_id"]:
        replay = require_store().get_decision_analysis_run(scenario_id, existing["resource_id"])
        if replay and replay.status in {"FAILED", "INTERRUPTED"}:
            retry_parent = replay
    created = require_store().begin_command_record(
        namespace,
        key,
        fingerprint,
        status="RESERVED",
        resource_type="decision_analysis_run",
        resource_id=None,
        payload={"original_analysis_id": original.id},
    )
    if not created:
        raise HTTPException(409, detail={"code": "IDEMPOTENT_REQUEST_IN_PROGRESS", "message": "相同精确重试正在执行"})

    def record_reserved(reserved: DecisionAnalysisRun) -> None:
        require_store().update_command_record(
            namespace,
            key,
            fingerprint,
            status="ANALYSIS_RESERVED",
            resource_type="decision_analysis_run",
            resource_id=reserved.id,
            payload={"original_analysis_id": original.id, "analysis_id": reserved.id},
        )

    try:
        retried = execute_decision_analysis_run(
            scenario_id,
            plan,
            request,
            response,
            retry_of=retry_parent,
            context_override=retry_context,
            on_reserved=record_reserved,
        )
    except Exception:
        require_store().update_command_record(
            namespace,
            key,
            fingerprint,
            status="FAILED_RETRYABLE",
            resource_type="decision_analysis_run",
            resource_id=None,
            payload={"original_analysis_id": original.id, "stage": "EXECUTION_FAILED"},
        )
        raise
    require_store().update_command_record(
        namespace,
        key,
        fingerprint,
        status="COMPLETED",
        resource_type="decision_analysis_run",
        resource_id=retried.id,
        payload={"analysis_id": retried.id, "attempt_number": retried.attempt_number},
    )
    return retried


@router.post(
    "/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}/rerun-current",
    response_model=DecisionAnalysisRun,
    status_code=201,
)
def rerun_decision_analysis_current(
    scenario_id: str,
    analysis_id: str,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> DecisionAnalysisRun:
    original = require_store().get_decision_analysis_run(scenario_id, analysis_id)
    if not original:
        raise HTTPException(404, "经营分析记录不存在")
    plan = require_store().get_plan_version(scenario_id, original.plan_version_id)
    if not plan:
        raise HTTPException(409, {"code": "ANALYSIS_PLAN_MISSING", "message": "原分析引用的方案已不存在"})
    if not original.request_snapshot:
        raise HTTPException(409, {"code": "ANALYSIS_REQUEST_SNAPSHOT_MISSING", "message": "旧分析缺少请求快照"})
    if original.integrity_status is AnalysisIntegrityStatus.failed:
        raise HTTPException(
            409,
            {"code": "ANALYSIS_INTEGRITY_FAILED", "message": "原分析请求快照未通过完整性校验，不能重跑"},
        )
    if not 8 <= len(idempotency_key) <= 120:
        raise HTTPException(422, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "幂等键长度须为 8–120"})
    request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(original.request_snapshot)
    namespace = f"{scenario_id}:analysis-rerun-current"
    fingerprint = content_hash(
        {
            "original_analysis_id": original.id,
            "request": request,
            "current_plan_manifest_hash": plan.publication_manifest_hash,
            "current_build_sha": decision_build_sha(),
            "current_runtime_manifest": decision_runtime_manifest(),
        }
    )
    try:
        existing = require_store().get_command_record(namespace, idempotency_key, fingerprint)
    except PublicationConflict as error:
        raise HTTPException(409, detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": str(error)}) from error
    retry_parent: DecisionAnalysisRun | None = None
    if existing and existing["status"] != "FAILED_RETRYABLE":
        if existing["resource_id"]:
            replay = require_store().get_decision_analysis_run(scenario_id, existing["resource_id"])
            if replay:
                response.status_code = status.HTTP_202_ACCEPTED if replay.status == "RUNNING" else status.HTTP_200_OK
                return replay
        raise HTTPException(
            409,
            detail={"code": "IDEMPOTENT_REQUEST_IN_PROGRESS", "message": "相同当前环境重跑正在执行"},
        )
    if existing and existing["status"] == "FAILED_RETRYABLE" and existing["resource_id"]:
        replay = require_store().get_decision_analysis_run(scenario_id, existing["resource_id"])
        if replay and replay.status in {"FAILED", "INTERRUPTED"}:
            retry_parent = replay
    created = require_store().begin_command_record(
        namespace,
        idempotency_key,
        fingerprint,
        status="RESERVED",
        resource_type="decision_analysis_run",
        resource_id=None,
        payload={"original_analysis_id": original.id},
    )
    if not created:
        raise HTTPException(409, detail={"code": "IDEMPOTENT_REQUEST_IN_PROGRESS", "message": "相同重跑正在执行"})

    def record_reserved(reserved: DecisionAnalysisRun) -> None:
        require_store().update_command_record(
            namespace,
            idempotency_key,
            fingerprint,
            status="ANALYSIS_RESERVED",
            resource_type="decision_analysis_run",
            resource_id=reserved.id,
            payload={"original_analysis_id": original.id, "analysis_id": reserved.id},
        )

    try:
        rerun = execute_decision_analysis_run(
            scenario_id,
            plan,
            request,
            response,
            retry_of=retry_parent,
            force_new=retry_parent is None,
            supersedes_analysis_id=original.id,
            on_reserved=record_reserved,
        )
    except Exception:
        require_store().update_command_record(
            namespace,
            idempotency_key,
            fingerprint,
            status="FAILED_RETRYABLE",
            resource_type="decision_analysis_run",
            resource_id=None,
            payload={"original_analysis_id": original.id, "stage": "EXECUTION_FAILED"},
        )
        raise
    require_store().update_command_record(
        namespace,
        idempotency_key,
        fingerprint,
        status="COMPLETED",
        resource_type="decision_analysis_run",
        resource_id=rerun.id,
        payload={"analysis_id": rerun.id, "attempt_number": rerun.attempt_number},
    )
    return rerun


@router.get(
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/analysis-runs",
    response_model=list[DecisionAnalysisRun],
)
def list_decision_analysis_runs(scenario_id: str, version_id: str) -> list[DecisionAnalysisRun]:
    plan = require_store().get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return require_store().list_decision_analysis_runs(scenario_id, plan.id)


@router.get(
    "/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}",
    response_model=DecisionAnalysisRun,
)
def get_decision_analysis_run(scenario_id: str, analysis_id: str) -> DecisionAnalysisRun:
    run = require_store().get_decision_analysis_run(scenario_id, analysis_id)
    if not run:
        raise HTTPException(404, "经营分析记录不存在")
    return run


@router.get(
    "/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}/artifacts",
    response_model=list[DecisionAnalysisArtifact],
)
def list_decision_analysis_artifacts(
    scenario_id: str,
    analysis_id: str,
) -> list[DecisionAnalysisArtifact]:
    run = require_store().get_decision_analysis_run(scenario_id, analysis_id)
    if not run:
        raise HTTPException(404, "经营分析记录不存在")
    return require_store().list_decision_analysis_artifacts(scenario_id, run.id)


@router.get(
    "/api/scenarios/{scenario_id}/analysis-runs/{analysis_id}/artifacts/{artifact_id}",
    response_model=DecisionAnalysisArtifact,
)
def get_decision_analysis_artifact(
    scenario_id: str,
    analysis_id: str,
    artifact_id: str,
) -> DecisionAnalysisArtifact:
    run = require_store().get_decision_analysis_run(scenario_id, analysis_id)
    if not run:
        raise HTTPException(404, "经营分析记录不存在")
    artifact = require_store().get_decision_analysis_artifact(scenario_id, run.id, artifact_id)
    if not artifact:
        raise HTTPException(404, "经营分析证据不存在")
    return artifact


def paired_metric_summary(
    before_values: list[float],
    after_values: list[float],
    *,
    higher_is_better: bool,
    conditioning_event: str | None = None,
) -> PairedMetricSummary | None:
    if len(before_values) != len(after_values):
        raise HTTPException(
            409,
            detail={"code": "PAIRED_TRIAL_EVIDENCE_MISMATCH", "message": "配对 trial 证据数量不一致"},
        )
    if not before_values:
        return None
    deltas = [after - before for before, after in zip(before_values, after_values, strict=True)]
    wins = sum(1 for delta in deltas if delta > 0) if higher_is_better else sum(1 for delta in deltas if delta < 0)
    losses = sum(1 for delta in deltas if delta < 0) if higher_is_better else sum(1 for delta in deltas if delta > 0)
    ties = len(deltas) - wins - losses
    if conditioning_event and len(deltas) < 20:
        return PairedMetricSummary(
            win_count=wins,
            tie_count=ties,
            loss_count=losses,
            effective_sample_count=len(deltas),
            conditioning_event=conditioning_event,
            interval_method="NOT_ESTIMATED",
            interpretation_status="INSUFFICIENT_EVENT_TRIALS",
        )
    mean_delta = statistics.fmean(deltas)
    if len(deltas) == 1:
        ci_low = ci_high = mean_delta
    else:
        rng = random.Random(int(content_hash(deltas)[:16], 16))
        samples = sorted(statistics.fmean(deltas[rng.randrange(len(deltas))] for _ in deltas) for _ in range(2_000))
        ci_low = samples[int(0.025 * (len(samples) - 1))]
        ci_high = samples[int(0.975 * (len(samples) - 1))]
    return PairedMetricSummary(
        mean_delta=round(mean_delta, 6),
        ci_low=round(ci_low, 6),
        ci_high=round(ci_high, 6),
        win_count=wins,
        tie_count=ties,
        loss_count=losses,
        effective_sample_count=len(deltas),
        conditioning_event=conditioning_event,
    )


@router.post("/api/scenarios/{scenario_id}/risk-comparison", response_model=RiskComparisonRun)
def compare_plan_risk_paired(
    scenario_id: str,
    request: RiskSimulationRequest,
    before: str = Query(...),
    after: str = Query(...),
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
) -> RiskComparisonRun:
    if not 8 <= len(idempotency_key) <= 120:
        raise HTTPException(422, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "幂等键长度须为 8–120"})
    before_plan = require_plan_for_use(scenario_id, before, PlanUseCase.compare)
    after_plan = require_plan_for_use(scenario_id, after, PlanUseCase.compare)
    request_fingerprint = content_hash(
        {"scenario_id": scenario_id, "before": before_plan.id, "after": after_plan.id, "request": request}
    )
    try:
        existing = require_store().risk_comparison_for_idempotency(
            scenario_id,
            idempotency_key,
            request_fingerprint,
        )
    except PublicationConflict as error:
        raise HTTPException(
            409,
            detail={"code": error.code, "message": str(error), **error.details},
        ) from error
    if existing is not None:
        return existing
    if before_plan.scenario_snapshot_hash != after_plan.scenario_snapshot_hash:
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_SIMULATION_SNAPSHOT_MISMATCH",
                "message": "两个方案的业务快照不同，不能声称使用共同随机场景做配对比较",
            },
        )
    before_context = resolve_decision_analysis_context(scenario_id, before_plan, request.analysis_scope)
    after_context = resolve_decision_analysis_context(scenario_id, after_plan, request.analysis_scope)
    if before_context != after_context:
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_ANALYSIS_CONTEXT_MISMATCH",
                "message": "两个方案的分析范围、时点或发布上下文不同，不能创建配对分析",
            },
        )
    if not before_plan.scenario_snapshot or not after_plan.scenario_snapshot:
        raise HTTPException(409, detail={"code": "PAIRED_SIMULATION_SNAPSHOT_MISSING", "message": "方案快照缺失"})
    before_seed = before_plan.scenario_snapshot.seed if request.seed is None else request.seed
    after_seed = after_plan.scenario_snapshot.seed if request.seed is None else request.seed
    before_scenario_set_identity = content_hash(
        build_simulation_scenario_set(
            before_plan.scenario_snapshot,
            request,
            before_seed,
            before_context.analysis_as_of_time or 0,
        )
    )
    after_scenario_set_identity = content_hash(
        build_simulation_scenario_set(
            after_plan.scenario_snapshot,
            request,
            after_seed,
            after_context.analysis_as_of_time or 0,
        )
    )
    if before_scenario_set_identity != after_scenario_set_identity:
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_SCENARIO_SET_MISMATCH",
                "message": "两个方案无法构造同一共同随机场景集，未创建子分析",
            },
        )
    payload = request.model_dump(mode="json", exclude={"analysis_scope"})
    run_request = TypeAdapter(DecisionAnalysisRunRequest).validate_python(
        {"analysis_type": "RISK", "analysis_scope": request.analysis_scope, "request": payload}
    )
    before_run = execute_decision_analysis_run(
        scenario_id,
        before_plan,
        run_request,
        Response(),
        context_override=before_context,
    )
    after_run = execute_decision_analysis_run(
        scenario_id,
        after_plan,
        run_request,
        Response(),
        context_override=after_context,
    )
    if (
        before_run.status != "COMPLETED"
        or after_run.status != "COMPLETED"
        or before_run.effective_integrity is not AnalysisIntegrityStatus.verified
        or after_run.effective_integrity is not AnalysisIntegrityStatus.verified
        or not isinstance(before_run.result, RiskSimulationResult)
        or not isinstance(after_run.result, RiskSimulationResult)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_SIMULATION_FAILED",
                "message": "至少一个方案的风险分析失败",
                "before_error": before_run.error,
                "after_error": after_run.error,
            },
        )
    if before_run.result.simulation_scenario_set_hash != after_run.result.simulation_scenario_set_hash:
        raise HTTPException(
            409,
            detail={"code": "PAIRED_SCENARIO_SET_MISMATCH", "message": "两次分析没有绑定同一共同随机场景集"},
        )
    before_artifact = (
        require_store().get_decision_analysis_artifact(
            scenario_id,
            before_run.id,
            before_run.result.trial_outcome_artifact_id,
        )
        if before_run.result.trial_outcome_artifact_id
        else None
    )
    after_artifact = (
        require_store().get_decision_analysis_artifact(
            scenario_id,
            after_run.id,
            after_run.result.trial_outcome_artifact_id,
        )
        if after_run.result.trial_outcome_artifact_id
        else None
    )
    if (
        not isinstance(before_artifact, RiskTrialOutcomeArtifact)
        or not isinstance(after_artifact, RiskTrialOutcomeArtifact)
        or before_artifact.effective_integrity is not AnalysisIntegrityStatus.verified
        or after_artifact.effective_integrity is not AnalysisIntegrityStatus.verified
        or before_artifact.scenario_set_hash != after_artifact.scenario_set_hash
    ):
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_TRIAL_EVIDENCE_INVALID",
                "message": "配对风险比较缺少完整且使用同一场景集的 trial 证据",
            },
        )
    before_scenario_artifact = (
        require_store().get_decision_analysis_artifact(
            scenario_id,
            before_run.id,
            before_run.result.scenario_set_artifact_id,
        )
        if before_run.result.scenario_set_artifact_id
        else None
    )
    after_scenario_artifact = (
        require_store().get_decision_analysis_artifact(
            scenario_id,
            after_run.id,
            after_run.result.scenario_set_artifact_id,
        )
        if after_run.result.scenario_set_artifact_id
        else None
    )
    if (
        not isinstance(before_scenario_artifact, SimulationScenarioSetArtifact)
        or not isinstance(after_scenario_artifact, SimulationScenarioSetArtifact)
        or before_scenario_artifact.effective_integrity is not AnalysisIntegrityStatus.verified
        or after_scenario_artifact.effective_integrity is not AnalysisIntegrityStatus.verified
        or before_scenario_artifact.scenario_set_hash != before_artifact.scenario_set_hash
        or after_scenario_artifact.scenario_set_hash != after_artifact.scenario_set_hash
    ):
        raise HTTPException(
            409,
            detail={
                "code": "PAIRED_SCENARIO_EVIDENCE_INVALID",
                "message": "配对风险比较缺少完整的共同场景集证据",
            },
        )
    before_metrics = sorted(before_artifact.metrics, key=lambda item: item.trial)
    after_metrics = sorted(after_artifact.metrics, key=lambda item: item.trial)
    if [item.trial for item in before_metrics] != [item.trial for item in after_metrics]:
        raise HTTPException(
            409,
            detail={"code": "PAIRED_TRIAL_INDEX_MISMATCH", "message": "两个分析的 trial 编号不一致"},
        )
    delta = {
        "expected_sla_on_time_rate": round(
            after_run.result.expected_sla_on_time_rate - before_run.result.expected_sla_on_time_rate,
            4,
        ),
        "expected_overtime_minutes": round(
            after_run.result.expected_overtime_minutes - before_run.result.expected_overtime_minutes,
            2,
        ),
        "additional_disruption_probability": round(
            after_run.result.additional_disruption_probability - before_run.result.additional_disruption_probability,
            4,
        ),
        "expected_total_unserved_orders": round(
            after_run.result.expected_total_unserved_orders - before_run.result.expected_total_unserved_orders,
            2,
        ),
    }
    comparison_input_payload = {
        "policy_version": "FIELD_SERVICE_RISK_COMPARISON_INPUT_V1",
        "scenario_id": scenario_id,
        "before_analysis_id": before_run.id,
        "after_analysis_id": after_run.id,
        "before_analysis_manifest_hash": before_run.analysis_manifest_hash,
        "after_analysis_manifest_hash": after_run.analysis_manifest_hash,
        "before_trial_artifact_id": before_artifact.id,
        "after_trial_artifact_id": after_artifact.id,
        "before_trial_artifact_hash": before_artifact.artifact_hash,
        "after_trial_artifact_hash": after_artifact.artifact_hash,
        "before_scenario_artifact_id": before_scenario_artifact.id,
        "after_scenario_artifact_id": after_scenario_artifact.id,
        "before_scenario_artifact_hash": before_scenario_artifact.artifact_hash,
        "after_scenario_artifact_hash": after_scenario_artifact.artifact_hash,
        "scenario_set_hash": before_artifact.scenario_set_hash,
        "trials": len(before_metrics),
    }
    paired_published_sla = paired_metric_summary(
        [item.published_commitment_sla_rate for item in before_metrics],
        [item.published_commitment_sla_rate for item in after_metrics],
        higher_is_better=True,
    )
    paired_all_demand_sla = paired_metric_summary(
        [item.all_demand_sla_rate for item in before_metrics],
        [item.all_demand_sla_rate for item in after_metrics],
        higher_is_better=True,
    )
    emergency_pairs = [
        (before_item, after_item)
        for before_item, after_item in zip(before_metrics, after_metrics, strict=True)
        if before_item.emergency_event and after_item.emergency_event
    ]
    paired_unconditional_emergency_completion = paired_metric_summary(
        [float(item.emergency_completed) for item in before_metrics],
        [float(item.emergency_completed) for item in after_metrics],
        higher_is_better=True,
    )
    assert paired_unconditional_emergency_completion is not None
    paired_emergency_completion = paired_metric_summary(
        [float(before_item.emergency_completed) for before_item, _after_item in emergency_pairs],
        [float(after_item.emergency_completed) for _before_item, after_item in emergency_pairs],
        higher_is_better=True,
        conditioning_event="EMERGENCY_EVENT_OCCURRED",
    )
    paired_emergency_on_time = paired_metric_summary(
        [float(before_item.emergency_on_time) for before_item, _after_item in emergency_pairs],
        [float(after_item.emergency_on_time) for _before_item, after_item in emergency_pairs],
        higher_is_better=True,
        conditioning_event="EMERGENCY_EVENT_OCCURRED",
    )
    paired_overtime = paired_metric_summary(
        [float(item.total_overtime_minutes) for item in before_metrics],
        [float(item.total_overtime_minutes) for item in after_metrics],
        higher_is_better=False,
    )
    paired_unserved = paired_metric_summary(
        [float(item.total_unserved_orders) for item in before_metrics],
        [float(item.total_unserved_orders) for item in after_metrics],
        higher_is_better=False,
    )
    paired_disruption = paired_metric_summary(
        [float(item.disrupted) for item in before_metrics],
        [float(item.disrupted) for item in after_metrics],
        higher_is_better=False,
    )
    assert paired_published_sla is not None
    assert paired_all_demand_sla is not None
    assert paired_overtime is not None
    assert paired_unserved is not None
    assert paired_disruption is not None
    business_result = RiskComparisonResult(
        paired_published_sla_delta=paired_published_sla,
        paired_all_demand_sla_delta=paired_all_demand_sla,
        paired_emergency_completion_delta=paired_emergency_completion,
        paired_emergency_on_time_delta=paired_emergency_on_time,
        paired_unconditional_emergency_completion_impact=paired_unconditional_emergency_completion,
        paired_overtime_delta=paired_overtime,
        paired_unserved_delta=paired_unserved,
        paired_disruption_delta=paired_disruption,
        delta=delta,
    )
    comparison = RiskComparisonRun(
        id="pending",
        scenario_id=scenario_id,
        number=0,
        before_analysis_id=before_run.id,
        after_analysis_id=after_run.id,
        before_plan_version_id=before_plan.id,
        after_plan_version_id=after_plan.id,
        scenario_set_hash=before_artifact.scenario_set_hash,
        comparison_input_hash=content_hash(comparison_input_payload),
        before_analysis_manifest_hash=before_run.analysis_manifest_hash or "",
        after_analysis_manifest_hash=after_run.analysis_manifest_hash or "",
        before_trial_artifact_id=before_artifact.id,
        after_trial_artifact_id=after_artifact.id,
        before_trial_artifact_hash=before_artifact.artifact_hash,
        after_trial_artifact_hash=after_artifact.artifact_hash,
        before_scenario_artifact_id=before_scenario_artifact.id,
        after_scenario_artifact_id=after_scenario_artifact.id,
        before_scenario_artifact_hash=before_scenario_artifact.artifact_hash,
        after_scenario_artifact_hash=after_scenario_artifact.artifact_hash,
        trials=len(before_metrics),
        result=business_result,
        paired_sla_delta=paired_published_sla,
        paired_all_demand_sla_delta=paired_all_demand_sla,
        paired_emergency_completion_delta=paired_emergency_completion,
        paired_emergency_on_time_delta=paired_emergency_on_time,
        paired_overtime_delta=paired_overtime,
        paired_unserved_delta=paired_unserved,
        paired_disruption_delta=paired_disruption,
        delta=delta,
        comparison_hash="pending",
        created_at=_now(),
    )
    try:
        return require_store().save_risk_comparison(
            comparison,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
    except PublicationConflict as error:
        raise HTTPException(
            409,
            detail={"code": error.code, "message": str(error), **error.details},
        ) from error


@router.get(
    "/api/scenarios/{scenario_id}/risk-comparisons/{comparison_id}",
    response_model=RiskComparisonRun,
)
def get_risk_comparison(scenario_id: str, comparison_id: str) -> RiskComparisonRun:
    comparison = require_store().get_risk_comparison(scenario_id, comparison_id)
    if comparison is None:
        raise HTTPException(404, "风险比较记录不存在")
    return comparison


def schedule_change_rows(
    before: ScheduleResult,
    after: ScheduleResult,
) -> list[dict[str, str | int | None]]:
    before_by_id = {item.work_order_id: item for item in before.assignments}
    after_by_id = {item.work_order_id: item for item in after.assignments}
    before_unassigned = {item.work_order_id: item.reason.value for item in before.unassigned}
    after_unassigned = {item.work_order_id: item.reason.value for item in after.unassigned}
    changed: list[dict[str, str | int | None]] = []
    for order_id in sorted(set(before_by_id) | set(after_by_id) | set(before_unassigned) | set(after_unassigned)):
        old = before_by_id.get(order_id)
        new = after_by_id.get(order_id)
        old_values = {
            "disposition": "ASSIGNED" if old else f"UNASSIGNED:{before_unassigned.get(order_id, 'MISSING')}",
            "technician": old.technician_id if old else None,
            "sequence": old.sequence if old else None,
            "arrival": old.arrival_time if old else None,
            "start": old.start_time if old else None,
            "finish": old.finish_time if old else None,
            "travel": old.travel_minutes if old else None,
            "locked": int(old.locked) if old else None,
            "source_sequence": old.source_sequence if old else None,
            "source_assignment_hash": old.source_assignment_hash if old else None,
        }
        new_values = {
            "disposition": "ASSIGNED" if new else f"UNASSIGNED:{after_unassigned.get(order_id, 'MISSING')}",
            "technician": new.technician_id if new else None,
            "sequence": new.sequence if new else None,
            "arrival": new.arrival_time if new else None,
            "start": new.start_time if new else None,
            "finish": new.finish_time if new else None,
            "travel": new.travel_minutes if new else None,
            "locked": int(new.locked) if new else None,
            "source_sequence": new.source_sequence if new else None,
            "source_assignment_hash": new.source_assignment_hash if new else None,
        }
        changed_fields = [field for field in old_values if old_values[field] != new_values[field]]
        if changed_fields:
            changed.append(
                {
                    "work_order_id": order_id,
                    "changed_fields": ",".join(changed_fields),
                    **{f"before_{field}": value for field, value in old_values.items()},
                    **{f"after_{field}": value for field, value in new_values.items()},
                    "reason": "、".join(changed_fields) + " 发生变化",
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
        plan_changes=schedule_change_rows(current_plan.selected, source.selected) if current_plan else [],
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
                "code": "SCENARIO_REVISION_CONFLICT",
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": request.expected_revision,
                "current_revision": current.revision,
            },
        )
    source = require_plan_for_use(scenario_id, version_id, PlanUseCase.restore)
    if not source.scenario_snapshot:
        raise HTTPException(404, "方案业务快照不存在")
    require_frozen_plan_integrity(source)
    current_fingerprint = content_hash(current.model_dump(exclude={"revision"}))
    source_fingerprint = content_hash(source.scenario_snapshot.model_dump(exclude={"revision"}))
    if current_fingerprint != source_fingerprint:
        store = require_store()
        current_plan = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
        preview = build_rollback_preview(current, source, current_plan)
        raise HTTPException(
            409,
            detail={
                "code": "PLAN_SCENARIO_MISMATCH",
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
    stability_baseline = None
    if selected.kind == "replan":
        baseline_version_id = source.stability_baseline_version_id or source.source_version_id
        if baseline_version_id:
            stability_baseline = require_plan_for_use(scenario_id, baseline_version_id, PlanUseCase.replay)
    if selected.kind != "replan":
        selected = normalize_schedule(
            current,
            selected,
            stability_baseline.selected if stability_baseline else None,
            provider=require_store().travel_provider,
        )
    reservation, run = start_schedule_run(
        current,
        "activate",
        source=source,
        solver_name="plan-activation",
        solver_config_hash=selected.solver_config_hash,
        expected_active_plan_version_id=request.expected_active_plan_version_id,
        check_active_plan="expected_active_plan_version_id" in request.model_fields_set,
        command_fingerprint=fingerprint,
    )
    source = reservation.source_plan or source
    published = publish_selected(
        current,
        selected,
        "activate",
        artifacts=[artifact("selected", selected, selected.strategy)],
        source=source,
        stability_baseline=stability_baseline,
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
    "/api/scenarios/{scenario_id}/plan-versions/{version_id}/reattest",
    response_model=PlanVersion,
)
def reattest_plan_version(
    scenario_id: str,
    version_id: str,
    request: ReattestPlanRequest,
) -> PlanVersion:
    """Create a new, fully attested V from a view-only legacy plan."""
    current = require_scenario(scenario_id)
    if current.revision != request.expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_REVISION_CONFLICT",
                "message": "业务数据已变化，请刷新后重试",
                "expected_revision": request.expected_revision,
                "current_revision": current.revision,
            },
        )
    if any(order.status is not WorkOrderStatus.pending for order in current.work_orders):
        raise HTTPException(
            409,
            detail={
                "code": "REATTESTATION_EXECUTION_CONTEXT_REQUIRED",
                "message": "已有工单开始或完成服务；请基于当前执行水位重排，不能重新认证旧快照",
            },
        )
    source = require_store().get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    if source.effective_integrity is AnalysisIntegrityStatus.failed:
        raise HTTPException(
            409,
            detail={"code": "PLAN_INTEGRITY_FAILED", "message": "损坏的方案不能重新验证"},
        )
    if source.effective_integrity is AnalysisIntegrityStatus.verified:
        raise HTTPException(
            409,
            detail={"code": "PLAN_ALREADY_ATTESTED", "message": "该方案已经通过当前发布证明"},
        )
    if source.selected.kind == "replan" and source.publication_planning_context is None:
        raise HTTPException(
            409,
            detail={
                "code": "LEGACY_REPLAN_CONTEXT_REQUIRED",
                "message": "旧重排方案缺少发布时路线入口和执行水位，不能安全重新认证",
            },
        )
    if request.mode is ReattestationMode.planning_equivalent:
        provider = require_store().travel_provider
        current_fingerprint = content_hash(plan_scoped_assignment_feasibility_payload(current, source, provider))
        source_fingerprint = content_hash(
            plan_scoped_assignment_feasibility_payload(source.scenario_snapshot, source, provider)
        )
        mismatch_code = "REATTESTATION_PLANNING_INPUT_MISMATCH"
        mismatch_message = "当前分配可行性输入与历史冻结快照不同，不能按规划等价模式重新认证"
    else:
        current_fingerprint = content_hash(current.model_dump(exclude={"revision"}))
        source_fingerprint = content_hash(source.scenario_snapshot.model_dump(exclude={"revision"}))
        mismatch_code = "REATTESTATION_SNAPSHOT_MISMATCH"
        mismatch_message = "当前业务数据与历史冻结快照不同，不能把该历史计划重新发布为当前方案"
    if current_fingerprint != source_fingerprint:
        raise HTTPException(
            409,
            detail={
                "code": mismatch_code,
                "message": mismatch_message,
            },
        )
    publication_key, fingerprint, existing = publication_retry(
        "reattest",
        current,
        request.idempotency_key,
        {
            "source_version_id": source.id,
            "expected_revision": request.expected_revision,
            "mode": request.mode.value,
        },
    )
    if existing:
        return existing
    selected = source.selected.model_copy(deep=True)
    selected.id = f"SCH-{scenario_id}-reattest-{uuid.uuid4().hex[:8]}"
    selected.created_at = _now()
    selected.source_schedule_id = source.selected.id
    selected.scenario_revision = current.revision
    selected.solution_found = True
    selected = bind_replayed_solver_policy(selected, current, "plan-reattestation")
    selected.solver_note += f" 重新认证模式：{request.mode.value}。"
    if selected.kind != "replan":
        selected = normalize_schedule(current, selected, provider=require_store().travel_provider)
    reservation, run = start_schedule_run(
        current,
        "reattest",
        source=source,
        solver_name="plan-reattestation",
        solver_config_hash=selected.solver_config_hash,
        expected_active_plan_version_id=request.expected_active_plan_version_id,
        check_active_plan="expected_active_plan_version_id" in request.model_fields_set,
        command_fingerprint=fingerprint,
    )
    source = reservation.source_plan or source
    published = publish_selected(
        current,
        selected,
        "reattest",
        artifacts=[artifact("selected", selected, selected.strategy)],
        source=source,
        relation="reattested_from",
        label=f"重新验证自 V{source.number:03d} · {source.label}"[:60].strip(),
        run=run,
        idempotency_key=publication_key,
        request_fingerprint=fingerprint,
        reattestation_mode=request.mode,
    )
    plan = require_store().active_plan_version(scenario_id)
    if not plan or plan.selected.id != published.id:
        raise HTTPException(500, "方案已发布但无法读取重新验证版本")
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
    source = require_plan_for_use(scenario_id, version_id, PlanUseCase.clone)
    if not source.scenario_snapshot:
        raise HTTPException(404, "方案业务快照不存在")
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
        raise publication_conflict_to_http(error) from error


@router.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}/rollback-preview", response_model=RollbackPreview)
def rollback_plan_preview(scenario_id: str, version_id: str) -> RollbackPreview:
    current = require_scenario(scenario_id)
    store = require_store()
    source = require_plan_for_use(scenario_id, version_id, PlanUseCase.restore)
    if not source.scenario_snapshot:
        raise HTTPException(404, "方案业务快照不存在")
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
    source = require_plan_for_use(scenario_id, version_id, PlanUseCase.restore)
    if not source.scenario_snapshot:
        raise HTTPException(404, "方案业务快照不存在")
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
        raise publication_conflict_to_http(error) from error
    if existing:
        return existing
    if current.revision != request.expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "SCENARIO_REVISION_CONFLICT",
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
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_CONFIRMATION_EXPIRED",
                "message": "回滚确认已过期，请重新查看差异",
                "expected_active_plan_id": request.expected_active_plan_version_id,
                "current_active_plan_id": preview.current_plan_version_id,
            },
        )
    if preview.completed_work_orders_reopened:
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_REOPENS_COMPLETED_WORK",
                "message": "回滚会重新打开已有执行事件的已完成工单，禁止执行",
                "completed_work_orders_reopened": preview.completed_work_orders_reopened,
            },
        )
    if preview.started_work_orders_reopened:
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_REOPENS_STARTED_WORK",
                "message": "回滚会重新打开服务中的工单，禁止执行",
                "started_work_orders_reopened": preview.started_work_orders_reopened,
            },
        )
    if preview.executed_work_orders_deleted:
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_DELETES_EXECUTED_WORK",
                "message": "回滚会删除已有执行记录的工单，禁止执行",
                "executed_work_orders_deleted": preview.executed_work_orders_deleted,
                "affected_execution_event_ids": preview.affected_execution_event_ids,
            },
        )
    if preview.removed_work_orders and not request.allow_delete_new_orders:
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_DELETES_NEW_WORK",
                "message": "回滚会删除历史版本之后新增的工单，默认禁止",
                "removed_work_orders": preview.removed_work_orders,
            },
        )
    expected_active_plan_id = (
        request.expected_active_plan_version_id
        if "expected_active_plan_version_id" in request.model_fields_set
        else preview.current_plan_version_id
    )
    reservation, run = start_schedule_run(
        current,
        "restore",
        source=source,
        solver_name="plan-restore",
        solver_config_hash=source.selected.solver_config_hash,
        expected_active_plan_version_id=expected_active_plan_id,
        check_active_plan=True,
        command_fingerprint=fingerprint,
    )
    current = reservation.scenario_snapshot
    source = reservation.source_plan or source
    if not source.scenario_snapshot:
        raise HTTPException(409, detail={"code": "SOURCE_PLAN_SNAPSHOT_MISSING", "message": "来源方案快照缺失"})
    restored = source.scenario_snapshot.model_copy(deep=True)
    restored.id = scenario_id
    restored.revision = current.revision + 1
    selected = source.selected.model_copy(deep=True)
    selected.id = f"SCH-{scenario_id}-restore-{uuid.uuid4().hex[:8]}"
    selected.created_at = _now()
    selected.source_schedule_id = source.selected.id
    selected.scenario_revision = restored.revision
    selected.solution_found = True
    stability_baseline = None
    if selected.kind == "replan":
        baseline_version_id = source.stability_baseline_version_id or source.source_version_id
        if baseline_version_id:
            stability_baseline = require_store().get_plan_version(scenario_id, baseline_version_id)
    selected = bind_replayed_solver_policy(selected, restored, "plan-restore")
    if selected.kind == "replan":
        selected = recompute_business_result(
            restored,
            selected,
            stability_baseline.selected if stability_baseline else None,
            require_store().travel_provider,
        )
    else:
        selected = normalize_schedule(restored, selected, provider=require_store().travel_provider)
    run.scenario_revision = restored.revision
    run.scenario_snapshot_hash = content_hash(restored)
    run.solver_config_hash = selected.solver_config_hash
    run = require_store().save_schedule_run(run)
    verification = validate_result(
        restored,
        selected,
        stability_baseline.selected if stability_baseline else None,
    )
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}",
        run_id=run.id,
        scenario_id=scenario_id,
        scenario_revision=restored.revision,
        scenario_snapshot_hash=content_hash(restored),
        source_plan_version_id=source.id,
        expected_active_plan_version_id=run.expected_active_plan_version_id,
        reservation_id=run.reservation_id,
        reservation_hash=run.reservation_hash,
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
            publication_planning_context_override=(
                rebind_publication_planning_context(
                    source.publication_planning_context,
                    restored.revision,
                )
                if selected.kind == "replan" and source.publication_planning_context
                else None
            ),
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(
            409,
            detail={
                "code": "ROLLBACK_SCENARIO_CHANGED",
                "message": "业务数据已变化，请刷新后重新确认恢复",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise publication_conflict_to_http(error) from error


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
    before_policy = before.solver_policy.fingerprint if before.solver_policy else None
    after_policy = after.solver_policy.fingerprint if after.solver_policy else None
    same_objective_policy = bool(before_policy and before_policy == after_policy)
    comparable_delta = {
        "sla_late_count": a.sla_late_count - b.sla_late_count,
        "travel_minutes": a.total_travel_minutes - b.total_travel_minutes,
        "overtime_minutes": a.total_overtime_minutes - b.total_overtime_minutes,
        "unassigned_count": a.unassigned_count - b.unassigned_count,
        "completion_rate": round(a.completion_rate - b.completion_rate, 4),
        "stability_rate": a.stability_rate,
    }
    return Comparison(
        scenario_id=scenario_id,
        before=before,
        after=after,
        before_schedule_id=before.id,
        after_schedule_id=after.id,
        before_source_schedule_id=before.source_schedule_id,
        after_source_schedule_id=after.source_schedule_id,
        delta={
            "objective": round(after.objective - before.objective, 2)
            if same_snapshot and same_objective_policy
            else None,
            **(
                {key: value for key, value in comparable_delta.items()}
                if same_snapshot
                else {key: None for key in comparable_delta}
            ),
        },
        changed_orders=changed,
        comparable=same_snapshot,
        raw_objective_comparable=same_snapshot and same_objective_policy,
        raw_objective_comparison_reason=(
            "同一数据快照和求解政策，可比较原始目标值"
            if same_snapshot and same_objective_policy
            else "原始目标值只在同一数据快照和完全相同的求解政策下可比"
        ),
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
        after_plan = require_plan_for_use(scenario_id, after, PlanUseCase.compare)
        before_plan = require_plan_for_use(scenario_id, before, PlanUseCase.compare) if before else None
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
            raise HTTPException(
                409,
                detail={"code": "COMPARISON_BASELINE_MISSING", "message": "指定方案没有可比较的基线"},
            )
        return build_comparison(
            scenario_id,
            before_result,
            after_plan.selected,
            after_plan.scenario_snapshot,
            after_plan.scenario_snapshot,
        )
    plans = [
        item
        for item in store.list_plan_versions(scenario_id, include_snapshots=True)
        if item.effective_integrity is AnalysisIntegrityStatus.verified
    ]
    if not plans:
        raise HTTPException(409, detail={"code": "PLAN_REQUIRED", "message": "请先生成至少一个方案"})
    after_plan = store.active_plan_version(scenario_id) or next(
        (item for item in reversed(plans) if item.action != "baseline"),
        plans[-1],
    )
    after_plan = require_plan_for_use(scenario_id, after_plan.id, PlanUseCase.compare)
    internal_baseline = next((item.schedule for item in after_plan.artifacts if item.role == "baseline"), None)
    before_result = internal_baseline or next(
        (item.selected for item in reversed(plans) if item.action == "baseline" and item.number < after_plan.number),
        None,
    )
    if not before_result:
        raise HTTPException(
            409,
            detail={"code": "COMPARISON_BASELINE_MISSING", "message": "当前方案没有可比较的基线"},
        )
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
        raise HTTPException(409, detail={"code": "STRATEGY_PROFILE_CONFLICT", "message": str(error)}) from error


@router.delete("/api/strategy-profiles/{profile_id}", status_code=204)
def delete_strategy_profile(profile_id: str) -> Response:
    try:
        deleted = require_store().delete_profile(profile_id)
    except ValueError as error:
        raise HTTPException(409, detail={"code": "STRATEGY_PROFILE_CONFLICT", "message": str(error)}) from error
    if not deleted:
        raise HTTPException(404, "策略不存在")
    return Response(status_code=204)


COMMON_EVALUATION_POLICY = {
    "travel": 0.20,
    "sla_late": 0.25,
    "overtime": 0.15,
    "normalized_workload_range": 0.10,
    "unassigned_penalty": 0.25,
    "replan_changes": 0.05,
}


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
        result.kpis.total_travel_minutes / total_shift * COMMON_EVALUATION_POLICY["travel"]
        + result.kpis.total_late_minutes / total_service * COMMON_EVALUATION_POLICY["sla_late"]
        + result.kpis.total_overtime_minutes / total_shift * COMMON_EVALUATION_POLICY["overtime"]
        + result.kpis.normalized_workload_range * COMMON_EVALUATION_POLICY["normalized_workload_range"]
        + unassigned / total_penalty * COMMON_EVALUATION_POLICY["unassigned_penalty"]
        + changes / active_count * COMMON_EVALUATION_POLICY["replan_changes"]
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
            reservation, run = start_schedule_run(
                scenario,
                "experiment",
                source_mode="ACTIVE_OR_LATEST",
                requested_time_limit_seconds=profile.time_limit_seconds,
                solver_config_hash=content_hash(effective.solver_config),
                expected_active_plan_version_id=experiment.expected_active_plan_version_id,
                check_active_plan=True,
                command_fingerprint=content_hash(
                    {
                        "experiment_id": experiment.id,
                        "profile_id": profile.id,
                        "experiment_fingerprint": experiment.fingerprint,
                    }
                ),
            )
            scenario = reservation.scenario_snapshot
            effective = scenario_for_profile(scenario, profile)
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
                    source_plan_version_id=reservation.source_plan_version_id,
                    expected_active_plan_version_id=run.expected_active_plan_version_id,
                    reservation_id=run.reservation_id,
                    reservation_hash=run.reservation_hash,
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
    active_plan = require_store().active_plan_version(target_id)
    experiment_fingerprint = content_hash(
        {
            "scenario_snapshot_hash": scenario_snapshot_hash,
            "profiles": [profile.model_dump(mode="json") for profile in profiles],
            "time_limit_seconds": request.time_limit_seconds,
            "solver_version": ortools.__version__,
            "travel_model_version": require_store().travel_provider.version,
            "travel_model_fingerprint": require_store().travel_provider.fingerprint,
            "score_policy_version": "FIELD_SERVICE_SCORE_V2",
            "score_policy_snapshot": COMMON_EVALUATION_POLICY,
            "seed": scenario.seed,
            "expected_active_plan_version_id": active_plan.id if active_plan else None,
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
        expected_active_plan_version_id=active_plan.id if active_plan else None,
        score_policy_version="FIELD_SERVICE_SCORE_V2",
        score_policy_snapshot=COMMON_EVALUATION_POLICY,
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
        raise HTTPException(
            409,
            detail={"code": "EXPERIMENT_NOT_COMPLETED", "message": "策略实验尚未完成", "resource_id": experiment.id},
        )
    if experiment.winner_candidate_id and experiment.winner_candidate_id != request.candidate_id:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_ALREADY_PUBLISHED",
                "message": "该实验已发布其他候选，一次实验只能选定一个方案",
                "resource_id": experiment.winner_candidate_id,
            },
        )
    if scenario.revision != request.expected_revision or experiment.data_revision != scenario.revision:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_SCENARIO_CHANGED",
                "message": "实验完成后业务数据已变化，请重新运行",
                "experiment_revision": experiment.data_revision,
                "current_revision": scenario.revision,
            },
        )
    candidate = next((item for item in experiment.candidates if item.id == request.candidate_id), None)
    if not candidate:
        raise HTTPException(404, "候选方案不存在")
    if experiment.winner_candidate_id == candidate.id and experiment.winner_plan_version_id:
        return require_plan_for_use(
            scenario_id,
            experiment.winner_plan_version_id,
            PlanUseCase.replay,
        )
    if not candidate.publishable:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_CANDIDATE_NOT_PUBLISHABLE",
                "message": "该候选没有可发布的可行方案",
                "resource_id": candidate.id,
            },
        )
    verification = validate_result(scenario, candidate.schedule)
    if not verification.publishable:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_CANDIDATE_VALIDATION_FAILED",
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
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_CANDIDATE_PROVENANCE_MISSING",
                "message": "候选方案缺少可追溯的求解记录，请重新运行实验",
                "resource_id": candidate.id,
            },
        )
    current_active = require_store().active_plan_version(scenario_id)
    current_active_id = current_active.id if current_active else None
    expected_active_id = schedule_candidate.expected_active_plan_version_id
    if expected_active_id != experiment.expected_active_plan_version_id:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_PROVENANCE_MISMATCH",
                "message": "实验候选与实验冻结的活动方案不一致，请重新运行实验",
                "experiment_active_plan_version_id": experiment.expected_active_plan_version_id,
                "candidate_active_plan_version_id": expected_active_id,
            },
        )
    if (
        "expected_active_plan_version_id" in request.model_fields_set
        and request.expected_active_plan_version_id != current_active_id
    ):
        raise HTTPException(
            409,
            detail={
                "code": "ACTIVE_PLAN_PRECONDITION_FAILED",
                "message": "活动方案已变化，请刷新后重新确认发布",
                "expected_active_plan_version_id": request.expected_active_plan_version_id,
                "current_active_plan_version_id": current_active_id,
            },
        )
    if current_active_id != expected_active_id:
        raise HTTPException(
            409,
            detail={
                "code": "EXPERIMENT_ACTIVE_PLAN_CHANGED",
                "message": "实验运行后活动方案已变化，请重新运行实验",
                "experiment_active_plan_version_id": expected_active_id,
                "current_active_plan_version_id": current_active_id,
            },
        )
    source = None
    if schedule_candidate.source_plan_version_id:
        source = require_plan_for_use(
            scenario_id,
            schedule_candidate.source_plan_version_id,
            PlanUseCase.replay,
        )
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
                "code": "EXPERIMENT_SCENARIO_CHANGED",
                "message": "业务数据已变化，请重新运行策略实验",
                "expected_revision": error.expected,
                "current_revision": error.current,
            },
        ) from error
    except PublicationConflict as error:
        raise publication_conflict_to_http(error) from error


@router.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}/report", response_class=HTMLResponse)
def version_report(scenario_id: str, version_id: str) -> HTMLResponse:
    plan = require_plan_for_use(scenario_id, version_id, PlanUseCase.report)
    if not plan.scenario_snapshot:
        raise HTTPException(404, "方案报告不存在")
    require_frozen_plan_integrity(plan)
    safe_scenario_id = safe_filename_component(scenario_id)
    return HTMLResponse(
        build_report(plan.scenario_snapshot, plan.selected),
        headers={"Content-Disposition": f'inline; filename="fieldflow-{safe_scenario_id}-V{plan.number:03d}.html"'},
    )


@router.get("/api/scenarios/{scenario_id}/report", response_class=HTMLResponse)
def report(scenario_id: str, schedule_id: str | None = None) -> HTMLResponse:
    scenario = require_scenario(scenario_id)
    if schedule_id:
        plan = next(
            (
                item
                for item in require_store().list_plan_versions(scenario_id, include_snapshots=True)
                if item.selected.id == schedule_id
            ),
            None,
        )
        if plan is None:
            raise HTTPException(
                409,
                detail={
                    "code": "UNATTESTED_SCHEDULE",
                    "message": "该排程没有可验证的正式方案，不能生成业务报告",
                },
            )
        plan = require_plan_for_use(scenario_id, plan.id, PlanUseCase.report)
        result = plan.selected
    else:
        plan = require_store().active_plan_version(scenario_id)
        if plan:
            plan = require_plan_for_use(scenario_id, plan.id, PlanUseCase.report)
        result = plan.selected if plan else None
    if not result or result.scenario_id != scenario_id:
        raise HTTPException(404, "当前没有可导出的方案")
    if plan:
        require_frozen_plan_integrity(plan)
    snapshot = plan.scenario_snapshot if plan and plan.scenario_snapshot else scenario
    safe_scenario_id = safe_filename_component(scenario_id)
    return HTMLResponse(
        build_report(snapshot, result),
        headers={"Content-Disposition": f'inline; filename="fieldflow-{safe_scenario_id}-V{result.version:03d}.html"'},
    )


FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}
LEGACY_CAS_WRITE = re.compile(r"^/api/scenarios/[^/]+/(?:work-orders(?:/[^/]+)?|technicians(?:/[^/]+)?|locks|reset)$")


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

    @application.exception_handler(DecisionAnalysisIntegrityError)
    async def decision_integrity_error_handler(_request: Request, error: DecisionAnalysisIntegrityError):
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": error.code,
                    "legacy_code": "MALFORMED_ATTESTED_RECORD",
                    "message": str(error),
                    "record_id": error.record_id,
                    "record_type": error.record_type,
                    **error.details,
                }
            },
        )

    @application.exception_handler(PublicationConflict)
    async def publication_conflict_handler(_request: Request, error: PublicationConflict):
        return JSONResponse(status_code=409, content={"detail": publication_conflict_detail(error)})

    @application.exception_handler(ScenarioRevisionConflict)
    async def scenario_revision_conflict_handler(_request: Request, error: ScenarioRevisionConflict):
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "SCENARIO_REVISION_CONFLICT",
                    "message": "业务数据已变化，请刷新后重试",
                    "retryable": True,
                    "refresh_required": True,
                    "expected_revision": error.expected,
                    "current_revision": error.current,
                }
            },
        )

    @application.exception_handler(ActivePlanConflict)
    async def active_plan_conflict_handler(_request: Request, error: ActivePlanConflict):
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "ACTIVE_PLAN_CHANGED_DURING_COMMAND",
                    "message": "活动方案已变化，请刷新后重试",
                    "retryable": True,
                    "refresh_required": True,
                    "expected_active_plan_id": error.expected,
                    "current_active_plan_id": error.current,
                }
            },
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
        response = await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and LEGACY_CAS_WRITE.fullmatch(request.url.path):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = "Wed, 31 Mar 2027 00:00:00 GMT"
            response.headers["Link"] = f'</api/v2{request.url.path.removeprefix("/api")}>; rel="successor-version"'
        return response

    application.include_router(router)
    if FRONTEND_DIST.exists():
        application.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return application


app = create_app()
