from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import ortools
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ._version import __version__
from .fixtures import get_fixture
from .hashing import content_hash
from .models import (
    Comparison,
    ExperimentPublishRequest,
    LockedAssignment,
    LockRequest,
    OptimizeRequest,
    PlanVersion,
    PlanVersionPatch,
    ReplanRequest,
    RestoreRequest,
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
    WorkOrderStatus,
    WorkOrderUpdate,
)
from .report import build_report
from .scheduler import (
    baseline_schedule,
    optimized_schedule,
    recompute_business_result,
    replan_schedule,
    scenario_for_profile,
)
from .storage import PublicationConflict, ScenarioRevisionConflict, Store
from .verification import verify_schedule

DB_PATH = Path(os.getenv("FIELDFLOW_DB", Path(__file__).resolve().parents[1] / "fieldflow.db"))
store = Store(DB_PATH)
experiment_executor: ThreadPoolExecutor | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global experiment_executor
    experiment_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fieldflow-strategy")
    try:
        yield
    finally:
        executor, experiment_executor = experiment_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

app = FastAPI(
    title="FieldFlow API",
    description="本地现场服务排程接口",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def require_scenario(scenario_id: str) -> ScheduleScenario:
    scenario = store.get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(404, f"场景 {scenario_id} 不存在")
    return scenario


def require_profile(profile_id: str) -> StrategyProfile:
    profile = store.get_profile(profile_id)
    if not profile:
        raise HTTPException(404, f"策略 {profile_id} 不存在")
    return profile


def validate_result(scenario: ScheduleScenario, result: ScheduleResult, source: ScheduleResult | None = None):
    return verify_schedule(scenario, result, source)


def save_scenario_change(scenario: ScheduleScenario, reason: str, *, preserve_active_plan: bool = False) -> ScheduleScenario:
    try:
        scenario = ScheduleScenario.model_validate(scenario.model_dump())
    except ValueError as error:
        raise HTTPException(422, detail={"message": "场景数据不完整", "error": str(error)}) from error
    expected_revision = scenario.revision
    scenario.revision += 1
    try:
        store.save_scenario(scenario, reason, expected_revision=expected_revision, preserve_active_plan=preserve_active_plan)
    except ScenarioRevisionConflict as error:
        raise HTTPException(409, detail={"message": "业务数据已被其他操作更新，请刷新后重试", "expected_revision": error.expected, "current_revision": error.current}) from error
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


def artifact(role: str, result: ScheduleResult, strategy: str) -> ScheduleArtifact:
    return ScheduleArtifact(id=f"ART-{uuid.uuid4().hex[:10]}", role=role, strategy=strategy, schedule=result)


def start_schedule_run(
    scenario: ScheduleScenario,
    action: str,
    *,
    source: PlanVersion | None = None,
    requested_time_limit_seconds: float = 0,
    solver_name: str = "ortools-routing",
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
        solver_config_hash=content_hash(scenario.solver_config),
        requested_time_limit_ms=requested_ms,
        effective_time_limit_ms=requested_ms,
        status=ScheduleRunStatus.running,
        started_at=_now(),
    )
    return store.save_schedule_run(run)


def fail_schedule_run(run: ScheduleRun, reason: str) -> None:
    run.status = ScheduleRunStatus.failed
    run.termination_reason = reason
    run.finished_at = _now()
    store.save_schedule_run(run)


def run_status_for_result(result: ScheduleResult) -> ScheduleRunStatus:
    try:
        return ScheduleRunStatus(result.solver_status.value)
    except ValueError:
        return ScheduleRunStatus.failed


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
) -> ScheduleResult:
    source_schedule = source.selected if source and result.kind == "replan" else None
    verification = validate_result(scenario, result, source_schedule)
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}",
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        source_plan_version_id=source.id if source else None,
        solver_config_hash=result.solver_config_hash,
        schedule=result,
        verification_report=verification,
        publishable=verification.publishable,
        created_at=_now(),
    )
    store.save_schedule_candidate(candidate)
    run.status = run_status_for_result(result)
    run.termination_reason = result.termination_reason
    run.solution_found = result.solution_found
    run.finished_at = _now()
    run.candidate_id = candidate.id
    run.solver_name = result.solver_name
    run.solver_version = result.solver_version
    run.solver_config_hash = result.solver_config_hash
    run.requested_time_limit_ms = result.requested_time_limit_ms or run.requested_time_limit_ms
    run.effective_time_limit_ms = result.effective_time_limit_ms or run.effective_time_limit_ms
    store.save_schedule_run(run)
    if not candidate.publishable:
        active = store.active_plan_version(scenario.id)
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
        plan = store.publish_plan(
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
        raise HTTPException(409, detail={"message": "求解期间业务数据已变化，结果未发布，请重新运行", "expected_revision": error.expected, "current_revision": error.current}) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error
    return plan.selected


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fieldflow", "version": __version__}


@app.get("/api/scenarios", response_model=list[ScheduleScenario])
def list_scenarios() -> list[ScheduleScenario]:
    return store.list_scenarios()


@app.get("/api/scenarios/{scenario_id}", response_model=ScheduleScenario)
def get_scenario(scenario_id: str) -> ScheduleScenario:
    return require_scenario(scenario_id)


@app.post("/api/scenarios", response_model=ScheduleScenario)
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
        store.save_scenario(scenario, "创建业务场景")
    except ScenarioRevisionConflict as error:
        raise HTTPException(409, detail={"message": "场景标识发生冲突，请重新创建", "current_revision": error.current}) from error
    return scenario


@app.post("/api/scenarios/{scenario_id}/reset", response_model=ScheduleScenario)
def reset_scenario(scenario_id: str) -> ScheduleScenario:
    current = require_scenario(scenario_id)
    fixture_id = current.source_scenario_id or scenario_id
    try:
        fresh = get_fixture(fixture_id)
    except KeyError as error:
        raise HTTPException(409, "该自定义场景没有可恢复的初始模板") from error
    fresh.id = current.id
    fresh.name = current.name
    fresh.source_scenario_id = current.source_scenario_id
    fresh.revision = current.revision + 1
    try:
        store.save_scenario(fresh, "恢复初始业务数据", expected_revision=current.revision)
    except ScenarioRevisionConflict as error:
        raise HTTPException(409, detail={"message": "业务数据已被其他操作更新，请刷新后重试", "expected_revision": error.expected, "current_revision": error.current}) from error
    return fresh


@app.get("/api/technicians")
def get_technicians(scenario_id: str = Query("main")):
    return require_scenario(scenario_id).technicians


@app.get("/api/work-orders")
def get_work_orders(scenario_id: str = Query("main")):
    return require_scenario(scenario_id).work_orders


@app.post("/api/scenarios/{scenario_id}/work-orders", response_model=ScheduleScenario)
def create_work_order(scenario_id: str, work_order: WorkOrder) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    if any(item.id == work_order.id for item in scenario.work_orders):
        raise HTTPException(409, f"工单 {work_order.id} 已存在")
    scenario.work_orders.append(normalize_order(work_order))
    return save_scenario_change(scenario, f"新增工单 {work_order.id}")


@app.put("/api/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
def update_work_order(scenario_id: str, work_order_id: str, request: WorkOrderUpdate) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    index = next((i for i, item in enumerate(scenario.work_orders) if item.id == work_order_id), None)
    if index is None:
        raise HTTPException(404, f"工单 {work_order_id} 不存在")
    original = scenario.work_orders[index]
    updates = request.model_dump(exclude_none=True)
    requested_status = updates.get("status")
    allowed_transitions = {
        WorkOrderStatus.pending: {WorkOrderStatus.pending, WorkOrderStatus.started, WorkOrderStatus.completed},
        WorkOrderStatus.started: {WorkOrderStatus.started, WorkOrderStatus.completed},
        WorkOrderStatus.completed: {WorkOrderStatus.completed},
    }
    if requested_status is not None and requested_status not in allowed_transitions[original.status]:
        raise HTTPException(409, f"工单状态不能从 {original.status.value} 回退到 {requested_status.value}")
    if original.status.value in {"started", "completed"}:
        immutable_fields = {"customer_name", "title", "required_skills", "location", "service_duration", "window_start", "window_end", "sla_deadline", "priority", "drop_penalty", "vip", "is_emergency", "reported_at"}
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
    if requested_status is WorkOrderStatus.completed:
        scenario.locked_assignments = [item for item in scenario.locked_assignments if item.work_order_id != work_order_id]
    return save_scenario_change(scenario, f"更新工单 {work_order_id}")


@app.delete("/api/scenarios/{scenario_id}/work-orders/{work_order_id}", response_model=ScheduleScenario)
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


@app.post("/api/scenarios/{scenario_id}/technicians", response_model=ScheduleScenario)
def create_technician(scenario_id: str, technician: Technician) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    if any(item.id == technician.id for item in scenario.technicians):
        raise HTTPException(409, f"技师 {technician.id} 已存在")
    scenario.technicians.append(technician)
    return save_scenario_change(scenario, f"新增技师 {technician.id}")


@app.put("/api/scenarios/{scenario_id}/technicians/{technician_id}", response_model=ScheduleScenario)
def update_technician(scenario_id: str, technician_id: str, request: TechnicianUpdate) -> ScheduleScenario:
    scenario = require_scenario(scenario_id)
    index = next((i for i, item in enumerate(scenario.technicians) if item.id == technician_id), None)
    if index is None:
        raise HTTPException(404, f"技师 {technician_id} 不存在")
    payload = scenario.technicians[index].model_dump()
    payload.update(request.model_dump(exclude_none=True))
    scenario.technicians[index] = Technician.model_validate(payload)
    return save_scenario_change(scenario, f"更新技师 {technician_id}")


@app.post("/api/scenarios/{scenario_id}/lock", response_model=ScheduleScenario)
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
    scenario.locked_assignments = [item for item in scenario.locked_assignments if item.work_order_id != request.work_order_id]
    if request.locked:
        scenario.locked_assignments.append(LockedAssignment(work_order_id=request.work_order_id, technician_id=request.technician_id))
    return save_scenario_change(scenario, ("锁定" if request.locked else "解除锁定") + f"工单 {request.work_order_id}")


@app.get("/api/scenarios/{scenario_id}/schedules", response_model=list[ScheduleResult])
def list_schedule_versions(scenario_id: str) -> list[ScheduleResult]:
    require_scenario(scenario_id)
    return store.list_schedules(scenario_id)


@app.get("/api/scenarios/{scenario_id}/schedule-runs", response_model=list[ScheduleRun])
def list_schedule_runs(scenario_id: str) -> list[ScheduleRun]:
    require_scenario(scenario_id)
    return store.list_schedule_runs(scenario_id)


@app.get("/api/scenarios/{scenario_id}/schedule-runs/{run_id}", response_model=ScheduleRun)
def get_schedule_run(scenario_id: str, run_id: str) -> ScheduleRun:
    run = store.get_schedule_run(run_id)
    if not run or run.scenario_id != scenario_id:
        raise HTTPException(404, "求解记录不存在")
    return run


@app.get("/api/scenarios/{scenario_id}/schedule-candidates/{candidate_id}", response_model=ScheduleCandidate)
def get_schedule_candidate(scenario_id: str, candidate_id: str) -> ScheduleCandidate:
    candidate = store.get_schedule_candidate(candidate_id)
    if not candidate or candidate.scenario_id != scenario_id:
        raise HTTPException(404, "候选方案不存在")
    return candidate


@app.post("/api/scenarios/{scenario_id}/baseline", response_model=ScheduleResult)
def run_baseline(scenario_id: str) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    if any(order.status == WorkOrderStatus.started for order in scenario.work_orders):
        raise HTTPException(409, "场景中已有执行中的工单，请使用局部重排以保留已开始安排")
    run = start_schedule_run(scenario, "baseline", solver_name="fieldflow-greedy")
    try:
        result = recompute_business_result(scenario, baseline_schedule(scenario, 0))
    except Exception as error:
        fail_schedule_run(run, f"{type(error).__name__}: {error}")
        raise
    return publish_selected(scenario, result, "baseline", artifacts=[artifact("selected", result, "baseline")], run=run)


@app.post("/api/scenarios/{scenario_id}/optimize", response_model=ScheduleResult)
def run_optimize(scenario_id: str, request: OptimizeRequest | None = None) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    if any(order.status == WorkOrderStatus.started for order in scenario.work_orders):
        raise HTTPException(409, "场景中已有执行中的工单，请使用局部重排以保留已开始安排")
    request = request or OptimizeRequest()
    profile = profile_for_request(request.strategy, request.profile_id, request.time_limit_seconds)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    source = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
    run = start_schedule_run(scenario, "optimize", source=source, requested_time_limit_seconds=profile.time_limit_seconds)
    try:
        baseline = baseline_schedule(effective, 0, strategy_key)
        result = optimized_schedule(effective, 0, previous=baseline, time_limit_seconds=profile.time_limit_seconds, strategy=strategy_key)
        result = recompute_business_result(scenario, result)
    except Exception as error:
        fail_schedule_run(run, f"{type(error).__name__}: {error}")
        raise
    source_matches = bool(source and source.data_revision == scenario.revision and (source.scenario_snapshot_hash or (content_hash(source.scenario_snapshot) if source.scenario_snapshot else "")) == content_hash(scenario))
    relation = "optimized_from" if source_matches else "fresh_after_data_change" if source else "new"
    return publish_selected(scenario, result, "optimize", artifacts=[artifact("baseline", baseline, strategy_key), artifact("selected", result, strategy_key)], source=source, relation=relation, label=profile.name, run=run)


@app.post("/api/scenarios/{scenario_id}/replan", response_model=ScheduleResult)
def run_replan(scenario_id: str, request: ReplanRequest) -> ScheduleResult:
    scenario = require_scenario(scenario_id)
    persisted_revision = scenario.revision
    replace_scenario = False
    incoming = request.emergency_order
    idempotency_key = request.idempotency_key or (f"emergency-replan:{scenario_id}:{incoming.id}" if incoming else None)
    request_fingerprint = content_hash({"scenario_id": scenario_id, "request": request.model_dump(mode="json")}) if idempotency_key else None
    if idempotency_key and request_fingerprint:
        try:
            existing_publication = store.published_for_key(idempotency_key, request_fingerprint)
        except PublicationConflict as error:
            raise HTTPException(409, str(error)) from error
        if existing_publication:
            return existing_publication.selected
    if incoming:
        normalized = normalize_order(incoming.model_copy(deep=True))
        existing = next((order for order in scenario.work_orders if order.id == normalized.id), None)
        if existing and existing.model_dump(mode="json") != normalized.model_dump(mode="json"):
            raise HTTPException(409, f"工单 {normalized.id} 已存在，但内容与本次请求不同")
        if not existing:
            scenario.work_orders.append(normalized)
            scenario.revision += 1
            try:
                scenario = ScheduleScenario.model_validate(scenario.model_dump())
            except ValueError as error:
                raise HTTPException(422, detail={"message": "突发工单导致场景数据不完整", "error": str(error)}) from error
            replace_scenario = True
    source = store.active_plan_version(scenario_id) or store.latest_plan_version(scenario_id)
    previous = source.selected if source else None
    internal: list[ScheduleArtifact] = []
    if not previous:
        if any(order.status == WorkOrderStatus.started for order in scenario.work_orders):
            raise HTTPException(409, "执行中的工单缺少可追溯的原方案，不能安全重排")
        balanced = require_profile("balanced")
        base_effective = scenario_for_profile(scenario, balanced)
        previous = optimized_schedule(base_effective, 0, time_limit_seconds=balanced.time_limit_seconds, strategy="balanced")
        internal.append(artifact("candidate", previous, "balanced"))
    profile = profile_for_request(request.strategy, request.profile_id, request.time_limit_seconds)
    effective = scenario_for_profile(scenario, profile)
    strategy_key = profile.id if profile.builtin else "custom"
    planning_time = request.planning_time
    assert planning_time is not None
    run = start_schedule_run(scenario, "replan", source=source, requested_time_limit_seconds=profile.time_limit_seconds)
    try:
        result = replan_schedule(effective, 0, previous, planning_time, profile.time_limit_seconds, strategy_key)
        result = recompute_business_result(scenario, result, previous)
    except Exception as error:
        fail_schedule_run(run, f"{type(error).__name__}: {error}")
        raise
    internal.append(artifact("selected", result, strategy_key))
    source_matches = bool(source and source.data_revision == scenario.revision and (source.scenario_snapshot_hash or (content_hash(source.scenario_snapshot) if source.scenario_snapshot else "")) == content_hash(scenario))
    relation = "replanned_from" if source_matches else "fresh_after_data_change" if source else "new"
    return publish_selected(
        scenario,
        result,
        "replan",
        artifacts=internal,
        source=source,
        relation=relation,
        label=profile.name,
        run=run,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        replace_scenario=replace_scenario,
        expected_revision=persisted_revision if replace_scenario else scenario.revision,
    )


@app.get("/api/scenarios/{scenario_id}/plan-versions", response_model=list[PlanVersion])
def list_plan_versions(scenario_id: str) -> list[PlanVersion]:
    require_scenario(scenario_id)
    return store.list_plan_versions(scenario_id)


@app.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}", response_model=PlanVersion)
def get_plan_version(scenario_id: str, version_id: str) -> PlanVersion:
    require_scenario(scenario_id)
    plan = store.get_plan_version(scenario_id, version_id)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return plan


@app.patch("/api/scenarios/{scenario_id}/plan-versions/{version_id}", response_model=PlanVersion)
def rename_plan_version(scenario_id: str, version_id: str, request: PlanVersionPatch) -> PlanVersion:
    require_scenario(scenario_id)
    plan = store.rename_plan_version(scenario_id, version_id, request.label)
    if not plan:
        raise HTTPException(404, "方案版本不存在")
    return plan


@app.post("/api/scenarios/{scenario_id}/plan-versions/{version_id}/restore", response_model=PlanVersion)
def restore_plan_version(scenario_id: str, version_id: str, request: RestoreRequest) -> PlanVersion:
    current = require_scenario(scenario_id)
    if current.revision != request.expected_revision:
        raise HTTPException(409, detail={"message": "业务数据已变化，请刷新后重新确认恢复", "expected_revision": request.expected_revision, "current_revision": current.revision})
    source = store.get_plan_version(scenario_id, version_id)
    if not source or not source.scenario_snapshot:
        raise HTTPException(404, "方案版本或业务快照不存在")
    restored = source.scenario_snapshot.model_copy(deep=True)
    restored.id = scenario_id
    restored.revision = current.revision + 1
    selected = source.selected.model_copy(deep=True)
    selected.id = f"SCH-{scenario_id}-restore-{uuid.uuid4().hex[:8]}"
    selected.created_at = _now()
    selected.source_schedule_id = source.selected.id
    selected.scenario_revision = restored.revision
    selected.solution_found = True
    selected = recompute_business_result(restored, selected)
    run = start_schedule_run(restored, "restore", source=source, solver_name="plan-restore")
    verification = validate_result(restored, selected)
    candidate = ScheduleCandidate(
        id=f"CAND-{uuid.uuid4().hex[:12]}", run_id=run.id, scenario_id=scenario_id,
        scenario_revision=restored.revision, scenario_snapshot_hash=content_hash(restored),
        source_plan_version_id=source.id, solver_config_hash=selected.solver_config_hash,
        schedule=selected, verification_report=verification, publishable=verification.publishable,
        created_at=_now(),
    )
    store.save_schedule_candidate(candidate)
    run.status = run_status_for_result(selected)
    run.solution_found = selected.solution_found
    run.termination_reason = "RESTORED_FROM_VERIFIED_PLAN"
    run.finished_at = _now()
    run.candidate_id = candidate.id
    run.solver_name = "plan-restore"
    run.solver_version = "1"
    store.save_schedule_run(run)
    if not candidate.publishable:
        raise HTTPException(422, detail={"message": "历史方案未通过当前发布验证", "run_id": run.id, "candidate_id": candidate.id, "errors": [item.model_dump() for item in verification.errors]})
    try:
        return store.publish_plan(restored, selected, "restore", artifacts=[artifact("selected", selected, selected.strategy)], source_version_id=source.id, relation="restored_from", label=f"恢复自 V{source.number:03d} · {source.label}", replace_scenario=True, expected_revision=request.expected_revision, candidate_id=candidate.id)
    except ScenarioRevisionConflict as error:
        raise HTTPException(409, detail={"message": "业务数据已变化，请刷新后重新确认恢复", "expected_revision": error.expected, "current_revision": error.current}) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error


def build_comparison(scenario_id: str, before: ScheduleResult, after: ScheduleResult) -> Comparison:
    before_by_id = {item.work_order_id: item for item in before.assignments}
    after_by_id = {item.work_order_id: item for item in after.assignments}
    changed = []
    for order_id in sorted(set(before_by_id) | set(after_by_id)):
        old = before_by_id.get(order_id)
        new = after_by_id.get(order_id)
        if not old or not new or old.technician_id != new.technician_id or old.sequence != new.sequence or old.start_time != new.start_time:
            changed.append({"work_order_id": order_id, "before_technician": old.technician_id if old else None, "after_technician": new.technician_id if new else None, "before_start": old.start_time if old else None, "after_start": new.start_time if new else None, "reason": "技师、顺序或到场时间发生变化" if old and new else "工单进入或离开可执行计划"})
    b, a = before.kpis, after.kpis
    return Comparison(scenario_id=scenario_id, before=before, after=after, delta={"objective": round(after.objective - before.objective, 2) if after.strategy == before.strategy else None, "sla_late_count": a.sla_late_count - b.sla_late_count, "travel_minutes": a.total_travel_minutes - b.total_travel_minutes, "overtime_minutes": a.total_overtime_minutes - b.total_overtime_minutes, "unassigned_count": a.unassigned_count - b.unassigned_count, "completion_rate": round(a.completion_rate - b.completion_rate, 4), "stability_rate": a.stability_rate}, changed_orders=changed)


@app.get("/api/scenarios/{scenario_id}/comparison", response_model=Comparison)
def comparison(scenario_id: str, before: str | None = None, after: str | None = None) -> Comparison:
    require_scenario(scenario_id)
    if before and after:
        before_plan = store.get_plan_version(scenario_id, before)
        after_plan = store.get_plan_version(scenario_id, after)
        if not before_plan or not after_plan:
            raise HTTPException(404, "用于比较的方案版本不存在")
        return build_comparison(scenario_id, before_plan.selected, after_plan.selected)
    plans = store.list_plan_versions(scenario_id, include_snapshots=True)
    if not plans:
        raise HTTPException(409, "请先生成至少一个方案")
    after_plan = next((item for item in reversed(plans) if item.action != "baseline"), plans[-1])
    internal_baseline = next((item.schedule for item in after_plan.artifacts if item.role == "baseline"), None)
    before_result = internal_baseline or next((item.selected for item in reversed(plans) if item.action == "baseline" and item.number < after_plan.number), None)
    if not before_result:
        raise HTTPException(409, "当前方案没有可比较的基线")
    return build_comparison(scenario_id, before_result, after_plan.selected)


@app.get("/api/strategy-profiles", response_model=list[StrategyProfile])
def list_strategy_profiles(include_stable: bool = True) -> list[StrategyProfile]:
    return store.list_profiles(include_stable)


@app.post("/api/strategy-profiles", response_model=StrategyProfile, status_code=201)
def create_strategy_profile(request: StrategyProfileCreate) -> StrategyProfile:
    return store.save_profile(request)


@app.put("/api/strategy-profiles/{profile_id}", response_model=StrategyProfile)
def update_strategy_profile(profile_id: str, request: StrategyProfileCreate) -> StrategyProfile:
    if not store.get_profile(profile_id):
        raise HTTPException(404, "策略不存在")
    try:
        return store.save_profile(request, profile_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.delete("/api/strategy-profiles/{profile_id}", status_code=204)
def delete_strategy_profile(profile_id: str) -> Response:
    try:
        deleted = store.delete_profile(profile_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not deleted:
        raise HTTPException(404, "策略不存在")
    return Response(status_code=204)


def common_evaluation_score(scenario: ScheduleScenario, result: ScheduleResult) -> float:
    order_map = {order.id: order for order in scenario.work_orders}
    total_shift = max(1, sum(item.shift_end - item.shift_start for item in scenario.technicians))
    total_service = max(1, sum(item.service_duration for item in scenario.work_orders if item.status != WorkOrderStatus.completed))
    total_penalty = max(1, sum(item.drop_penalty for item in scenario.work_orders if item.status != WorkOrderStatus.completed))
    unassigned = sum(order_map[item.work_order_id].drop_penalty for item in result.unassigned if item.work_order_id in order_map)
    changes = sum(1 for item in result.assignments if item.changed)
    active_count = max(1, len([item for item in scenario.work_orders if item.status != WorkOrderStatus.completed]))
    normalized = (
        result.kpis.total_travel_minutes / total_shift * .20
        + result.kpis.total_late_minutes / total_service * .25
        + result.kpis.total_overtime_minutes / total_shift * .15
        + result.kpis.normalized_workload_range * .10
        + unassigned / total_penalty * .25
        + changes / active_count * .05
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
            no_worse = all(left <= right + tolerance for left, right in zip(other_values, candidate_values, strict=False))
            strictly_better = any(left < right - tolerance for left, right in zip(other_values, candidate_values, strict=False))
            if no_worse and strictly_better:
                dominated_by.append(other.id)
        candidate.dominated_by = dominated_by
        candidate.pareto_optimal = not dominated_by


def _run_experiment(experiment_id: str, override_limit: float | None) -> None:
    experiment = store.get_experiment(experiment_id)
    if not experiment or not experiment.scenario_snapshot:
        return
    try:
        scenario = experiment.scenario_snapshot
        experiment.status = "RUNNING"
        store.save_experiment(experiment)
        profiles = experiment.profile_snapshots
        candidates: list[StrategyCandidate] = []
        total = max(1, len(profiles))
        for index, frozen_profile in enumerate(profiles):
            profile = frozen_profile.model_copy(deep=True)
            if override_limit is not None:
                profile.time_limit_seconds = override_limit
            effective = scenario_for_profile(scenario, profile)
            strategy_key = profile.id if profile.builtin else "custom"
            run = start_schedule_run(scenario, "experiment", requested_time_limit_seconds=profile.time_limit_seconds)
            try:
                baseline = baseline_schedule(effective, 0, strategy_key)
                result = optimized_schedule(effective, 0, previous=baseline, time_limit_seconds=profile.time_limit_seconds, strategy=strategy_key)
                result = recompute_business_result(scenario, result)
                verification = verify_schedule(scenario, result)
                schedule_candidate = ScheduleCandidate(
                    id=f"CAND-{uuid.uuid4().hex[:12]}", run_id=run.id, scenario_id=scenario.id,
                    scenario_revision=scenario.revision, scenario_snapshot_hash=content_hash(scenario),
                    solver_config_hash=result.solver_config_hash, schedule=result,
                    verification_report=verification, publishable=verification.publishable, created_at=_now(),
                )
                store.save_schedule_candidate(schedule_candidate)
                run.status = run_status_for_result(result)
                run.termination_reason = result.termination_reason
                run.solution_found = result.solution_found
                run.finished_at = _now()
                run.candidate_id = schedule_candidate.id
                run.solver_name = result.solver_name
                run.solver_version = result.solver_version
                run.solver_config_hash = result.solver_config_hash
                run.requested_time_limit_ms = result.requested_time_limit_ms or run.requested_time_limit_ms
                run.effective_time_limit_ms = result.effective_time_limit_ms or run.effective_time_limit_ms
                store.save_schedule_run(run)
                candidate = StrategyCandidate(
                    id=f"SC-{uuid.uuid4().hex[:10]}", profile_id=profile.id, profile_name=profile.name,
                    schedule=result, evaluation_score=common_evaluation_score(scenario, result),
                    publishable=schedule_candidate.publishable, schedule_candidate_id=schedule_candidate.id,
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
            store.save_experiment(experiment)
        publishable_candidates = [item for item in candidates if item.publishable]
        if publishable_candidates:
            mark_pareto_candidates(publishable_candidates)
            for candidate in candidates:
                if not candidate.publishable:
                    candidate.pareto_optimal = False
            metrics = {
                "完成率最佳": max(item.schedule.kpis.completion_rate for item in publishable_candidates),
                "最准时": max(item.schedule.kpis.committed_on_time_rate for item in publishable_candidates),
                "最短行程": min(item.schedule.kpis.total_travel_minutes for item in publishable_candidates),
                "最少加班": min(item.schedule.kpis.total_overtime_minutes for item in publishable_candidates),
                "最公平": min(item.schedule.kpis.normalized_workload_range for item in publishable_candidates),
                "对比得分最低": min(item.evaluation_score for item in publishable_candidates),
            }
            for candidate in publishable_candidates:
                values = {"完成率最佳": candidate.schedule.kpis.completion_rate, "最准时": candidate.schedule.kpis.committed_on_time_rate, "最短行程": candidate.schedule.kpis.total_travel_minutes, "最少加班": candidate.schedule.kpis.total_overtime_minutes, "最公平": candidate.schedule.kpis.normalized_workload_range, "对比得分最低": candidate.evaluation_score}
                candidate.advantages = [label for label, value in values.items() if abs(value - metrics[label]) <= 1e-9]
        experiment.status = "COMPLETED"
        experiment.progress = 100
        experiment.candidates = candidates
    except Exception as error:  # keep the local UI actionable instead of losing the job
        experiment.status = "FAILED"
        experiment.error = str(error)
        experiment.progress = 100
    store.save_experiment(experiment)


@app.post("/api/scenarios/{scenario_id}/strategy-experiments", response_model=StrategyExperiment, status_code=status.HTTP_202_ACCEPTED)
def create_strategy_experiment(scenario_id: str, request: StrategyExperimentRequest) -> StrategyExperiment:
    target_id = scenario_id if request.dataset == "current" else request.dataset
    scenario = require_scenario(target_id)
    profile_ids = request.profile_ids or [profile.id for profile in store.list_profiles(include_stable=False)]
    profiles = [require_profile(profile_id).model_copy(deep=True) for profile_id in profile_ids]
    scenario_snapshot_hash = content_hash(scenario)
    experiment_fingerprint = content_hash({
        "scenario_snapshot_hash": scenario_snapshot_hash,
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
        "time_limit_seconds": request.time_limit_seconds,
        "solver_version": ortools.__version__,
        "travel_model_version": "EUCLIDEAN_GRID_V2",
        "score_policy_version": "FIELD_SERVICE_SCORE_V2",
        "seed": scenario.seed,
    })
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
        travel_model_version="EUCLIDEAN_GRID_V2",
        solver_version=ortools.__version__,
    )
    if experiment_executor is None:
        raise HTTPException(503, "策略实验执行器未启动")
    queued, created = store.queue_experiment(experiment)
    if created:
        experiment_executor.submit(_run_experiment, queued.id, request.time_limit_seconds)
    return queued.model_copy(update={"scenario_snapshot": None})


@app.get("/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}", response_model=StrategyExperiment)
def get_strategy_experiment(scenario_id: str, experiment_id: str) -> StrategyExperiment:
    experiment = store.get_experiment(experiment_id)
    if not experiment or experiment.scenario_id != scenario_id:
        raise HTTPException(404, "策略实验不存在")
    return experiment.model_copy(update={"scenario_snapshot": None})


@app.post("/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}/publish", response_model=PlanVersion)
def publish_strategy_candidate(scenario_id: str, experiment_id: str, request: ExperimentPublishRequest) -> PlanVersion:
    scenario = require_scenario(scenario_id)
    experiment = store.get_experiment(experiment_id)
    if not experiment or experiment.scenario_id != scenario_id:
        raise HTTPException(404, "策略实验不存在")
    if experiment.status != "COMPLETED":
        raise HTTPException(409, "策略实验尚未完成")
    if scenario.revision != request.expected_revision or experiment.data_revision != scenario.revision:
        raise HTTPException(409, detail={"message": "实验完成后业务数据已变化，请重新运行", "experiment_revision": experiment.data_revision, "current_revision": scenario.revision})
    candidate = next((item for item in experiment.candidates if item.id == request.candidate_id), None)
    if not candidate:
        raise HTTPException(404, "候选方案不存在")
    if not candidate.publishable:
        raise HTTPException(409, "该候选没有可发布的可行方案")
    verification = validate_result(scenario, candidate.schedule)
    if not verification.publishable:
        raise HTTPException(409, detail={"message": "候选方案未通过当前发布验证", "errors": [item.model_dump() for item in verification.errors]})
    schedule_candidate = store.get_schedule_candidate(candidate.schedule_candidate_id) if candidate.schedule_candidate_id else None
    if not schedule_candidate:
        raise HTTPException(409, "候选方案缺少可追溯的求解记录，请重新运行实验")
    schedule_candidate.verification_report = verification
    schedule_candidate.publishable = verification.publishable
    store.save_schedule_candidate(schedule_candidate)
    source = store.active_plan_version(scenario_id)
    try:
        return store.publish_plan(
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
        )
    except ScenarioRevisionConflict as error:
        raise HTTPException(409, detail={"message": "业务数据已变化，请重新运行策略实验", "expected_revision": error.expected, "current_revision": error.current}) from error
    except PublicationConflict as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/scenarios/{scenario_id}/plan-versions/{version_id}/report", response_class=HTMLResponse)
def version_report(scenario_id: str, version_id: str) -> HTMLResponse:
    plan = store.get_plan_version(scenario_id, version_id)
    if not plan or not plan.scenario_snapshot:
        raise HTTPException(404, "方案报告不存在")
    return HTMLResponse(build_report(plan.scenario_snapshot, plan.selected), headers={"Content-Disposition": f'inline; filename="fieldflow-{scenario_id}-V{plan.number:03d}.html"'})


@app.get("/api/scenarios/{scenario_id}/report", response_class=HTMLResponse)
def report(scenario_id: str, schedule_id: str | None = None) -> HTMLResponse:
    scenario = require_scenario(scenario_id)
    if schedule_id:
        result = store.get_schedule(schedule_id)
        plan = next((item for item in store.list_plan_versions(scenario_id, include_snapshots=True) if item.selected.id == schedule_id), None)
    else:
        plan = store.active_plan_version(scenario_id)
        result = plan.selected if plan else None
    if not result or result.scenario_id != scenario_id:
        raise HTTPException(404, "当前没有可导出的方案")
    snapshot = plan.scenario_snapshot if plan and plan.scenario_snapshot else scenario
    return HTMLResponse(build_report(snapshot, result), headers={"Content-Disposition": f'inline; filename="fieldflow-{scenario_id}-V{result.version:03d}.html"'})


FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
