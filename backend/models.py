from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
StrategyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=30)]
ShortDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
IdempotencyKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=120)]
HexColor = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^#[0-9A-Fa-f]{6}$")]


class Skill(str, Enum):
    electrical = "electrical"
    hvac = "hvac"
    network = "network"


class Priority(str, Enum):
    urgent = "urgent"
    high = "high"
    normal = "normal"
    low = "low"


class WorkOrderStatus(str, Enum):
    pending = "pending"
    started = "started"
    completed = "completed"


class SolverStatus(str, Enum):
    optimal = "OPTIMAL"
    feasible = "FEASIBLE"
    time_limit_feasible = "TIME_LIMIT_FEASIBLE"
    time_limit_no_solution = "TIME_LIMIT_NO_SOLUTION"
    infeasible = "INFEASIBLE"
    no_solution = "NO_SOLUTION"
    invalid_model = "INVALID_MODEL"
    failed = "FAILED"
    cancelled = "CANCELLED"
    # Kept for databases created before the status contract was expanded.
    time_limit = "TIME_LIMIT"


class ScheduleRunStatus(str, Enum):
    queued = "QUEUED"
    running = "RUNNING"
    optimal = "OPTIMAL"
    feasible = "FEASIBLE"
    time_limit_feasible = "TIME_LIMIT_FEASIBLE"
    time_limit_no_solution = "TIME_LIMIT_NO_SOLUTION"
    infeasible = "INFEASIBLE"
    no_solution = "NO_SOLUTION"
    invalid_model = "INVALID_MODEL"
    failed = "FAILED"
    cancelled = "CANCELLED"


class PlanCoverageStatus(str, Enum):
    current_and_complete = "CURRENT_AND_COMPLETE"
    partial_new_demand = "PARTIAL_NEW_DEMAND"
    stale_data_changed = "STALE_DATA_CHANGED"


class FreezeReason(str, Enum):
    started = "STARTED"
    completed = "COMPLETED"


class UnassignedReason(str, Enum):
    no_eligible_technician = "NO_ELIGIBLE_TECHNICIAN"
    time_window_infeasible = "TIME_WINDOW_INFEASIBLE"
    shift_capacity_exceeded = "SHIFT_CAPACITY_EXCEEDED"
    dropped_by_objective = "DROPPED_BY_OBJECTIVE"
    locked_plan_conflict = "LOCKED_PLAN_CONFLICT"


class Point(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class Technician(BaseModel):
    id: Identifier
    name: DisplayName
    skills: list[Skill]
    shift_start: int = Field(ge=0, le=1440)
    shift_end: int = Field(ge=0, le=1800)
    start_location: Point
    overtime_limit: int = Field(default=60, ge=0, le=240)
    cost_per_minute: float = Field(default=1.0, gt=0)
    color: HexColor = "#315c4b"

    @model_validator(mode="after")
    def validate_shift_and_skills(self) -> Technician:
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be later than shift_start")
        if not self.skills:
            raise ValueError("technician requires at least one skill")
        self.skills = list(dict.fromkeys(self.skills))
        return self


class WorkOrder(BaseModel):
    id: Identifier
    customer_name: DisplayName
    title: DisplayName
    required_skills: list[Skill]
    location: Point
    service_duration: int = Field(gt=0, le=480)
    window_start: int = Field(ge=0, le=1800)
    window_end: int = Field(ge=0, le=1800)
    sla_deadline: int = Field(ge=0, le=1800)
    priority: Priority = Priority.normal
    drop_penalty: int = Field(default=1200, gt=0)
    status: WorkOrderStatus = WorkOrderStatus.pending
    vip: bool = False
    is_emergency: bool = False
    reported_at: int | None = Field(default=None, ge=0, le=1800)
    note: ShortDescription = ""

    @model_validator(mode="after")
    def validate_times(self) -> WorkOrder:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        if self.sla_deadline < self.window_start:
            raise ValueError("sla_deadline must not precede window_start")
        if not self.required_skills:
            raise ValueError("work order requires at least one skill")
        if self.reported_at is not None and self.reported_at > self.window_end:
            raise ValueError("reported_at must not be later than window_end")
        if self.is_emergency and self.reported_at is None:
            raise ValueError("emergency work order requires reported_at")
        self.required_skills = list(dict.fromkeys(self.required_skills))
        return self


class LockedAssignment(BaseModel):
    work_order_id: Identifier
    technician_id: Identifier


class SolverConfig(BaseModel):
    time_limit_seconds: float = Field(default=2.0, ge=1, le=30)
    travel_weight: int = 4
    sla_late_weight: int = 12
    overtime_weight: int = 30
    imbalance_weight: int = 1
    replan_change_weight: int = 80


StrategyKey = Literal[
    "baseline", "balanced", "completion", "punctuality", "low_travel",
    "low_overtime", "fair_workload", "stable", "custom",
]


class ScheduleScenario(BaseModel):
    id: Identifier
    name: DisplayName
    description: ShortDescription
    planning_date: str = "2026-08-23"
    technicians: list[Technician]
    work_orders: list[WorkOrder]
    solver_config: SolverConfig = Field(default_factory=SolverConfig)
    locked_assignments: list[LockedAssignment] = Field(default_factory=list)
    source_scenario_id: str | None = None
    seed: int = 20260823
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_aggregate(self) -> ScheduleScenario:
        try:
            date.fromisoformat(self.planning_date)
        except ValueError as error:
            raise ValueError("planning_date must use YYYY-MM-DD") from error
        technician_ids = [item.id for item in self.technicians]
        work_order_ids = [item.id for item in self.work_orders]
        if len(technician_ids) != len(set(technician_ids)):
            raise ValueError("technician IDs must be unique within a scenario")
        if len(work_order_ids) != len(set(work_order_ids)):
            raise ValueError("work order IDs must be unique within a scenario")
        technicians = {item.id: item for item in self.technicians}
        work_orders = {item.id: item for item in self.work_orders}
        locked_ids: set[str] = set()
        for lock in self.locked_assignments:
            if lock.work_order_id in locked_ids:
                raise ValueError(f"work order {lock.work_order_id} is locked more than once")
            locked_ids.add(lock.work_order_id)
            order = work_orders.get(lock.work_order_id)
            technician = technicians.get(lock.technician_id)
            if order is None:
                raise ValueError(f"lock references unknown work order {lock.work_order_id}")
            if technician is None:
                raise ValueError(f"lock references unknown technician {lock.technician_id}")
            if order.status is WorkOrderStatus.completed:
                raise ValueError(f"completed work order {lock.work_order_id} cannot remain locked")
            if not set(order.required_skills).issubset(set(technician.skills)):
                raise ValueError(f"locked technician {lock.technician_id} lacks skills for {lock.work_order_id}")
        return self


class ScheduleAssignment(BaseModel):
    work_order_id: str
    technician_id: str
    sequence: int
    arrival_time: int
    start_time: int
    finish_time: int
    travel_minutes: int
    sla_late_minutes: int
    explanation: list[str]
    evidence: dict[str, Any] = Field(default_factory=dict)
    locked: bool = False
    changed: bool = False


class UnassignedWorkOrder(BaseModel):
    work_order_id: str
    reason: UnassignedReason
    detail: str
    suggestions: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class TechnicianKPI(BaseModel):
    technician_id: str
    service_minutes: int
    travel_minutes: int
    overtime_minutes: int
    utilization: float
    assignment_count: int
    waiting_minutes: int = 0
    occupied_minutes: int = 0
    service_utilization: float = 0
    occupied_utilization: float = 0
    travel_ratio: float = 0
    waiting_ratio: float = 0
    overtime_ratio: float = 0
    normalized_workload: float = 0


class ScheduleKPI(BaseModel):
    completion_rate: float
    sla_on_time_rate: float
    sla_late_count: int
    total_travel_minutes: int
    total_service_minutes: int
    total_overtime_minutes: int
    average_utilization: float
    unassigned_count: int
    high_priority_missed: int
    workload_stddev: float
    stability_rate: float | None = None
    technician: list[TechnicianKPI]
    assigned_on_time_rate: float = 0
    committed_on_time_rate: float = 0
    total_late_minutes: int = 0
    p90_late_minutes: int = 0
    total_waiting_minutes: int = 0
    average_occupied_utilization: float = 0
    workload_range: int = 0
    normalized_workload_range: float = 0
    same_technician_rate: float | None = None
    adjacency_preservation_rate: float | None = None
    start_time_shift_median: float | None = None
    start_time_shift_p90: int | None = None
    start_time_shift_over_15m_count: int | None = None
    customer_notification_count: int | None = None


class ScheduleResult(BaseModel):
    id: str
    scenario_id: str
    kind: Literal["baseline", "optimized", "replan"]
    version: int
    created_at: str
    solver_status: SolverStatus
    runtime_ms: int
    objective: float
    assignments: list[ScheduleAssignment]
    unassigned: list[UnassignedWorkOrder]
    kpis: ScheduleKPI
    source_schedule_id: str | None = None
    solver_note: str = ""
    scenario_revision: int = 0
    strategy: StrategyKey = "balanced"
    objective_breakdown: dict[str, float] = Field(default_factory=dict)
    requested_time_limit_ms: int | None = None
    effective_time_limit_ms: int | None = None
    solver_status_code: int | None = None
    termination_reason: str | None = None
    solution_found: bool = True
    solver_objective_value: float | None = None
    business_score: float | None = None
    business_score_policy_version: str = "FIELD_SERVICE_SCORE_V2"
    scenario_snapshot_hash: str = ""
    solver_config_hash: str = ""
    travel_model_version: str = "EUCLIDEAN_GRID_V2"
    metric_policy_version: str = "FIELD_SERVICE_METRICS_V2"
    solver_name: str = "fieldflow-greedy"
    solver_version: str = "1"


class ScenarioCreate(BaseModel):
    fixture_id: Identifier = "main"
    name: DisplayName | None = None


class LockRequest(BaseModel):
    work_order_id: Identifier
    technician_id: Identifier
    locked: bool = True


class OptimizeRequest(BaseModel):
    time_limit_seconds: float | None = Field(default=None, ge=1, le=30)
    strategy: Literal["balanced", "completion", "punctuality", "low_travel", "low_overtime", "fair_workload"] = "balanced"
    profile_id: Identifier | None = None


class ReplanRequest(BaseModel):
    emergency_order: WorkOrder | None = None
    current_time: int | None = Field(default=None, ge=0, le=1800)
    planning_time: int | None = Field(default=None, ge=0, le=1800)
    time_limit_seconds: float | None = Field(default=None, ge=1, le=30)
    strategy: Literal["balanced", "completion", "punctuality", "low_travel", "low_overtime", "fair_workload", "stable"] = "stable"
    profile_id: str | None = None
    idempotency_key: IdempotencyKey | None = None

    @model_validator(mode="after")
    def resolve_planning_time(self) -> ReplanRequest:
        if self.planning_time is None and self.current_time is None:
            raise ValueError("planning_time is required")
        if self.planning_time is not None and self.current_time is not None and self.planning_time != self.current_time:
            raise ValueError("planning_time and legacy current_time must match")
        resolved = self.planning_time if self.planning_time is not None else self.current_time
        self.planning_time = resolved
        self.current_time = resolved
        return self


class WorkOrderUpdate(BaseModel):
    customer_name: DisplayName | None = None
    title: DisplayName | None = None
    required_skills: list[Skill] | None = Field(default=None, min_length=1)
    location: Point | None = None
    service_duration: int | None = Field(default=None, gt=0, le=480)
    window_start: int | None = Field(default=None, ge=0, le=1800)
    window_end: int | None = Field(default=None, ge=0, le=1800)
    sla_deadline: int | None = Field(default=None, ge=0, le=1800)
    priority: Priority | None = None
    drop_penalty: int | None = Field(default=None, gt=0)
    status: WorkOrderStatus | None = None
    vip: bool | None = None
    is_emergency: bool | None = None
    reported_at: int | None = Field(default=None, ge=0, le=1800)
    note: ShortDescription | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> WorkOrderUpdate:
        if not self.model_fields_set:
            raise ValueError("工单更新至少需要一个字段")
        clearable = {"reported_at", "note"}
        invalid_nulls = [
            field
            for field in self.model_fields_set - clearable
            if getattr(self, field) is None
        ]
        if invalid_nulls:
            raise ValueError(f"字段不能为 null: {', '.join(sorted(invalid_nulls))}")
        return self


class TechnicianUpdate(BaseModel):
    name: DisplayName | None = None
    skills: list[Skill] | None = Field(default=None, min_length=1)
    shift_start: int | None = Field(default=None, ge=0, le=1440)
    shift_end: int | None = Field(default=None, ge=0, le=1800)
    start_location: Point | None = None
    overtime_limit: int | None = Field(default=None, ge=0, le=240)
    cost_per_minute: float | None = Field(default=None, gt=0)
    color: HexColor | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> TechnicianUpdate:
        if not self.model_fields_set:
            raise ValueError("技师更新至少需要一个字段")
        invalid_nulls = [
            field for field in self.model_fields_set if getattr(self, field) is None
        ]
        if invalid_nulls:
            raise ValueError(f"字段不能为 null: {', '.join(sorted(invalid_nulls))}")
        return self


class Comparison(BaseModel):
    scenario_id: str
    before: ScheduleResult
    after: ScheduleResult
    delta: dict[str, float | int | None]
    changed_orders: list[dict[str, Any]]
    comparable: bool = True
    same_scenario_snapshot: bool = True
    common_work_order_count: int = 0
    added_work_orders: list[str] = Field(default_factory=list)
    removed_work_orders: list[str] = Field(default_factory=list)
    modified_work_orders: list[str] = Field(default_factory=list)
    common_technicians: list[str] = Field(default_factory=list)


class ScenarioRevision(BaseModel):
    id: str
    scenario_id: str
    number: int
    reason: str
    scenario: ScheduleScenario
    created_at: str


class ScheduleArtifact(BaseModel):
    id: str
    role: Literal["baseline", "selected", "candidate"]
    strategy: str
    schedule: ScheduleResult


class PlanVersion(BaseModel):
    id: str
    scenario_id: str
    number: int
    action: Literal["baseline", "optimize", "replan", "activate", "restore", "experiment_publish"]
    label: ShortLabel
    data_revision: int
    source_version_id: str | None = None
    relation: Literal["new", "optimized_from", "replanned_from", "reactivated_from", "restored_from", "published_from_experiment", "fresh_after_data_change"] = "new"
    active: bool = False
    created_at: str
    scenario_snapshot: ScheduleScenario | None = None
    selected: ScheduleResult
    artifacts: list[ScheduleArtifact] = Field(default_factory=list)
    candidate_id: str | None = None
    scenario_snapshot_hash: str = ""
    source_plan_snapshot_hash: str | None = None
    coverage_status: PlanCoverageStatus = PlanCoverageStatus.current_and_complete


class PlanVersionPatch(BaseModel):
    label: ShortLabel


class ActivatePlanRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey


class CloneScenarioRequest(BaseModel):
    name: DisplayName
    idempotency_key: IdempotencyKey


class RollbackPreview(BaseModel):
    scenario_id: str
    source_version_id: str
    expected_revision: int
    confirmation_token: str
    added_work_orders: list[str] = Field(default_factory=list)
    removed_work_orders: list[str] = Field(default_factory=list)
    modified_work_orders: list[str] = Field(default_factory=list)
    completed_work_orders_reopened: list[str] = Field(default_factory=list)
    technician_changes: list[str] = Field(default_factory=list)
    lock_changes: list[str] = Field(default_factory=list)


class RestoreRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    confirmation_token: str = Field(min_length=16, max_length=128)
    reason: ShortLabel
    allow_reopen_completed: bool = False
    allow_delete_new_orders: bool = False
    idempotency_key: IdempotencyKey


class StrategyWeights(BaseModel):
    travel_weight: int = Field(default=4, ge=0, le=1000)
    sla_late_weight: int = Field(default=12, ge=0, le=1000)
    overtime_weight: int = Field(default=30, ge=0, le=1000)
    imbalance_weight: int = Field(default=1, ge=0, le=1000)
    replan_change_weight: int = Field(default=80, ge=0, le=2000)
    unassigned_penalty_scale: float = Field(default=1.0, ge=0.1, le=5.0)

    @model_validator(mode="after")
    def validate_objective(self) -> StrategyWeights:
        if not any((self.travel_weight, self.sla_late_weight, self.overtime_weight, self.imbalance_weight, self.replan_change_weight)):
            raise ValueError("至少保留一个非零目标权重")
        return self


class StrategyProfile(BaseModel):
    id: Identifier
    name: StrategyName
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    builtin: bool = False
    weights: StrategyWeights = Field(default_factory=StrategyWeights)
    time_limit_seconds: float = Field(default=2.0, ge=1, le=30)
    created_at: str


class StrategyProfileCreate(BaseModel):
    name: StrategyName
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    weights: StrategyWeights = Field(default_factory=StrategyWeights)
    time_limit_seconds: float = Field(default=2.0, ge=1, le=30)


class StrategyExperimentRequest(BaseModel):
    dataset: Literal["current", "strategy-medium", "strategy-stress"] = "current"
    profile_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    time_limit_seconds: float | None = Field(default=None, ge=1, le=30)

    @model_validator(mode="after")
    def validate_profiles(self) -> StrategyExperimentRequest:
        if len(set(self.profile_ids)) != len(self.profile_ids):
            raise ValueError("参与实验的策略不能重复")
        return self


class StrategyCandidate(BaseModel):
    id: str
    profile_id: str
    profile_name: str
    schedule: ScheduleResult
    evaluation_score: float
    advantages: list[str] = Field(default_factory=list)
    publishable: bool = True
    schedule_candidate_id: str | None = None
    pareto_optimal: bool = True
    dominated_by: list[str] = Field(default_factory=list)
    verification_report: ScheduleVerificationReport | None = None


class StrategyExperiment(BaseModel):
    id: str
    scenario_id: str
    dataset: str
    data_revision: int
    status: Literal[
        "QUEUED",
        "RUNNING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
        "FAILED",
        "INTERRUPTED",
    ]
    progress: int = Field(ge=0, le=100)
    error: str | None = None
    created_at: str
    profile_ids: list[str] = Field(default_factory=list)
    requested_time_limit_seconds: float | None = None
    scenario_snapshot: ScheduleScenario | None = None
    candidates: list[StrategyCandidate] = Field(default_factory=list)
    profile_snapshots: list[StrategyProfile] = Field(default_factory=list)
    fingerprint: str = ""
    scenario_snapshot_hash: str = ""
    score_policy_version: str = "FIELD_SERVICE_SCORE_V2"
    travel_model_version: str = "EUCLIDEAN_GRID_V2"
    solver_version: str = ""
    candidate_errors: dict[str, str] = Field(default_factory=dict)
    finished_at: str | None = None
    cancel_requested_at: str | None = None
    winner_candidate_id: str | None = None
    winner_plan_version_id: str | None = None
    published_at: str | None = None


class ExperimentPublishRequest(BaseModel):
    candidate_id: str
    expected_revision: int = Field(ge=0)


class VerificationIssue(BaseModel):
    code: str
    message: str
    work_order_id: str | None = None
    technician_id: str | None = None


class CoverageSummary(BaseModel):
    active_work_orders: int
    assigned_work_orders: int
    unassigned_work_orders: int
    missing_work_orders: list[str] = Field(default_factory=list)
    duplicate_assignments: list[str] = Field(default_factory=list)
    duplicate_unassigned: list[str] = Field(default_factory=list)
    overlapping_work_orders: list[str] = Field(default_factory=list)


class FrozenAssignment(BaseModel):
    work_order_id: Identifier
    technician_id: Identifier
    sequence: int = Field(ge=1)
    start_time: int = Field(ge=0, le=1800)
    finish_time: int = Field(ge=0, le=2280)
    reason: FreezeReason


class PlanningContext(BaseModel):
    planning_time: int = Field(ge=0, le=1800)
    source_plan_version_id: str | None = None
    source_plan_snapshot_hash: str | None = None
    scenario_revision: int = Field(ge=0)
    frozen_assignments: list[FrozenAssignment] = Field(default_factory=list)
    inferred_departure_warnings: list[Identifier] = Field(default_factory=list)


class ScheduleVerificationReport(BaseModel):
    valid: bool
    publishable: bool
    errors: list[VerificationIssue] = Field(default_factory=list)
    warnings: list[VerificationIssue] = Field(default_factory=list)
    coverage: CoverageSummary
    recomputed_kpis: ScheduleKPI | None = None
    checked_at: str


class ScheduleRun(BaseModel):
    id: str
    scenario_id: str
    action: Literal["baseline", "optimize", "replan", "activate", "restore", "experiment"]
    scenario_revision: int
    scenario_snapshot_hash: str
    source_plan_version_id: str | None = None
    source_plan_snapshot_hash: str | None = None
    solver_name: str
    solver_version: str
    solver_config_hash: str
    requested_time_limit_ms: int
    effective_time_limit_ms: int
    status: ScheduleRunStatus
    termination_reason: str | None = None
    solution_found: bool = False
    started_at: str
    finished_at: str | None = None
    candidate_id: str | None = None
    planning_context: PlanningContext | None = None
    planning_context_hash: str | None = None


class ScheduleCandidate(BaseModel):
    id: str
    run_id: str
    scenario_id: str
    scenario_revision: int
    scenario_snapshot_hash: str
    source_plan_version_id: str | None = None
    solver_config_hash: str
    schedule: ScheduleResult
    verification_report: ScheduleVerificationReport
    publishable: bool
    created_at: str
    planning_context: PlanningContext | None = None
    planning_context_hash: str | None = None
