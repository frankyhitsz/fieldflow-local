from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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
    infeasible = "INFEASIBLE"
    time_limit = "TIME_LIMIT"


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
    id: str
    name: str
    skills: list[Skill]
    shift_start: int = Field(ge=0, le=1440)
    shift_end: int = Field(ge=0, le=1800)
    start_location: Point
    overtime_limit: int = Field(default=60, ge=0, le=240)
    cost_per_minute: float = Field(default=1.0, gt=0)
    color: str = "#315c4b"

    @model_validator(mode="after")
    def validate_shift_and_skills(self) -> "Technician":
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be later than shift_start")
        if not self.skills:
            raise ValueError("technician requires at least one skill")
        self.skills = list(dict.fromkeys(self.skills))
        return self


class WorkOrder(BaseModel):
    id: str
    customer_name: str
    title: str
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
    note: str = ""

    @model_validator(mode="after")
    def validate_times(self) -> "WorkOrder":
        if self.window_end < self.window_start:
            raise ValueError("window_end must not precede window_start")
        if self.sla_deadline < self.window_start:
            raise ValueError("sla_deadline must not precede window_start")
        if not self.required_skills:
            raise ValueError("work order requires at least one skill")
        if self.reported_at is not None and self.reported_at > self.window_end:
            raise ValueError("reported_at must not be later than window_end")
        self.required_skills = list(dict.fromkeys(self.required_skills))
        return self


class LockedAssignment(BaseModel):
    work_order_id: str
    technician_id: str


class SolverConfig(BaseModel):
    time_limit_seconds: float = Field(default=2.0, ge=0.05, le=30)
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
    id: str
    name: str
    description: str
    planning_date: str = "2026-08-23"
    technicians: list[Technician]
    work_orders: list[WorkOrder]
    solver_config: SolverConfig = Field(default_factory=SolverConfig)
    locked_assignments: list[LockedAssignment] = Field(default_factory=list)
    source_scenario_id: str | None = None
    seed: int = 20260823
    revision: int = Field(default=0, ge=0)


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
    locked: bool = False
    changed: bool = False


class UnassignedWorkOrder(BaseModel):
    work_order_id: str
    reason: UnassignedReason
    detail: str
    suggestions: list[str] = Field(default_factory=list)


class TechnicianKPI(BaseModel):
    technician_id: str
    service_minutes: int
    travel_minutes: int
    overtime_minutes: int
    utilization: float
    assignment_count: int


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


class ScenarioCreate(BaseModel):
    fixture_id: str = "main"
    name: str | None = None


class LockRequest(BaseModel):
    work_order_id: str
    technician_id: str
    locked: bool = True


class OptimizeRequest(BaseModel):
    time_limit_seconds: float | None = Field(default=None, ge=0.05, le=30)
    strategy: Literal["balanced", "completion", "punctuality", "low_travel", "low_overtime", "fair_workload"] = "balanced"
    profile_id: str | None = None


class ReplanRequest(BaseModel):
    emergency_order: WorkOrder | None = None
    current_time: int = Field(default=600, ge=0, le=1800)
    time_limit_seconds: float | None = Field(default=None, ge=0.05, le=30)
    strategy: Literal["balanced", "completion", "punctuality", "low_travel", "low_overtime", "fair_workload", "stable"] = "stable"
    profile_id: str | None = None


class WorkOrderUpdate(BaseModel):
    customer_name: str | None = None
    title: str | None = None
    required_skills: list[Skill] | None = None
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
    note: str | None = None


class TechnicianUpdate(BaseModel):
    name: str | None = None
    skills: list[Skill] | None = None
    shift_start: int | None = Field(default=None, ge=0, le=1440)
    shift_end: int | None = Field(default=None, ge=0, le=1800)
    start_location: Point | None = None
    overtime_limit: int | None = Field(default=None, ge=0, le=240)
    cost_per_minute: float | None = Field(default=None, gt=0)
    color: str | None = None


class Comparison(BaseModel):
    scenario_id: str
    before: ScheduleResult
    after: ScheduleResult
    delta: dict[str, float | int | None]
    changed_orders: list[dict[str, Any]]


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
    action: Literal["baseline", "optimize", "replan", "restore", "experiment_publish"]
    label: str
    data_revision: int
    source_version_id: str | None = None
    relation: Literal["new", "optimized_from", "replanned_from", "restored_from", "published_from_experiment"] = "new"
    active: bool = False
    created_at: str
    scenario_snapshot: ScheduleScenario | None = None
    selected: ScheduleResult
    artifacts: list[ScheduleArtifact] = Field(default_factory=list)


class PlanVersionPatch(BaseModel):
    label: str = Field(min_length=1, max_length=60)


class RestoreRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class StrategyWeights(BaseModel):
    travel_weight: int = Field(default=4, ge=0, le=1000)
    sla_late_weight: int = Field(default=12, ge=0, le=1000)
    overtime_weight: int = Field(default=30, ge=0, le=1000)
    imbalance_weight: int = Field(default=1, ge=0, le=1000)
    replan_change_weight: int = Field(default=80, ge=0, le=2000)
    unassigned_penalty_scale: float = Field(default=1.0, ge=0.1, le=5.0)

    @model_validator(mode="after")
    def validate_objective(self) -> "StrategyWeights":
        if not any((self.travel_weight, self.sla_late_weight, self.overtime_weight, self.imbalance_weight, self.replan_change_weight)):
            raise ValueError("至少保留一个非零目标权重")
        return self


class StrategyProfile(BaseModel):
    id: str
    name: str = Field(min_length=2, max_length=30)
    description: str = Field(default="", max_length=160)
    builtin: bool = False
    weights: StrategyWeights = Field(default_factory=StrategyWeights)
    time_limit_seconds: float = Field(default=2.0, ge=0.05, le=30)
    created_at: str


class StrategyProfileCreate(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    description: str = Field(default="", max_length=160)
    weights: StrategyWeights = Field(default_factory=StrategyWeights)
    time_limit_seconds: float = Field(default=2.0, ge=0.05, le=30)


class StrategyExperimentRequest(BaseModel):
    dataset: Literal["current", "strategy-medium", "strategy-stress"] = "current"
    profile_ids: list[str] = Field(default_factory=list)
    time_limit_seconds: float | None = Field(default=None, ge=0.05, le=30)


class StrategyCandidate(BaseModel):
    id: str
    profile_id: str
    profile_name: str
    schedule: ScheduleResult
    evaluation_score: float
    advantages: list[str] = Field(default_factory=list)
    publishable: bool = True


class StrategyExperiment(BaseModel):
    id: str
    scenario_id: str
    dataset: str
    data_revision: int
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"]
    progress: int = Field(ge=0, le=100)
    error: str | None = None
    created_at: str
    profile_ids: list[str] = Field(default_factory=list)
    requested_time_limit_seconds: float | None = None
    scenario_snapshot: ScheduleScenario | None = None
    candidates: list[StrategyCandidate] = Field(default_factory=list)


class ExperimentPublishRequest(BaseModel):
    candidate_id: str
    expected_revision: int = Field(ge=0)
