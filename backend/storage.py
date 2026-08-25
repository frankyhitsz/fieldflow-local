from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .applicability import (
    applicability_from_legacy_status,
    coverage_status_from_applicability,
    reduce_plan_applicability,
)
from .fixtures import all_fixtures
from .hashing import content_hash
from .models import (
    AnalysisFailureManifest,
    AnalysisIntegrityStatus,
    AnalysisReservationManifest,
    AttestationRequirement,
    CapacityCounterfactualArtifact,
    CurrentWorkOrderDisposition,
    DecisionAnalysisArtifact,
    DecisionAnalysisContext,
    DecisionAnalysisRun,
    DecisionResultManifest,
    ExecutionSourceAssignment,
    ExecutionSourceContext,
    FieldImpact,
    FrozenBookingIdentity,
    OperationalMetrics,
    OperationalWorkOrderView,
    PlanApplicability,
    PlanCoverageStatus,
    PlanningContext,
    PlanningReservation,
    PlanUseCase,
    PlanVersion,
    PublicationPlanningContext,
    PublicationVerificationArtifact,
    ReattestationMode,
    RevisionChainStatus,
    RevisionProofOrigin,
    RiskComparisonRun,
    RiskTrialOutcomeArtifact,
    RouteEntryContext,
    ScenarioOperationalView,
    ScenarioRevision,
    ScheduleArtifact,
    ScheduleCandidate,
    ScheduleResult,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleScenario,
    SimulationScenarioSetArtifact,
    SourceSolverProvenance,
    StrategyExperiment,
    StrategyProfile,
    StrategyProfileCreate,
    StrategyWeights,
    TechnicianExecutionProjection,
    WorkOrder,
    WorkOrderExecutionEvent,
    WorkOrderExecutionRequest,
    WorkOrderExecutionResult,
    WorkOrderStatus,
)
from .planning import assignment_planning_fingerprint, assignment_source_fingerprint
from .provenance import build_decision_input_manifest, build_plan_manifest_payload
from .timeutils import service_ready_at
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider
from .verification import verify_schedule

SCHEMA_VERSION = 23

_INTEGRITY_RANK = {
    AnalysisIntegrityStatus.failed: 0,
    AnalysisIntegrityStatus.legacy_unattested: 1,
    AnalysisIntegrityStatus.verified: 2,
}


def _effective_integrity(*statuses: AnalysisIntegrityStatus) -> AnalysisIntegrityStatus:
    return min(statuses, key=_INTEGRITY_RANK.__getitem__)


def _artifact_hash_payload(artifact: DecisionAnalysisArtifact) -> dict:
    return artifact.model_dump(
        exclude={
            "artifact_hash",
            "integrity_status",
            "self_integrity",
            "parent_analysis_integrity",
            "effective_integrity",
            "business_result_available",
            "attestation_requirement",
        },
        mode="json",
    )


def _risk_comparison_hash_payload(comparison: RiskComparisonRun) -> dict:
    return comparison.model_dump(
        exclude={
            "id",
            "number",
            "created_at",
            "comparison_hash",
            "integrity_status",
            "self_integrity",
            "effective_integrity",
            "business_result_available",
            "attestation_requirement",
        },
        mode="json",
    )


def _risk_comparison_input_payload(comparison: RiskComparisonRun) -> dict[str, object]:
    return {
        "policy_version": "FIELD_SERVICE_RISK_COMPARISON_INPUT_V1",
        "scenario_id": comparison.scenario_id,
        "before_analysis_id": comparison.before_analysis_id,
        "after_analysis_id": comparison.after_analysis_id,
        "before_analysis_manifest_hash": comparison.before_analysis_manifest_hash,
        "after_analysis_manifest_hash": comparison.after_analysis_manifest_hash,
        "before_trial_artifact_id": comparison.before_trial_artifact_id,
        "after_trial_artifact_id": comparison.after_trial_artifact_id,
        "before_trial_artifact_hash": comparison.before_trial_artifact_hash,
        "after_trial_artifact_hash": comparison.after_trial_artifact_hash,
        "before_scenario_artifact_id": comparison.before_scenario_artifact_id,
        "after_scenario_artifact_id": comparison.after_scenario_artifact_id,
        "before_scenario_artifact_hash": comparison.before_scenario_artifact_hash,
        "after_scenario_artifact_hash": comparison.after_scenario_artifact_hash,
        "scenario_set_hash": comparison.scenario_set_hash,
        "trials": comparison.trials,
    }


def _upgrade_technician_costs(payload: object) -> bool:
    """Convert legacy floating currency units to explicit integer cents in snapshots."""
    changed = False
    if isinstance(payload, dict):
        technicians = payload.get("technicians")
        if isinstance(technicians, list):
            for technician in technicians:
                if not isinstance(technician, dict) or not {"id", "skills", "shift_start"} <= technician.keys():
                    continue
                if "cost_per_minute_cents" not in technician:
                    legacy = technician.pop("cost_per_minute", 1.0)
                    technician["cost_per_minute_cents"] = max(1, round(float(legacy) * 100))
                    changed = True
                elif "cost_per_minute" in technician:
                    technician.pop("cost_per_minute")
                    changed = True
        for value in payload.values():
            changed = _upgrade_technician_costs(value) or changed
    elif isinstance(payload, list):
        for value in payload:
            changed = _upgrade_technician_costs(value) or changed
    return changed


class ScenarioRevisionConflict(RuntimeError):
    def __init__(self, expected: int, current: int):
        super().__init__(f"scenario revision changed: expected {expected}, current {current}")
        self.expected = expected
        self.current = current


class ActivePlanConflict(RuntimeError):
    def __init__(self, expected: str | None, current: str | None):
        super().__init__(f"active plan changed: expected {expected}, current {current}")
        self.expected = expected
        self.current = current


class PublicationConflict(RuntimeError):
    def __init__(self, message: str, *, code: str = "PUBLICATION_CONFLICT", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class DecisionAnalysisIntegrityError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        record_id: str,
        record_type: str,
        code: str = "RECORD_INTEGRITY_FAILED",
        details: dict | None = None,
    ):
        super().__init__(message)
        self.record_id = record_id
        self.record_type = record_type
        self.code = code
        self.details = details or {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


_EXECUTION_EVENT_TRUST_FIELDS = {
    "event_content_hash",
    "self_integrity",
    "source_plan_integrity",
    "effective_integrity",
}


def _execution_event_content_hash(event: WorkOrderExecutionEvent) -> str:
    return content_hash(event.model_dump(exclude=_EXECUTION_EVENT_TRUST_FIELDS, mode="json"))


def _scenario_revision_hash(revision: ScenarioRevision) -> str:
    return content_hash(
        revision.model_dump(
            exclude={
                "revision_hash",
                "proof_origin",
                "chain_status",
                "self_integrity",
                "effective_integrity",
            },
            mode="json",
        )
    )


def _planning_reservation_hash(reservation: PlanningReservation) -> str:
    return content_hash(reservation.model_dump(exclude={"reservation_hash"}, mode="json"))


def _plan_applicability_hash(
    plan_version_id: str,
    scenario_id: str,
    applicability: PlanApplicability,
) -> str:
    return content_hash(
        {
            "plan_version_id": plan_version_id,
            "scenario_id": scenario_id,
            "applicability": applicability.model_dump(exclude={"projection_hash"}, mode="json"),
        }
    )


def _parse_decision_artifact(payload: object) -> DecisionAnalysisArtifact:
    if not isinstance(payload, dict):
        raise ValueError("artifact payload is not an object")
    artifact_type = payload.get("artifact_type")
    if artifact_type == "SIMULATION_SCENARIO_SET":
        return SimulationScenarioSetArtifact.model_validate(payload)
    if artifact_type == "RISK_TRIAL_OUTCOMES":
        return RiskTrialOutcomeArtifact.model_validate(payload)
    return CapacityCounterfactualArtifact.model_validate(payload)


def _analysis_context_from_run(run: DecisionAnalysisRun) -> DecisionAnalysisContext:
    return DecisionAnalysisContext(
        analysis_scope=run.analysis_scope,
        current_execution_watermark=run.current_execution_watermark,
        analysis_as_of_time=run.analysis_as_of_time,
        execution_context_hash=run.execution_context_hash,
        actual_execution_included=run.actual_execution_included,
        active_booking_ids=run.active_booking_ids,
    )


def _analysis_manifest_payload(run: DecisionAnalysisRun) -> dict[str, object]:
    return {
        "policy_version": "FIELD_SERVICE_ANALYSIS_MANIFEST_V2",
        "analysis_id": run.id,
        "input_hash": run.input_hash,
        "input_manifest": run.input_manifest,
        "reservation_manifest": run.reservation_manifest,
        "result_manifest": run.result_manifest,
        "failure_manifest": run.failure_manifest,
        "artifact_manifest": run.artifact_manifest,
        "status": run.status,
        "build_sha": run.build_sha,
        "algorithm_version": run.algorithm_version,
        "schedule_hash": run.schedule_hash,
        "attestation_requirement": run.attestation_requirement,
    }


def _failed_integrity_copy(run: DecisionAnalysisRun, reason: str) -> DecisionAnalysisRun:
    checked = run.model_copy(deep=True)
    checked.integrity_status = AnalysisIntegrityStatus.failed
    checked.self_integrity = AnalysisIntegrityStatus.failed
    checked.effective_integrity = AnalysisIntegrityStatus.failed
    checked.result = None
    checked.error = {
        "code": "ANALYSIS_INTEGRITY_FAILED",
        "message": "经营分析证明缺失、损坏或与冻结输入不一致",
        "integrity_reason": reason,
    }
    return checked


BUILTIN_PROFILES: tuple[StrategyProfile, ...] = (
    StrategyProfile(
        id="balanced",
        name="均衡",
        description="兼顾计划覆盖、准时、行程和加班",
        builtin=True,
        weights=StrategyWeights(overtime_weight=30),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="completion",
        name="覆盖率优先",
        description="优先把更多工单排入计划，必要时增加行程和计划加班",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=1,
            sla_late_weight=1,
            overtime_weight=1,
            imbalance_weight=0,
            replan_change_weight=60,
            unassigned_penalty_scale=5,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="punctuality",
        name="准时优先",
        description="优先按时完成，必要时少排部分工单",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=2,
            sla_late_weight=200,
            overtime_weight=30,
            imbalance_weight=2,
            replan_change_weight=100,
            unassigned_penalty_scale=1,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="low_travel",
        name="低行程",
        description="减少跨区往返，可能少排部分工单",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=30,
            sla_late_weight=8,
            overtime_weight=8,
            imbalance_weight=1,
            replan_change_weight=80,
            unassigned_penalty_scale=0.8,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="low_overtime",
        name="低加班",
        description="尽量在正常班次内收工",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=2,
            sla_late_weight=5,
            overtime_weight=500,
            imbalance_weight=1,
            replan_change_weight=90,
            unassigned_penalty_scale=0.5,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="fair_workload",
        name="工作量公平",
        description="压低最忙技师的标准化服务负荷，通常让分配更均衡",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=3,
            sla_late_weight=10,
            overtime_weight=8,
            imbalance_weight=10,
            replan_change_weight=90,
            unassigned_penalty_scale=1.3,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
    StrategyProfile(
        id="stable",
        name="稳定优先",
        description="局部重排时尽量保留技师和顺序",
        builtin=True,
        weights=StrategyWeights(
            travel_weight=4,
            sla_late_weight=16,
            overtime_weight=10,
            imbalance_weight=2,
            replan_change_weight=260,
            unassigned_penalty_scale=1,
        ),
        time_limit_seconds=2,
        created_at="2026-08-23T00:00:00+00:00",
    ),
)


class Store:
    def __init__(
        self,
        path: str | Path,
        travel_provider: TravelTimeProvider = DEFAULT_TRAVEL_PROVIDER,
    ):
        self.path = str(path)
        self.travel_provider = travel_provider
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _backup_legacy(self, con: sqlite3.Connection) -> Path | None:
        tables = {row["name"] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        protected = ("scenarios", "schedules", "plan_versions", "scenario_revisions", "strategy_experiments")
        if not any(table in tables and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in protected):
            return None
        source = Path(self.path)
        # Include microseconds so repeated recovery attempts cannot overwrite the
        # only copy of a legacy database created during the same second.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = source.with_name(f"{source.stem}.legacy-{stamp}.db")
        target = sqlite3.connect(backup)
        try:
            con.backup(target)
        finally:
            target.close()
        return backup

    @staticmethod
    def _migrate_relational_schema(con: sqlite3.Connection) -> None:
        """Rebuild v2/v3 tables with enforceable relationships without losing history."""
        con.commit()
        con.execute("PRAGMA foreign_keys = OFF")
        for trigger in ("sync_active_plan_insert", "sync_active_plan_update", "sync_active_plan_delete"):
            con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        con.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE scenarios_new (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                active_plan_version_id TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(active_plan_version_id) REFERENCES plan_versions(id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE schedules_new (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
            );
            CREATE TABLE scenario_revisions_new (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scenario_id, number),
                FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
            );
            CREATE TABLE plan_versions_new (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                attestation_requirement TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED',
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scenario_id, number),
                UNIQUE(id, scenario_id),
                FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
            );
            CREATE TABLE strategy_experiments_new (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
            );
            CREATE TABLE schedule_artifacts_new (
                id TEXT PRIMARY KEY,
                plan_version_id TEXT,
                experiment_id TEXT,
                role TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK ((plan_version_id IS NOT NULL AND experiment_id IS NULL) OR (plan_version_id IS NULL AND experiment_id IS NOT NULL)),
                FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE,
                FOREIGN KEY(experiment_id) REFERENCES strategy_experiments(id) ON DELETE CASCADE
            );
            CREATE TABLE publication_keys_new (
                key TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                plan_version_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS migration_orphans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table TEXT NOT NULL,
                source_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                reason TEXT NOT NULL,
                migrated_at TEXT NOT NULL
            );

            INSERT INTO scenarios_new(id, payload, active_plan_version_id, updated_at)
            SELECT s.id, s.payload,
                   CASE WHEN EXISTS (SELECT 1 FROM plan_versions p WHERE p.id=s.active_plan_version_id)
                        THEN s.active_plan_version_id ELSE NULL END,
                   s.updated_at
            FROM scenarios s;
            INSERT INTO schedules_new SELECT s.* FROM schedules s WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=s.scenario_id);
            INSERT INTO scenario_revisions_new(id, scenario_id, number, reason, payload, created_at)
            SELECT r.id, r.scenario_id, r.number, r.reason, r.payload, r.created_at
            FROM scenario_revisions r WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=r.scenario_id);
            INSERT INTO plan_versions_new(id, scenario_id, number, payload, created_at)
            SELECT p.id, p.scenario_id, p.number, p.payload, p.created_at
            FROM plan_versions p WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=p.scenario_id);
            INSERT INTO strategy_experiments_new SELECT e.* FROM strategy_experiments e WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=e.scenario_id);
            INSERT INTO schedule_artifacts_new
            SELECT a.* FROM schedule_artifacts a
            WHERE (a.plan_version_id IS NOT NULL AND a.experiment_id IS NULL AND EXISTS (SELECT 1 FROM plan_versions p WHERE p.id=a.plan_version_id))
               OR (a.plan_version_id IS NULL AND a.experiment_id IS NOT NULL AND EXISTS (SELECT 1 FROM strategy_experiments e WHERE e.id=a.experiment_id));
            INSERT INTO publication_keys_new
            SELECT k.* FROM publication_keys k WHERE EXISTS (SELECT 1 FROM plan_versions p WHERE p.id=k.plan_version_id);
            INSERT INTO migration_orphans(source_table, source_id, payload, reason, migrated_at)
            SELECT 'schedule_artifacts', a.id, a.payload, 'invalid or missing parent', CURRENT_TIMESTAMP
            FROM schedule_artifacts a
            WHERE NOT (
                (a.plan_version_id IS NOT NULL AND a.experiment_id IS NULL AND EXISTS (SELECT 1 FROM plan_versions p WHERE p.id=a.plan_version_id))
                OR (a.plan_version_id IS NULL AND a.experiment_id IS NOT NULL AND EXISTS (SELECT 1 FROM strategy_experiments e WHERE e.id=a.experiment_id))
            );
            INSERT INTO migration_orphans(source_table, source_id, payload, reason, migrated_at)
            SELECT 'publication_keys', k.key, k.request_fingerprint, 'missing plan version', CURRENT_TIMESTAMP
            FROM publication_keys k WHERE NOT EXISTS (SELECT 1 FROM plan_versions p WHERE p.id=k.plan_version_id);

            DROP TABLE schedule_artifacts;
            DROP TABLE publication_keys;
            DROP TABLE schedules;
            DROP TABLE scenario_revisions;
            DROP TABLE strategy_experiments;
            DROP TABLE plan_versions;
            DROP TABLE scenarios;
            ALTER TABLE scenarios_new RENAME TO scenarios;
            ALTER TABLE schedules_new RENAME TO schedules;
            ALTER TABLE scenario_revisions_new RENAME TO scenario_revisions;
            ALTER TABLE plan_versions_new RENAME TO plan_versions;
            ALTER TABLE strategy_experiments_new RENAME TO strategy_experiments;
            ALTER TABLE schedule_artifacts_new RENAME TO schedule_artifacts;
            ALTER TABLE publication_keys_new RENAME TO publication_keys;
            COMMIT;
            """
        )
        con.execute("PRAGMA foreign_keys = ON")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"foreign key check failed after migration: {violations[:3]}")

    def _rehash_migrated_revision_chains(self, con: sqlite3.Connection, *, reason: str) -> None:
        """Rebuild proofs only for a contiguous, relationally valid legacy chain."""
        for scenario_row in con.execute(
            "SELECT DISTINCT scenario_id FROM scenario_revisions ORDER BY scenario_id"
        ).fetchall():
            previous_hash: str | None = None
            expected_number = 0
            ancestor_invalid = False
            for revision_row in con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number, created_at, id",
                (scenario_row["scenario_id"],),
            ).fetchall():
                try:
                    revision = ScenarioRevision.model_validate_json(revision_row["payload"])
                    relation_valid = (
                        not ancestor_invalid
                        and revision.id == revision_row["id"]
                        and revision.scenario_id == revision_row["scenario_id"]
                        and revision.number == revision_row["number"]
                        and revision.number == expected_number
                        and revision.reason == revision_row["reason"]
                        and revision.created_at == revision_row["created_at"]
                        and revision.scenario.id == revision.scenario_id
                        and revision.scenario.revision == revision.number
                    )
                    if not relation_valid:
                        raise ValueError("legacy revision identity or continuity mismatch")
                except (TypeError, ValueError):
                    ancestor_invalid = True
                    self._record_read_isolation(
                        con,
                        "scenario_revisions",
                        str(revision_row["id"]),
                        str(revision_row["payload"]),
                        reason,
                    )
                    continue
                revision.scenario_snapshot_hash = content_hash(revision.scenario)
                revision.previous_revision_hash = previous_hash
                revision.revision_hash = _scenario_revision_hash(revision)
                con.execute(
                    "UPDATE scenario_revisions SET payload=? WHERE id=?",
                    (revision.model_dump_json(), revision.id),
                )
                previous_hash = revision.revision_hash
                expected_number += 1

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as con:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{version} is newer than this application supports (v{SCHEMA_VERSION})"
                )
            con.execute("PRAGMA journal_mode = WAL")
            if version < SCHEMA_VERSION:
                self._backup_legacy(con)
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    active_plan_version_id TEXT,
                    current_snapshot_hash TEXT NOT NULL DEFAULT '',
                    latest_revision_number INTEGER NOT NULL DEFAULT -1,
                    latest_revision_hash TEXT NOT NULL DEFAULT '',
                    proof_origin TEXT NOT NULL DEFAULT 'NATIVE_ATTESTED' CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED')),
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(active_plan_version_id) REFERENCES plan_versions(id) DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS scenario_revisions (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    proof_origin TEXT NOT NULL DEFAULT 'NATIVE_ATTESTED' CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED')),
                    chain_status TEXT NOT NULL DEFAULT 'VERIFIED' CHECK(chain_status IN ('VERIFIED', 'ROOT_INVALID', 'GAP_DETECTED', 'ANCESTOR_INVALID')),
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS plan_versions (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    attestation_requirement TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(attestation_requirement IN ('REQUIRED', 'LEGACY_MIGRATED')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    UNIQUE(id, scenario_id),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS plan_applicability (
                    plan_version_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    coverage_status TEXT NOT NULL CHECK(coverage_status IN ('CURRENT_AND_COMPLETE', 'PARTIAL_NEW_DEMAND', 'STALE_DATA_CHANGED')),
                    route_executable INTEGER NOT NULL DEFAULT 1 CHECK(route_executable IN (0, 1)),
                    coverage_complete INTEGER NOT NULL DEFAULT 1 CHECK(coverage_complete IN (0, 1)),
                    planning_current INTEGER NOT NULL DEFAULT 1 CHECK(planning_current IN (0, 1)),
                    metrics_current INTEGER NOT NULL DEFAULT 1 CHECK(metrics_current IN (0, 1)),
                    commercial_current INTEGER NOT NULL DEFAULT 1 CHECK(commercial_current IN (0, 1)),
                    reoptimization_opportunity INTEGER NOT NULL DEFAULT 0 CHECK(reoptimization_opportunity IN (0, 1)),
                    invalid_assignment_ids TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(invalid_assignment_ids) AND json_type(invalid_assignment_ids)='array'),
                    evaluated_scenario_revision INTEGER,
                    evaluated_scenario_snapshot_hash TEXT NOT NULL DEFAULT '',
                    reducer_policy_version TEXT NOT NULL DEFAULT 'FIELD_SERVICE_PLAN_APPLICABILITY_V2',
                    projection_hash TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_version_id, scenario_id) REFERENCES plan_versions(id, scenario_id) ON DELETE CASCADE,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS plan_metadata (
                    plan_version_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS decision_analysis_runs (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    plan_version_id TEXT NOT NULL,
                    analysis_type TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'INTERRUPTED')),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    analysis_manifest_hash TEXT,
                    attestation_requirement TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(attestation_requirement IN ('REQUIRED', 'LEGACY_MIGRATED')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                    FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS decision_analysis_artifacts (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    analysis_run_id TEXT NOT NULL,
                    option_id TEXT NOT NULL,
                    attestation_requirement TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(attestation_requirement IN ('REQUIRED', 'LEGACY_MIGRATED')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(analysis_run_id, option_id),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                    FOREIGN KEY(analysis_run_id) REFERENCES decision_analysis_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS decision_analysis_attempts (
                    logical_analysis_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    analysis_run_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(logical_analysis_id, attempt_number),
                    FOREIGN KEY(analysis_run_id) REFERENCES decision_analysis_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS risk_comparison_runs (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    comparison_hash TEXT NOT NULL,
                    comparison_input_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_fingerprint TEXT,
                    attestation_requirement TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(attestation_requirement IN ('REQUIRED', 'LEGACY_MIGRATED')),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    UNIQUE(scenario_id, comparison_hash),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS migration_orphans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_table TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    migrated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_artifacts (
                    id TEXT PRIMARY KEY,
                    plan_version_id TEXT,
                    experiment_id TEXT,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK ((plan_version_id IS NOT NULL AND experiment_id IS NULL) OR (plan_version_id IS NULL AND experiment_id IS NOT NULL)),
                    FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE,
                    FOREIGN KEY(experiment_id) REFERENCES strategy_experiments(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS strategy_profiles (
                    id TEXT PRIMARY KEY,
                    builtin INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_experiments (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS publication_keys (
                    key TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    plan_version_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS schedule_candidates (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    publishable INTEGER NOT NULL CHECK(publishable IN (0, 1)),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES schedule_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS planning_reservations (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    reservation_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS command_keys (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    publication_key TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, key)
                );
                CREATE TABLE IF NOT EXISTS work_order_execution_events (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('start', 'complete')),
                    sequence INTEGER NOT NULL,
                    occurred_at INTEGER NOT NULL,
                    technician_id TEXT NOT NULL DEFAULT '',
                    plan_version_id TEXT NOT NULL DEFAULT '',
                    booking_id TEXT NOT NULL DEFAULT '',
                    source_assignment_hash TEXT NOT NULL DEFAULT '',
                    event_content_hash TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_scenario ON schedules(scenario_id, version);
                CREATE INDEX IF NOT EXISTS idx_plan_versions_scenario ON plan_versions(scenario_id, number);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_versions_id_scenario_unique ON plan_versions(id, scenario_id);
                CREATE INDEX IF NOT EXISTS idx_revisions_scenario ON scenario_revisions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_plan ON schedule_artifacts(plan_version_id);
                CREATE INDEX IF NOT EXISTS idx_runs_scenario ON schedule_runs(scenario_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_run ON schedule_candidates(run_id);
                CREATE INDEX IF NOT EXISTS idx_commands_status ON command_keys(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_execution_events_order ON work_order_execution_events(scenario_id, work_order_id, sequence);
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(scenarios)")}
            if "active_plan_version_id" not in columns:
                con.execute("ALTER TABLE scenarios ADD COLUMN active_plan_version_id TEXT")
            event_columns = {row[1] for row in con.execute("PRAGMA table_info(work_order_execution_events)")}
            event_schema_upgraded = "sequence" not in event_columns
            if "sequence" not in event_columns:
                con.execute("ALTER TABLE work_order_execution_events ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
            event_relational_columns = {
                "technician_id": "TEXT NOT NULL DEFAULT ''",
                "plan_version_id": "TEXT NOT NULL DEFAULT ''",
                "booking_id": "TEXT NOT NULL DEFAULT ''",
                "source_assignment_hash": "TEXT NOT NULL DEFAULT ''",
                "event_content_hash": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in event_relational_columns.items():
                if column not in event_columns:
                    event_schema_upgraded = True
                    con.execute(f"ALTER TABLE work_order_execution_events ADD COLUMN {column} {definition}")
            if version < 21 or event_schema_upgraded:
                for event_row in con.execute("SELECT rowid, payload FROM work_order_execution_events").fetchall():
                    try:
                        event = WorkOrderExecutionEvent.model_validate_json(event_row["payload"])
                    except (TypeError, ValueError):
                        continue
                    event.event_content_hash = _execution_event_content_hash(event)
                    con.execute(
                        """
                        UPDATE work_order_execution_events
                        SET technician_id=?, plan_version_id=?, booking_id=?, source_assignment_hash=?,
                            event_content_hash=?, payload=?
                        WHERE rowid=?
                        """,
                        (
                            event.technician_id,
                            event.plan_version_id,
                            event.booking_id,
                            event.source_assignment_hash,
                            event.event_content_hash,
                            event.model_dump_json(),
                            event_row["rowid"],
                        ),
                    )
            if 10 <= version < 22:
                for scenario_row in con.execute(
                    "SELECT DISTINCT scenario_id FROM scenario_revisions ORDER BY scenario_id"
                ).fetchall():
                    previous_hash: str | None = None
                    revision_rows = con.execute(
                        "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number",
                        (scenario_row["scenario_id"],),
                    ).fetchall()
                    for revision_row in revision_rows:
                        try:
                            revision = ScenarioRevision.model_validate_json(revision_row["payload"])
                            relation_valid = (
                                revision.id == revision_row["id"]
                                and revision.scenario_id == revision_row["scenario_id"]
                                and revision.number == revision_row["number"]
                                and revision.reason == revision_row["reason"]
                                and revision.created_at == revision_row["created_at"]
                            )
                            if not relation_valid:
                                raise ValueError("scenario revision identity mismatch")
                        except (TypeError, ValueError):
                            self._record_read_isolation(
                                con,
                                "scenario_revisions",
                                str(revision_row["id"]),
                                str(revision_row["payload"]),
                                "v22 migration: malformed revision identity",
                            )
                            previous_hash = None
                            continue
                        revision.scenario_snapshot_hash = content_hash(revision.scenario)
                        revision.previous_revision_hash = previous_hash
                        revision.revision_hash = _scenario_revision_hash(revision)
                        con.execute(
                            "UPDATE scenario_revisions SET payload=? WHERE id=?",
                            (revision.model_dump_json(), revision.id),
                        )
                        previous_hash = revision.revision_hash
            # v21 already has the constrained applicability table, so its v23
            # proof backfill can run before the legacy v21 repair below.
            if 21 <= version < 23:
                scenario_columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(scenarios)")}
                for column, definition in {
                    "current_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                    "latest_revision_number": "INTEGER NOT NULL DEFAULT -1",
                    "latest_revision_hash": "TEXT NOT NULL DEFAULT ''",
                    "proof_origin": (
                        "TEXT NOT NULL DEFAULT 'MIGRATION_BACKFILLED' "
                        "CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED'))"
                    ),
                }.items():
                    if column not in scenario_columns:
                        con.execute(f"ALTER TABLE scenarios ADD COLUMN {column} {definition}")
                revision_columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(scenario_revisions)")}
                for column, definition in {
                    "proof_origin": (
                        "TEXT NOT NULL DEFAULT 'MIGRATION_BACKFILLED' "
                        "CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED'))"
                    ),
                    "chain_status": (
                        "TEXT NOT NULL DEFAULT 'VERIFIED' "
                        "CHECK(chain_status IN ('VERIFIED', 'ROOT_INVALID', 'GAP_DETECTED', 'ANCESTOR_INVALID'))"
                    ),
                }.items():
                    if column not in revision_columns:
                        con.execute(f"ALTER TABLE scenario_revisions ADD COLUMN {column} {definition}")
                applicability_columns = {
                    str(row["name"]) for row in con.execute("PRAGMA table_info(plan_applicability)")
                }
                for column, definition in {
                    "evaluated_scenario_revision": "INTEGER",
                    "evaluated_scenario_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                    "reducer_policy_version": ("TEXT NOT NULL DEFAULT 'FIELD_SERVICE_PLAN_APPLICABILITY_V2'"),
                    "projection_hash": "TEXT NOT NULL DEFAULT ''",
                }.items():
                    if column not in applicability_columns:
                        con.execute(f"ALTER TABLE plan_applicability ADD COLUMN {column} {definition}")
                con.execute("UPDATE scenario_revisions SET proof_origin='MIGRATION_BACKFILLED'")
                for scenario_row in con.execute("SELECT id, payload FROM scenarios ORDER BY id").fetchall():
                    scenario_id = str(scenario_row["id"])
                    previous_hash: str | None = None
                    expected_number = 0
                    ancestor_invalid = False
                    latest_verified: ScenarioRevision | None = None
                    for revision_row in con.execute(
                        "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number, created_at, id",
                        (scenario_id,),
                    ).fetchall():
                        status = RevisionChainStatus.verified
                        revision: ScenarioRevision | None = None
                        try:
                            revision = ScenarioRevision.model_validate_json(revision_row["payload"])
                            relation_valid = (
                                revision.id == revision_row["id"]
                                and revision.scenario_id == revision_row["scenario_id"]
                                and revision.number == revision_row["number"]
                                and revision.reason == revision_row["reason"]
                                and revision.created_at == revision_row["created_at"]
                                and revision.scenario.id == revision.scenario_id
                                and revision.scenario.revision == revision.number
                                and revision.scenario_snapshot_hash == content_hash(revision.scenario)
                                and revision.revision_hash == _scenario_revision_hash(revision)
                                and revision.previous_revision_hash == previous_hash
                            )
                            if ancestor_invalid:
                                status = RevisionChainStatus.ancestor_invalid
                            elif revision.number != expected_number:
                                status = (
                                    RevisionChainStatus.root_invalid
                                    if expected_number == 0
                                    else RevisionChainStatus.gap_detected
                                )
                            elif not relation_valid:
                                status = (
                                    RevisionChainStatus.root_invalid
                                    if expected_number == 0
                                    else RevisionChainStatus.ancestor_invalid
                                )
                        except (TypeError, ValueError):
                            status = (
                                RevisionChainStatus.root_invalid
                                if expected_number == 0
                                else RevisionChainStatus.ancestor_invalid
                            )
                        if status is RevisionChainStatus.verified and revision is not None:
                            revision.proof_origin = RevisionProofOrigin.migration_backfilled
                            revision.chain_status = status
                            con.execute(
                                "UPDATE scenario_revisions SET payload=?, proof_origin=?, chain_status=? WHERE id=?",
                                (
                                    revision.model_dump_json(),
                                    revision.proof_origin.value,
                                    status.value,
                                    revision.id,
                                ),
                            )
                            latest_verified = revision
                            previous_hash = revision.revision_hash
                            expected_number += 1
                        else:
                            ancestor_invalid = True
                            con.execute(
                                "UPDATE scenario_revisions SET proof_origin='MIGRATION_BACKFILLED', chain_status=? WHERE id=?",
                                (status.value, revision_row["id"]),
                            )
                            self._record_read_isolation(
                                con,
                                "scenario_revisions",
                                str(revision_row["id"]),
                                str(revision_row["payload"]),
                                f"v23 migration: {status.value.lower()}",
                            )
                    if latest_verified is None:
                        con.execute(
                            "UPDATE scenarios SET current_snapshot_hash='', latest_revision_number=-1, "
                            "latest_revision_hash='', proof_origin='MIGRATION_BACKFILLED' WHERE id=?",
                            (scenario_id,),
                        )
                    else:
                        con.execute(
                            "UPDATE scenarios SET current_snapshot_hash=?, latest_revision_number=?, "
                            "latest_revision_hash=?, proof_origin='MIGRATION_BACKFILLED' WHERE id=?",
                            (
                                latest_verified.scenario_snapshot_hash,
                                latest_verified.number,
                                latest_verified.revision_hash,
                                scenario_id,
                            ),
                        )
                for applicability_row in con.execute("SELECT * FROM plan_applicability").fetchall():
                    head = con.execute(
                        "SELECT latest_revision_number, current_snapshot_hash FROM scenarios WHERE id=?",
                        (applicability_row["scenario_id"],),
                    ).fetchone()
                    applicability = PlanApplicability(
                        route_executable=bool(applicability_row["route_executable"]),
                        coverage_complete=bool(applicability_row["coverage_complete"]),
                        planning_current=bool(applicability_row["planning_current"]),
                        metrics_current=bool(applicability_row["metrics_current"]),
                        commercial_current=bool(applicability_row["commercial_current"]),
                        reoptimization_opportunity=bool(applicability_row["reoptimization_opportunity"]),
                        invalid_assignment_ids=json.loads(applicability_row["invalid_assignment_ids"]),
                        evaluated_scenario_revision=(int(head["latest_revision_number"]) if head else None),
                        evaluated_scenario_snapshot_hash=(str(head["current_snapshot_hash"]) if head else ""),
                    )
                    applicability.projection_hash = _plan_applicability_hash(
                        str(applicability_row["plan_version_id"]),
                        str(applicability_row["scenario_id"]),
                        applicability,
                    )
                    con.execute(
                        "UPDATE plan_applicability SET evaluated_scenario_revision=?, "
                        "evaluated_scenario_snapshot_hash=?, reducer_policy_version=?, projection_hash=? "
                        "WHERE plan_version_id=?",
                        (
                            applicability.evaluated_scenario_revision,
                            applicability.evaluated_scenario_snapshot_hash,
                            applicability.reducer_policy_version,
                            applicability.projection_hash,
                            applicability_row["plan_version_id"],
                        ),
                    )
            command_columns = {row[1] for row in con.execute("PRAGMA table_info(command_keys)")}
            if "publication_key" not in command_columns:
                con.execute("ALTER TABLE command_keys ADD COLUMN publication_key TEXT")
                con.execute("UPDATE command_keys SET publication_key=key WHERE namespace='schedule-solve'")
                con.execute(
                    "UPDATE command_keys SET publication_key=namespace || ':' || key WHERE namespace LIKE '%:replan'"
                )
            if version < 18:
                for trigger in (
                    "prevent_legacy_plan_insert",
                    "prevent_plan_attestation_change",
                    "prevent_legacy_analysis_insert",
                    "prevent_analysis_attestation_change",
                    "prevent_legacy_artifact_insert",
                    "prevent_artifact_attestation_change",
                ):
                    con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                for table in ("plan_versions", "decision_analysis_runs", "decision_analysis_artifacts"):
                    table_columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                    if "attestation_requirement" not in table_columns:
                        con.execute(
                            f"ALTER TABLE {table} ADD COLUMN attestation_requirement TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED'"
                        )
            decision_run_sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_analysis_runs'"
            ).fetchone()["sql"]
            if "UNIQUE(plan_version_id, analysis_type, input_hash)" in decision_run_sql:
                con.commit()
                con.execute("PRAGMA foreign_keys = OFF")
                con.executescript(
                    """
                    BEGIN IMMEDIATE;
                    DROP TABLE IF EXISTS decision_analysis_attempts;
                    ALTER TABLE decision_analysis_artifacts RENAME TO decision_analysis_artifacts_legacy;
                    ALTER TABLE decision_analysis_runs RENAME TO decision_analysis_runs_legacy;
                    CREATE TABLE decision_analysis_runs (
                        id TEXT PRIMARY KEY,
                        scenario_id TEXT NOT NULL,
                        number INTEGER NOT NULL,
                        plan_version_id TEXT NOT NULL,
                        analysis_type TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        attestation_requirement TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED',
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(scenario_id, number),
                        FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                        FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
                    );
                    INSERT INTO decision_analysis_runs(
                        id, scenario_id, number, plan_version_id, analysis_type, input_hash,
                        payload, created_at, attestation_requirement
                    )
                    SELECT id, scenario_id, number, plan_version_id, analysis_type, input_hash,
                           payload, created_at, attestation_requirement
                    FROM decision_analysis_runs_legacy;
                    DROP TABLE decision_analysis_runs_legacy;
                    CREATE TABLE decision_analysis_artifacts (
                        id TEXT PRIMARY KEY,
                        scenario_id TEXT NOT NULL,
                        analysis_run_id TEXT NOT NULL,
                        option_id TEXT NOT NULL,
                        attestation_requirement TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED',
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(analysis_run_id, option_id),
                        FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                        FOREIGN KEY(analysis_run_id) REFERENCES decision_analysis_runs(id) ON DELETE CASCADE
                    );
                    INSERT INTO decision_analysis_artifacts(
                        id, scenario_id, analysis_run_id, option_id, payload, created_at,
                        attestation_requirement
                    )
                    SELECT id, scenario_id, analysis_run_id, option_id, payload, created_at,
                           attestation_requirement
                    FROM decision_analysis_artifacts_legacy
                    WHERE EXISTS (
                        SELECT 1 FROM decision_analysis_runs r
                        WHERE r.id=decision_analysis_artifacts_legacy.analysis_run_id
                    );
                    DROP TABLE decision_analysis_artifacts_legacy;
                    COMMIT;
                    """
                )
                con.execute("PRAGMA foreign_keys = ON")
            if version < 18:
                for table in ("plan_versions", "decision_analysis_runs", "decision_analysis_artifacts"):
                    table_columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                    if "attestation_requirement" not in table_columns:
                        con.execute(
                            f"ALTER TABLE {table} ADD COLUMN attestation_requirement TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED'"
                        )
                    con.execute(f"UPDATE {table} SET attestation_requirement='LEGACY_MIGRATED'")
                    rows = con.execute(f"SELECT rowid, payload FROM {table}").fetchall()
                    for item in rows:
                        try:
                            payload = json.loads(item["payload"])
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict):
                            payload["attestation_requirement"] = AttestationRequirement.legacy_migrated.value
                            payload["integrity_status"] = AnalysisIntegrityStatus.legacy_unattested.value
                            con.execute(
                                f"UPDATE {table} SET payload=? WHERE rowid=?",
                                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), item["rowid"]),
                            )
            if version < 19:
                for trigger in (
                    "prevent_legacy_plan_insert",
                    "prevent_plan_attestation_change",
                    "prevent_legacy_analysis_insert",
                    "prevent_analysis_attestation_change",
                    "prevent_legacy_artifact_insert",
                    "prevent_artifact_attestation_change",
                    "prevent_analysis_terminal_transition",
                ):
                    con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                run_columns = {row[1] for row in con.execute("PRAGMA table_info(decision_analysis_runs)")}
                run_additions = {
                    "status": "TEXT NOT NULL DEFAULT 'RUNNING'",
                    "started_at": "TEXT NOT NULL DEFAULT ''",
                    "finished_at": "TEXT",
                    "lease_owner": "TEXT",
                    "lease_expires_at": "TEXT",
                    "analysis_manifest_hash": "TEXT",
                }
                for column, definition in run_additions.items():
                    if column not in run_columns:
                        con.execute(f"ALTER TABLE decision_analysis_runs ADD COLUMN {column} {definition}")
                con.execute(
                    """
                    UPDATE decision_analysis_runs
                    SET status=CASE
                            WHEN json_valid(payload) AND json_extract(payload, '$.status') IN ('RUNNING','COMPLETED','FAILED','INTERRUPTED')
                            THEN json_extract(payload, '$.status') ELSE 'INTERRUPTED' END,
                        started_at=COALESCE(NULLIF(started_at, ''), created_at),
                        finished_at=CASE WHEN json_valid(payload) THEN json_extract(payload, '$.finished_at') ELSE finished_at END,
                        analysis_manifest_hash=CASE WHEN json_valid(payload) THEN json_extract(payload, '$.analysis_manifest_hash') ELSE NULL END
                    """
                )
                comparison_columns = {row[1] for row in con.execute("PRAGMA table_info(risk_comparison_runs)")}
                comparison_additions = {
                    "comparison_input_hash": "TEXT NOT NULL DEFAULT ''",
                    "idempotency_key": "TEXT",
                    "request_fingerprint": "TEXT",
                    "attestation_requirement": "TEXT NOT NULL DEFAULT 'LEGACY_MIGRATED'",
                }
                for column, definition in comparison_additions.items():
                    if column not in comparison_columns:
                        con.execute(f"ALTER TABLE risk_comparison_runs ADD COLUMN {column} {definition}")
                # V1 publication manifests did not bind identity, lineage, or Plan artifacts.
                # They remain viewable, but are explicitly non-actionable after this migration.
                for table in ("plan_versions", "decision_analysis_runs", "decision_analysis_artifacts"):
                    con.execute(f"UPDATE {table} SET attestation_requirement='LEGACY_MIGRATED'")
                    rows = con.execute(f"SELECT rowid, payload FROM {table}").fetchall()
                    for item in rows:
                        try:
                            payload = json.loads(item["payload"])
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict):
                            payload["attestation_requirement"] = AttestationRequirement.legacy_migrated.value
                            payload["integrity_status"] = AnalysisIntegrityStatus.legacy_unattested.value
                            payload["self_integrity"] = AnalysisIntegrityStatus.legacy_unattested.value
                            payload["effective_integrity"] = AnalysisIntegrityStatus.legacy_unattested.value
                            con.execute(
                                f"UPDATE {table} SET payload=? WHERE rowid=?",
                                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), item["rowid"]),
                            )
                con.execute("UPDATE risk_comparison_runs SET attestation_requirement='LEGACY_MIGRATED'")
                for item in con.execute("SELECT rowid, payload FROM risk_comparison_runs").fetchall():
                    try:
                        payload = json.loads(item["payload"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict):
                        payload["attestation_requirement"] = AttestationRequirement.legacy_migrated.value
                        payload["integrity_status"] = AnalysisIntegrityStatus.legacy_unattested.value
                        payload["self_integrity"] = AnalysisIntegrityStatus.legacy_unattested.value
                        payload["effective_integrity"] = AnalysisIntegrityStatus.legacy_unattested.value
                        con.execute(
                            "UPDATE risk_comparison_runs SET payload=? WHERE rowid=?",
                            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), item["rowid"]),
                        )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_analysis_attempts (
                    logical_analysis_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    analysis_run_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(logical_analysis_id, attempt_number),
                    FOREIGN KEY(analysis_run_id) REFERENCES decision_analysis_runs(id) ON DELETE CASCADE
                )
                """
            )
            for analysis_row in con.execute("SELECT id, payload FROM decision_analysis_runs").fetchall():
                try:
                    analysis = DecisionAnalysisRun.model_validate_json(analysis_row["payload"])
                except (TypeError, ValueError):
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO decision_analysis_attempts(logical_analysis_id, attempt_number, analysis_run_id) VALUES (?, ?, ?)",
                    (analysis.logical_analysis_id or analysis.id, analysis.attempt_number, analysis.id),
                )
            if version < 7:
                scenario_rows = con.execute(
                    "SELECT DISTINCT scenario_id FROM work_order_execution_events ORDER BY scenario_id"
                ).fetchall()
                for scenario_row in scenario_rows:
                    event_rows = con.execute(
                        "SELECT id, payload FROM work_order_execution_events WHERE scenario_id=? ORDER BY created_at, id",
                        (scenario_row["scenario_id"],),
                    ).fetchall()
                    for sequence, event_row in enumerate(event_rows, start=1):
                        event = WorkOrderExecutionEvent.model_validate_json(event_row["payload"])
                        event.sequence = sequence
                        con.execute(
                            "UPDATE work_order_execution_events SET sequence=?, payload=? WHERE id=?",
                            (sequence, event.model_dump_json(), event.id),
                        )
            if version < 2:
                # Legacy schedules lack a restorable business snapshot. Preserve
                # every raw row before applying the user-selected clean restart.
                for source_table, id_column in (
                    ("schedules", "id"),
                    ("plan_versions", "id"),
                    ("schedule_artifacts", "id"),
                    ("strategy_experiments", "id"),
                    ("publication_keys", "key"),
                ):
                    for legacy_row in con.execute(f"SELECT {id_column} AS source_id, * FROM {source_table}").fetchall():
                        con.execute(
                            "INSERT INTO migration_orphans(source_table, source_id, payload, reason, migrated_at) VALUES (?, ?, ?, ?, ?)",
                            (
                                source_table,
                                str(legacy_row["source_id"]),
                                json.dumps(dict(legacy_row), ensure_ascii=False, sort_keys=True),
                                "v1 history archived before confirmed clean restart",
                                _now(),
                            ),
                        )
                con.execute("DELETE FROM schedules")
                con.execute("DELETE FROM plan_versions")
                con.execute("DELETE FROM schedule_artifacts")
                con.execute("DELETE FROM strategy_experiments")
                con.execute("DELETE FROM publication_keys")
                con.execute("UPDATE scenarios SET active_plan_version_id=NULL")
            if version < 4:
                self._migrate_relational_schema(con)
            if version < 8:
                # Older releases repaired a few legacy defaults only after a
                # scenario was read.  That made GET responses differ from the
                # persisted snapshot and bypassed revision/history semantics.
                # Apply the repair once, transactionally, as an explicit data
                # migration instead.
                scenario_rows = con.execute(
                    "SELECT id, payload, active_plan_version_id FROM scenarios ORDER BY id"
                ).fetchall()
                for scenario_row in scenario_rows:
                    scenario = ScheduleScenario.model_validate_json(scenario_row["payload"])
                    before = scenario.model_dump_json()
                    scenario = self._legacy_upgrade_scenario(scenario)
                    if scenario.model_dump_json() == before:
                        continue
                    latest_revision = con.execute(
                        "SELECT COALESCE(MAX(number), -1) FROM scenario_revisions WHERE scenario_id=?",
                        (scenario.id,),
                    ).fetchone()[0]
                    if int(latest_revision) < 0:
                        original = ScheduleScenario.model_validate_json(before)
                        if original.revision != 0:
                            raise DecisionAnalysisIntegrityError(
                                "旧数据库缺少 D000，不能为已有非零修订伪造链根",
                                record_id=scenario.id,
                                record_type="SCENARIO_REVISION",
                            )
                        self._insert_revision(con, original, "v8 语义升级前快照")
                        latest_revision = 0
                    scenario.revision = max(scenario.revision, int(latest_revision)) + 1
                    active_plan_id = scenario_row["active_plan_version_id"]
                    has_execution = bool(
                        con.execute(
                            "SELECT 1 FROM work_order_execution_events WHERE scenario_id=? LIMIT 1",
                            (scenario.id,),
                        ).fetchone()
                    )
                    if active_plan_id:
                        plan_row = con.execute(
                            "SELECT payload FROM plan_versions WHERE id=?",
                            (active_plan_id,),
                        ).fetchone()
                        if plan_row:
                            plan = PlanVersion.model_validate_json(plan_row["payload"])
                            plan.coverage_status = PlanCoverageStatus.stale_data_changed
                            con.execute(
                                "UPDATE plan_versions SET payload=? WHERE id=?",
                                (plan.model_dump_json(), plan.id),
                            )
                    con.execute(
                        "UPDATE scenarios SET payload=?, active_plan_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (
                            scenario.model_dump_json(),
                            active_plan_id if has_execution else None,
                            scenario.id,
                        ),
                    )
                    self._insert_revision(con, scenario, "v8 旧数据语义升级")
            if version < 10:
                # Monetary values used to be serialized as ambiguous floating
                # units. Preserve their value while making every current and
                # historical scenario snapshot explicit integer cents.
                for table in ("scenarios", "scenario_revisions", "plan_versions", "strategy_experiments"):
                    rows = con.execute(f"SELECT rowid, payload FROM {table}").fetchall()
                    for row in rows:
                        payload = json.loads(row["payload"])
                        if _upgrade_technician_costs(payload):
                            con.execute(
                                f"UPDATE {table} SET payload=? WHERE rowid=?",
                                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["rowid"]),
                            )
                # v22 proof backfill must happen after v10 has normalized every
                # current and historical monetary snapshot. Otherwise its
                # hashes describe the pre-normalization payload.
                self._rehash_migrated_revision_chains(
                    con,
                    reason="v10 migration: invalid revision chain during post-normalization proof backfill",
                )
            if version < 12:
                applicability_columns = {
                    str(row["name"]) for row in con.execute("PRAGMA table_info(plan_applicability)").fetchall()
                }
                if "route_executable" in applicability_columns:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO plan_applicability(
                            plan_version_id, scenario_id, active, coverage_status,
                            route_executable, coverage_complete, planning_current, metrics_current,
                            commercial_current, reoptimization_opportunity, invalid_assignment_ids, updated_at
                        )
                        SELECT p.id,
                               p.scenario_id,
                               CASE WHEN s.active_plan_version_id=p.id THEN 1 ELSE 0 END,
                               COALESCE(json_extract(p.payload, '$.coverage_status'), 'CURRENT_AND_COMPLETE'),
                               1, 1, 1, 1, 1, 0, '[]', CURRENT_TIMESTAMP
                        FROM plan_versions p
                        JOIN scenarios s ON s.id=p.scenario_id
                        """
                    )
                else:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO plan_applicability(
                            plan_version_id, scenario_id, active, coverage_status, updated_at
                        )
                        SELECT p.id,
                               p.scenario_id,
                               CASE WHEN s.active_plan_version_id=p.id THEN 1 ELSE 0 END,
                               COALESCE(json_extract(p.payload, '$.coverage_status'), 'CURRENT_AND_COMPLETE'),
                               CURRENT_TIMESTAMP
                        FROM plan_versions p
                        JOIN scenarios s ON s.id=p.scenario_id
                        """
                    )
            if version < 20:
                existing_columns = {
                    str(row["name"]) for row in con.execute("PRAGMA table_info(plan_applicability)").fetchall()
                }
                applicability_columns = {
                    "route_executable": "INTEGER NOT NULL DEFAULT 1 CHECK(route_executable IN (0, 1))",
                    "coverage_complete": "INTEGER NOT NULL DEFAULT 1 CHECK(coverage_complete IN (0, 1))",
                    "planning_current": "INTEGER NOT NULL DEFAULT 1 CHECK(planning_current IN (0, 1))",
                    "metrics_current": "INTEGER NOT NULL DEFAULT 1 CHECK(metrics_current IN (0, 1))",
                    "commercial_current": "INTEGER NOT NULL DEFAULT 1 CHECK(commercial_current IN (0, 1))",
                    "reoptimization_opportunity": (
                        "INTEGER NOT NULL DEFAULT 0 CHECK(reoptimization_opportunity IN (0, 1))"
                    ),
                    "invalid_assignment_ids": "TEXT NOT NULL DEFAULT '[]'",
                }
                for column, definition in applicability_columns.items():
                    if column not in existing_columns:
                        con.execute(f"ALTER TABLE plan_applicability ADD COLUMN {column} {definition}")
                con.execute(
                    """
                    UPDATE plan_applicability
                    SET route_executable=CASE WHEN coverage_status='STALE_DATA_CHANGED' THEN 0 ELSE 1 END,
                        coverage_complete=CASE WHEN coverage_status='PARTIAL_NEW_DEMAND' THEN 0 ELSE 1 END,
                        planning_current=CASE WHEN coverage_status='CURRENT_AND_COMPLETE' THEN 1 ELSE 0 END,
                        metrics_current=CASE WHEN coverage_status='CURRENT_AND_COMPLETE' THEN 1 ELSE 0 END,
                        commercial_current=1,
                        reoptimization_opportunity=0,
                        invalid_assignment_ids='[]'
                    """
                )
            if version < 21:
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_versions_id_scenario_unique "
                    "ON plan_versions(id, scenario_id)"
                )
                invalid_rows = con.execute(
                    """
                    SELECT a.plan_version_id, a.scenario_id,
                           json_object('coverage_status', a.coverage_status,
                                       'invalid_assignment_ids', a.invalid_assignment_ids) AS payload
                    FROM plan_applicability a
                    LEFT JOIN plan_versions p ON p.id=a.plan_version_id AND p.scenario_id=a.scenario_id
                    WHERE p.id IS NULL
                       OR a.coverage_status NOT IN ('CURRENT_AND_COMPLETE','PARTIAL_NEW_DEMAND','STALE_DATA_CHANGED')
                       OR CASE WHEN json_valid(a.invalid_assignment_ids)
                               THEN json_type(a.invalid_assignment_ids)<>'array'
                               ELSE 1 END
                    """
                ).fetchall()
                for invalid in invalid_rows:
                    self._record_read_isolation(
                        con,
                        "plan_applicability",
                        str(invalid["plan_version_id"]),
                        str(invalid["payload"]),
                        "v21 applicability constraint repair",
                    )
                con.commit()
                con.execute("PRAGMA foreign_keys = OFF")
                con.executescript(
                    """
                    BEGIN;
                    DROP TRIGGER IF EXISTS sync_active_plan_insert;
                    DROP TRIGGER IF EXISTS sync_active_plan_update;
                    DROP TRIGGER IF EXISTS sync_active_plan_delete;
                    CREATE TABLE plan_applicability_v21 (
                        plan_version_id TEXT PRIMARY KEY,
                        scenario_id TEXT NOT NULL,
                        active INTEGER NOT NULL CHECK(active IN (0, 1)),
                        coverage_status TEXT NOT NULL CHECK(coverage_status IN ('CURRENT_AND_COMPLETE', 'PARTIAL_NEW_DEMAND', 'STALE_DATA_CHANGED')),
                        route_executable INTEGER NOT NULL CHECK(route_executable IN (0, 1)),
                        coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0, 1)),
                        planning_current INTEGER NOT NULL CHECK(planning_current IN (0, 1)),
                        metrics_current INTEGER NOT NULL CHECK(metrics_current IN (0, 1)),
                        commercial_current INTEGER NOT NULL CHECK(commercial_current IN (0, 1)),
                        reoptimization_opportunity INTEGER NOT NULL CHECK(reoptimization_opportunity IN (0, 1)),
                        invalid_assignment_ids TEXT NOT NULL CHECK(json_valid(invalid_assignment_ids) AND json_type(invalid_assignment_ids)='array'),
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(plan_version_id, scenario_id) REFERENCES plan_versions(id, scenario_id) ON DELETE CASCADE,
                        FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                    );
                    INSERT INTO plan_applicability_v21
                    SELECT a.plan_version_id,
                           p.scenario_id,
                           a.active,
                           CASE WHEN CASE WHEN json_valid(a.invalid_assignment_ids)
                                               THEN json_type(a.invalid_assignment_ids)<>'array'
                                               ELSE 1 END
                                THEN 'STALE_DATA_CHANGED'
                                WHEN a.coverage_complete=0 THEN 'PARTIAL_NEW_DEMAND'
                                WHEN a.route_executable=0 OR a.planning_current=0 OR a.metrics_current=0 OR a.commercial_current=0
                                THEN 'STALE_DATA_CHANGED'
                                ELSE 'CURRENT_AND_COMPLETE' END,
                           CASE WHEN CASE WHEN json_valid(a.invalid_assignment_ids)
                                               THEN json_type(a.invalid_assignment_ids)='array'
                                               ELSE 0 END
                                     AND a.route_executable IN (0,1)
                                THEN a.route_executable ELSE 0 END,
                           CASE WHEN a.coverage_complete IN (0,1) THEN a.coverage_complete ELSE 0 END,
                           CASE WHEN a.planning_current IN (0,1) THEN a.planning_current ELSE 0 END,
                           CASE WHEN a.metrics_current IN (0,1) THEN a.metrics_current ELSE 0 END,
                           CASE WHEN a.commercial_current IN (0,1) THEN a.commercial_current ELSE 0 END,
                           CASE WHEN a.reoptimization_opportunity IN (0,1) THEN a.reoptimization_opportunity ELSE 0 END,
                           CASE WHEN json_valid(a.invalid_assignment_ids)
                                THEN CASE WHEN json_type(a.invalid_assignment_ids)='array'
                                          THEN a.invalid_assignment_ids ELSE '[]' END
                                ELSE '[]' END,
                           a.updated_at
                    FROM plan_applicability a
                    JOIN plan_versions p ON p.id=a.plan_version_id AND p.scenario_id=a.scenario_id;
                    DROP TABLE plan_applicability;
                    ALTER TABLE plan_applicability_v21 RENAME TO plan_applicability;
                    COMMIT;
                    """
                )
                con.execute("PRAGMA foreign_keys = ON")
                violations = con.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(f"foreign key check failed after v21 migration: {violations[:3]}")
            # Databases older than v21 must first rebuild and sanitize the
            # applicability table; only then is it safe to calculate proofs.
            if version < 21:
                scenario_columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(scenarios)")}
                for column, definition in {
                    "current_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                    "latest_revision_number": "INTEGER NOT NULL DEFAULT -1",
                    "latest_revision_hash": "TEXT NOT NULL DEFAULT ''",
                    "proof_origin": (
                        "TEXT NOT NULL DEFAULT 'MIGRATION_BACKFILLED' "
                        "CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED'))"
                    ),
                }.items():
                    if column not in scenario_columns:
                        con.execute(f"ALTER TABLE scenarios ADD COLUMN {column} {definition}")
                revision_columns = {str(row["name"]) for row in con.execute("PRAGMA table_info(scenario_revisions)")}
                for column, definition in {
                    "proof_origin": (
                        "TEXT NOT NULL DEFAULT 'MIGRATION_BACKFILLED' "
                        "CHECK(proof_origin IN ('NATIVE_ATTESTED', 'MIGRATION_BACKFILLED', 'LEGACY_UNATTESTED'))"
                    ),
                    "chain_status": (
                        "TEXT NOT NULL DEFAULT 'VERIFIED' "
                        "CHECK(chain_status IN ('VERIFIED', 'ROOT_INVALID', 'GAP_DETECTED', 'ANCESTOR_INVALID'))"
                    ),
                }.items():
                    if column not in revision_columns:
                        con.execute(f"ALTER TABLE scenario_revisions ADD COLUMN {column} {definition}")
                con.execute("UPDATE scenario_revisions SET proof_origin='MIGRATION_BACKFILLED'")
                for scenario_row in con.execute("SELECT id, payload FROM scenarios ORDER BY id").fetchall():
                    scenario_id = str(scenario_row["id"])
                    previous_hash: str | None = None
                    expected_number = 0
                    ancestor_invalid = False
                    latest_verified: ScenarioRevision | None = None
                    for revision_row in con.execute(
                        "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number, created_at, id",
                        (scenario_id,),
                    ).fetchall():
                        status = RevisionChainStatus.verified
                        revision: ScenarioRevision | None = None
                        try:
                            revision = ScenarioRevision.model_validate_json(revision_row["payload"])
                            relation_valid = (
                                revision.id == revision_row["id"]
                                and revision.scenario_id == revision_row["scenario_id"]
                                and revision.number == revision_row["number"]
                                and revision.reason == revision_row["reason"]
                                and revision.created_at == revision_row["created_at"]
                                and revision.scenario.id == revision.scenario_id
                                and revision.scenario.revision == revision.number
                                and revision.scenario_snapshot_hash == content_hash(revision.scenario)
                                and revision.revision_hash == _scenario_revision_hash(revision)
                                and revision.previous_revision_hash == previous_hash
                            )
                            if ancestor_invalid:
                                status = RevisionChainStatus.ancestor_invalid
                            elif revision.number != expected_number:
                                status = (
                                    RevisionChainStatus.root_invalid
                                    if expected_number == 0
                                    else RevisionChainStatus.gap_detected
                                )
                            elif not relation_valid:
                                status = (
                                    RevisionChainStatus.root_invalid
                                    if expected_number == 0
                                    else RevisionChainStatus.ancestor_invalid
                                )
                        except (TypeError, ValueError):
                            status = (
                                RevisionChainStatus.root_invalid
                                if expected_number == 0
                                else RevisionChainStatus.ancestor_invalid
                            )
                        if status is RevisionChainStatus.verified and revision is not None:
                            revision.proof_origin = RevisionProofOrigin.migration_backfilled
                            revision.chain_status = status
                            con.execute(
                                "UPDATE scenario_revisions SET payload=?, proof_origin=?, chain_status=? WHERE id=?",
                                (
                                    revision.model_dump_json(),
                                    revision.proof_origin.value,
                                    status.value,
                                    revision.id,
                                ),
                            )
                            latest_verified = revision
                            previous_hash = revision.revision_hash
                            expected_number += 1
                        else:
                            ancestor_invalid = True
                            con.execute(
                                "UPDATE scenario_revisions SET proof_origin='MIGRATION_BACKFILLED', chain_status=? WHERE id=?",
                                (status.value, revision_row["id"]),
                            )
                            self._record_read_isolation(
                                con,
                                "scenario_revisions",
                                str(revision_row["id"]),
                                str(revision_row["payload"]),
                                f"v23 migration: {status.value.lower()}",
                            )
                    if latest_verified is None:
                        con.execute(
                            "UPDATE scenarios SET current_snapshot_hash='', latest_revision_number=-1, "
                            "latest_revision_hash='', proof_origin='MIGRATION_BACKFILLED' WHERE id=?",
                            (scenario_id,),
                        )
                    else:
                        con.execute(
                            "UPDATE scenarios SET current_snapshot_hash=?, latest_revision_number=?, "
                            "latest_revision_hash=?, proof_origin='MIGRATION_BACKFILLED' WHERE id=?",
                            (
                                latest_verified.scenario_snapshot_hash,
                                latest_verified.number,
                                latest_verified.revision_hash,
                                scenario_id,
                            ),
                        )
                applicability_columns = {
                    str(row["name"]) for row in con.execute("PRAGMA table_info(plan_applicability)")
                }
                for column, definition in {
                    "evaluated_scenario_revision": "INTEGER",
                    "evaluated_scenario_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
                    "reducer_policy_version": ("TEXT NOT NULL DEFAULT 'FIELD_SERVICE_PLAN_APPLICABILITY_V2'"),
                    "projection_hash": "TEXT NOT NULL DEFAULT ''",
                }.items():
                    if column not in applicability_columns:
                        con.execute(f"ALTER TABLE plan_applicability ADD COLUMN {column} {definition}")
                for applicability_row in con.execute("SELECT * FROM plan_applicability").fetchall():
                    head = con.execute(
                        "SELECT latest_revision_number, current_snapshot_hash FROM scenarios WHERE id=?",
                        (applicability_row["scenario_id"],),
                    ).fetchone()
                    applicability = PlanApplicability(
                        route_executable=bool(applicability_row["route_executable"]),
                        coverage_complete=bool(applicability_row["coverage_complete"]),
                        planning_current=bool(applicability_row["planning_current"]),
                        metrics_current=bool(applicability_row["metrics_current"]),
                        commercial_current=bool(applicability_row["commercial_current"]),
                        reoptimization_opportunity=bool(applicability_row["reoptimization_opportunity"]),
                        invalid_assignment_ids=json.loads(applicability_row["invalid_assignment_ids"]),
                        evaluated_scenario_revision=(int(head["latest_revision_number"]) if head else None),
                        evaluated_scenario_snapshot_hash=(str(head["current_snapshot_hash"]) if head else ""),
                    )
                    applicability.projection_hash = _plan_applicability_hash(
                        str(applicability_row["plan_version_id"]),
                        str(applicability_row["scenario_id"]),
                        applicability,
                    )
                    con.execute(
                        "UPDATE plan_applicability SET evaluated_scenario_revision=?, "
                        "evaluated_scenario_snapshot_hash=?, reducer_policy_version=?, projection_hash=? "
                        "WHERE plan_version_id=?",
                        (
                            applicability.evaluated_scenario_revision,
                            applicability.evaluated_scenario_snapshot_hash,
                            applicability.reducer_policy_version,
                            applicability.projection_hash,
                            applicability_row["plan_version_id"],
                        ),
                    )
            con.execute(
                """
                INSERT OR IGNORE INTO plan_metadata(plan_version_id, label, note, tags, updated_at)
                SELECT id,
                       CASE WHEN json_valid(payload)
                            THEN COALESCE(json_extract(payload, '$.label'), '未命名方案')
                            ELSE '损坏的方案记录' END,
                       '', '[]', created_at
                FROM plan_versions
                """
            )
            for quarantine_table in (
                "plan_versions",
                "decision_analysis_runs",
                "decision_analysis_artifacts",
                "schedule_runs",
                "strategy_experiments",
            ):
                malformed_rows = con.execute(
                    f"SELECT id, payload FROM {quarantine_table} WHERE NOT json_valid(payload)"
                ).fetchall()
                for malformed in malformed_rows:
                    already_recorded = con.execute(
                        "SELECT 1 FROM migration_orphans WHERE source_table=? AND source_id=? AND reason='malformed JSON'",
                        (quarantine_table, malformed["id"]),
                    ).fetchone()
                    if not already_recorded:
                        con.execute(
                            "INSERT INTO migration_orphans(source_table, source_id, payload, reason, migrated_at) VALUES (?, ?, ?, 'malformed JSON', ?)",
                            (quarantine_table, malformed["id"], malformed["payload"], _now()),
                        )
            if version < SCHEMA_VERSION:
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            duplicate_run = con.execute(
                "SELECT run_id, COUNT(*) AS count FROM schedule_candidates GROUP BY run_id HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate_run:
                raise sqlite3.IntegrityError(
                    f"schedule run {duplicate_run['run_id']} has {duplicate_run['count']} candidates; manual repair required"
                )
            con.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_schedules_scenario ON schedules(scenario_id, version);
                CREATE INDEX IF NOT EXISTS idx_plan_versions_scenario ON plan_versions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_plan_applicability_scenario ON plan_applicability(scenario_id, active);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_applicability_active_unique ON plan_applicability(scenario_id) WHERE active=1;
                CREATE INDEX IF NOT EXISTS idx_decision_analysis_plan ON decision_analysis_runs(plan_version_id, number);
                CREATE INDEX IF NOT EXISTS idx_decision_analysis_artifacts_run ON decision_analysis_artifacts(analysis_run_id, option_id);
                CREATE INDEX IF NOT EXISTS idx_risk_comparisons_scenario ON risk_comparison_runs(scenario_id, number);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_comparisons_idempotency ON risk_comparison_runs(scenario_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_revisions_scenario ON scenario_revisions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_plan ON schedule_artifacts(plan_version_id);
                CREATE INDEX IF NOT EXISTS idx_runs_scenario ON schedule_runs(scenario_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_run ON schedule_candidates(run_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_run_unique ON schedule_candidates(run_id);
                CREATE INDEX IF NOT EXISTS idx_planning_reservations_scenario ON planning_reservations(scenario_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_commands_status ON command_keys(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_execution_events_sequence ON work_order_execution_events(scenario_id, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_events_sequence_unique ON work_order_execution_events(scenario_id, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_events_action_unique ON work_order_execution_events(scenario_id, work_order_id, action);
                CREATE TRIGGER IF NOT EXISTS prevent_legacy_plan_insert
                BEFORE INSERT ON plan_versions
                WHEN NEW.attestation_requirement='LEGACY_MIGRATED'
                BEGIN SELECT RAISE(ABORT, 'LEGACY_MIGRATED is migration-only'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_plan_attestation_change
                BEFORE UPDATE OF attestation_requirement ON plan_versions
                WHEN OLD.attestation_requirement<>NEW.attestation_requirement
                BEGIN SELECT RAISE(ABORT, 'attestation requirement is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_legacy_analysis_insert
                BEFORE INSERT ON decision_analysis_runs
                WHEN NEW.attestation_requirement='LEGACY_MIGRATED'
                BEGIN SELECT RAISE(ABORT, 'LEGACY_MIGRATED is migration-only'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_analysis_attestation_change
                BEFORE UPDATE OF attestation_requirement ON decision_analysis_runs
                WHEN OLD.attestation_requirement<>NEW.attestation_requirement
                BEGIN SELECT RAISE(ABORT, 'attestation requirement is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_analysis_terminal_transition
                BEFORE UPDATE OF status ON decision_analysis_runs
                WHEN OLD.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED') AND OLD.status<>NEW.status
                BEGIN SELECT RAISE(ABORT, 'terminal analysis status is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_legacy_artifact_insert
                BEFORE INSERT ON decision_analysis_artifacts
                WHEN NEW.attestation_requirement='LEGACY_MIGRATED'
                BEGIN SELECT RAISE(ABORT, 'LEGACY_MIGRATED is migration-only'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_artifact_attestation_change
                BEFORE UPDATE OF attestation_requirement ON decision_analysis_artifacts
                WHEN OLD.attestation_requirement<>NEW.attestation_requirement
                BEGIN SELECT RAISE(ABORT, 'attestation requirement is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_legacy_risk_comparison_insert
                BEFORE INSERT ON risk_comparison_runs
                WHEN NEW.attestation_requirement='LEGACY_MIGRATED'
                BEGIN SELECT RAISE(ABORT, 'LEGACY_MIGRATED is migration-only'); END;
                CREATE TRIGGER IF NOT EXISTS prevent_risk_comparison_attestation_change
                BEFORE UPDATE OF attestation_requirement ON risk_comparison_runs
                WHEN OLD.attestation_requirement<>NEW.attestation_requirement
                BEGIN SELECT RAISE(ABORT, 'attestation requirement is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS sync_active_plan_insert
                AFTER INSERT ON plan_applicability WHEN NEW.active=1
                BEGIN UPDATE scenarios SET active_plan_version_id=NEW.plan_version_id WHERE id=NEW.scenario_id; END;
                CREATE TRIGGER IF NOT EXISTS sync_active_plan_update
                AFTER UPDATE OF active ON plan_applicability
                BEGIN
                    UPDATE scenarios
                    SET active_plan_version_id=(
                        SELECT plan_version_id FROM plan_applicability
                        WHERE scenario_id=NEW.scenario_id AND active=1 LIMIT 1
                    )
                    WHERE id=NEW.scenario_id;
                END;
                CREATE TRIGGER IF NOT EXISTS sync_active_plan_delete
                AFTER DELETE ON plan_applicability
                BEGIN
                    UPDATE scenarios
                    SET active_plan_version_id=(
                        SELECT plan_version_id FROM plan_applicability
                        WHERE scenario_id=OLD.scenario_id AND active=1 LIMIT 1
                    )
                    WHERE id=OLD.scenario_id;
                END;
                """
            )
            con.execute(
                """
                UPDATE scenarios
                SET active_plan_version_id=(
                    SELECT plan_version_id FROM plan_applicability
                    WHERE scenario_id=scenarios.id AND active=1 LIMIT 1
                )
                """
            )
            con.execute(
                "UPDATE strategy_experiments SET payload=json_set(payload, '$.status', 'INTERRUPTED', '$.error', '应用重启，实验已中断') WHERE json_valid(payload) AND json_extract(payload, '$.status') IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')"
            )
            interrupted = con.execute(
                "SELECT payload FROM schedule_runs WHERE status IN ('QUEUED', 'RUNNING')"
            ).fetchall()
            for row in interrupted:
                try:
                    run = ScheduleRun.model_validate_json(row["payload"])
                except (TypeError, ValueError):
                    continue
                run.status = ScheduleRunStatus.failed
                run.termination_reason = "APPLICATION_RESTARTED"
                run.finished_at = _now()
                con.execute(
                    "UPDATE schedule_runs SET status=?, payload=? WHERE id=?",
                    (run.status.value, run.model_dump_json(), run.id),
                )
            interrupted_analyses = con.execute(
                "SELECT id, payload, attestation_requirement FROM decision_analysis_runs"
            ).fetchall()
            for row in interrupted_analyses:
                try:
                    analysis = DecisionAnalysisRun.model_validate_json(row["payload"])
                except (TypeError, ValueError):
                    continue
                if analysis.status != "RUNNING":
                    continue
                analysis.status = "INTERRUPTED"
                analysis.error = {
                    "code": "APPLICATION_RESTARTED",
                    "message": "应用在经营分析完成前重启",
                }
                analysis.finished_at = _now()
                analysis.attestation_requirement = AttestationRequirement(row["attestation_requirement"])
                if analysis.attestation_requirement is AttestationRequirement.required:
                    analysis.result = None
                    analysis.result_hash = None
                    analysis.result_manifest = None
                    analysis.failure_manifest = AnalysisFailureManifest(
                        status="INTERRUPTED",
                        error_hash=content_hash(analysis.error),
                        error_code="APPLICATION_RESTARTED",
                        failure_stage="APPLICATION_RESTART",
                        finished_at=analysis.finished_at,
                    )
                    analysis.analysis_manifest_hash = content_hash(_analysis_manifest_payload(analysis))
                    analysis.integrity_status = AnalysisIntegrityStatus.verified
                con.execute(
                    "UPDATE decision_analysis_runs SET status='INTERRUPTED', finished_at=?, analysis_manifest_hash=?, payload=? WHERE id=? AND status='RUNNING'",
                    (
                        analysis.finished_at,
                        analysis.analysis_manifest_hash,
                        analysis.model_dump_json(),
                        analysis.id,
                    ),
                )
            abandoned_commands = con.execute(
                """
                SELECT namespace, key, publication_key, payload
                FROM command_keys
                WHERE status IN ('RUNNING', 'REPLAN_RUNNING')
                   OR (status IN ('RESERVED', 'ANALYSIS_RESERVED') AND namespace LIKE '%:analysis-%')
                   OR (status='INTAKE_COMMITTED' AND publication_key IS NOT NULL)
                """
            ).fetchall()
            for command in abandoned_commands:
                publication = con.execute(
                    "SELECT plan_version_id FROM publication_keys WHERE key=?",
                    (command["publication_key"] or command["key"],),
                ).fetchone()
                if publication:
                    con.execute(
                        "UPDATE command_keys SET status='COMPLETED', resource_type='plan_version', resource_id=?, payload=?, updated_at=? WHERE namespace=? AND key=?",
                        (
                            publication["plan_version_id"],
                            json.dumps(
                                {"plan_version_id": publication["plan_version_id"], "reconciled": True},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            _now(),
                            command["namespace"],
                            command["key"],
                        ),
                    )
                else:
                    con.execute(
                        "UPDATE command_keys SET status='FAILED_RETRYABLE', payload=?, updated_at=? WHERE namespace=? AND key=?",
                        (
                            json.dumps(
                                {
                                    "message": "应用在命令完成前重启；相同幂等键可以重新执行",
                                    "reconciled": True,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            _now(),
                            command["namespace"],
                            command["key"],
                        ),
                    )
        self.seed_fixtures()
        self.seed_profiles()
        self._scan_revision_integrity()
        self._scan_execution_status_integrity()

    def seed_fixtures(self) -> None:
        with self._lock, self._connect() as con:
            for scenario in all_fixtures().values():
                con.execute(
                    "INSERT OR IGNORE INTO scenarios(id, payload) VALUES (?, ?)",
                    (scenario.id, scenario.model_dump_json()),
                )
                if int(con.execute("SELECT changes()").fetchone()[0]) == 1:
                    self._insert_revision(con, scenario, "内置场景初始化")

    def seed_profiles(self) -> None:
        with self._lock, self._connect() as con:
            for profile in BUILTIN_PROFILES:
                con.execute(
                    "INSERT INTO strategy_profiles(id, builtin, payload, created_at) VALUES (?, 1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, builtin=1",
                    (profile.id, profile.model_dump_json(), profile.created_at),
                )

    def _scan_execution_status_integrity(self) -> None:
        """Record legacy WorkOrder states that cannot be derived from immutable events."""
        with self._lock, self._connect() as con:
            for scenario_row in con.execute("SELECT * FROM scenarios").fetchall():
                try:
                    scenario = self._load_scenario_head_row(con, scenario_row)
                except DecisionAnalysisIntegrityError:
                    continue
                event_actions: set[tuple[str, str]] = set()
                for event_row in con.execute(
                    "SELECT * FROM work_order_execution_events WHERE scenario_id=? ORDER BY sequence",
                    (scenario.id,),
                ).fetchall():
                    try:
                        event = self._load_execution_event_row(con, event_row)
                        event_actions.add((event.work_order_id, event.action))
                    except DecisionAnalysisIntegrityError:
                        self._record_read_isolation(
                            con,
                            "work_order_execution_events",
                            str(event_row["id"]),
                            str(event_row["payload"]),
                            "startup integrity scan: invalid execution event proof",
                        )
                for order in scenario.work_orders:
                    missing: list[str] = []
                    if (
                        order.status in {WorkOrderStatus.started, WorkOrderStatus.completed}
                        and (
                            order.id,
                            "start",
                        )
                        not in event_actions
                    ):
                        missing.append("start")
                    if order.status is WorkOrderStatus.completed and (order.id, "complete") not in event_actions:
                        missing.append("complete")
                    if not missing:
                        continue
                    reason = f"execution status {order.status.value} missing events: {','.join(missing)}"
                    self._record_read_isolation(
                        con,
                        "scenario_work_order_status",
                        f"{scenario.id}:{order.id}",
                        json.dumps(
                            {"scenario_id": scenario.id, "work_order_id": order.id, "status": order.status.value},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        reason,
                    )

    def _scan_revision_integrity(self) -> None:
        """Record broken revision proofs without preventing unrelated scenarios from opening."""
        with self._lock, self._connect() as con:
            for scenario_row in con.execute("SELECT * FROM scenarios ORDER BY id").fetchall():
                scenario_id = str(scenario_row["id"])
                previous_hash: str | None = None
                expected_number = 0
                ancestor_invalid = False
                for row in con.execute(
                    "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number",
                    (scenario_id,),
                ).fetchall():
                    status = RevisionChainStatus.verified
                    try:
                        if ancestor_invalid:
                            status = RevisionChainStatus.ancestor_invalid
                            raise ValueError("ancestor invalid")
                        if int(row["number"]) != expected_number:
                            status = (
                                RevisionChainStatus.root_invalid
                                if expected_number == 0
                                else RevisionChainStatus.gap_detected
                            )
                            raise ValueError("revision number discontinuity")
                        revision = self._load_revision_row(
                            con,
                            row,
                            previous_hash,
                            verify_chain_status=False,
                        )
                    except (DecisionAnalysisIntegrityError, ValueError):
                        if status is RevisionChainStatus.verified:
                            status = (
                                RevisionChainStatus.root_invalid
                                if expected_number == 0
                                else RevisionChainStatus.ancestor_invalid
                            )
                        con.execute(
                            "UPDATE scenario_revisions SET chain_status=? WHERE id=?",
                            (status.value, row["id"]),
                        )
                        self._record_read_isolation(
                            con,
                            "scenario_revisions",
                            str(row["id"]),
                            str(row["payload"]),
                            "startup integrity scan: invalid revision proof",
                        )
                        ancestor_invalid = True
                        continue
                    con.execute(
                        "UPDATE scenario_revisions SET chain_status='VERIFIED' WHERE id=?",
                        (row["id"],),
                    )
                    previous_hash = revision.revision_hash
                    expected_number += 1
                try:
                    self._load_scenario_head_row(con, scenario_row)
                except DecisionAnalysisIntegrityError:
                    self._record_read_isolation(
                        con,
                        "scenarios",
                        scenario_id,
                        str(scenario_row["payload"]),
                        "startup integrity scan: scenario head mismatch",
                    )

    @classmethod
    def _insert_revision(
        cls, con: sqlite3.Connection, scenario: ScheduleScenario, reason: str, ignore: bool = False
    ) -> ScenarioRevision:
        scenario_row = con.execute("SELECT * FROM scenarios WHERE id=?", (scenario.id,)).fetchone()
        if not scenario_row:
            raise DecisionAnalysisIntegrityError(
                "业务数据聚合不存在，不能写入修订",
                record_id=scenario.id,
                record_type="SCENARIO",
            )
        # During a pre-v4 relational rebuild the v23 head/proof columns do not
        # exist yet. Preserve the same D000/continuous-chain invariant using
        # the revision rows; the v23 migration then materializes the O(1) head.
        if "latest_revision_number" not in scenario_row.keys():
            previous_hash: str | None = None
            head_number = -1
            existing_at_number: ScenarioRevision | None = None
            for row in con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number, created_at, id",
                (scenario.id,),
            ).fetchall():
                revision = ScenarioRevision.model_validate_json(row["payload"])
                if (
                    revision.id != row["id"]
                    or revision.scenario_id != row["scenario_id"]
                    or revision.number != row["number"]
                    or revision.reason != row["reason"]
                    or revision.created_at != row["created_at"]
                    or revision.scenario.id != scenario.id
                    or revision.scenario.revision != revision.number
                    or revision.number != head_number + 1
                    or revision.scenario_snapshot_hash != content_hash(revision.scenario)
                    or revision.previous_revision_hash != previous_hash
                    or revision.revision_hash != _scenario_revision_hash(revision)
                ):
                    raise DecisionAnalysisIntegrityError(
                        "旧数据库业务修订链不连续或证明不一致",
                        record_id=str(row["id"]),
                        record_type="SCENARIO_REVISION",
                    )
                head_number = revision.number
                previous_hash = revision.revision_hash
                if revision.number == scenario.revision:
                    existing_at_number = revision
            if ignore and existing_at_number:
                if existing_at_number.scenario_snapshot_hash != content_hash(scenario):
                    raise DecisionAnalysisIntegrityError(
                        "业务数据聚合与已有修订不一致",
                        record_id=scenario.id,
                        record_type="SCENARIO_REVISION",
                    )
                return existing_at_number
            if scenario.revision != head_number + 1:
                raise DecisionAnalysisIntegrityError(
                    "业务数据修订号必须从 D000 开始并连续递增",
                    record_id=scenario.id,
                    record_type="SCENARIO_REVISION",
                )
            revision = ScenarioRevision(
                id=f"REV-{scenario.id}-{scenario.revision}-{uuid.uuid4().hex[:6]}",
                scenario_id=scenario.id,
                number=scenario.revision,
                reason=reason,
                scenario=scenario.model_copy(deep=True),
                created_at=_now(),
                scenario_snapshot_hash=content_hash(scenario),
                previous_revision_hash=previous_hash,
                proof_origin=RevisionProofOrigin.migration_backfilled,
                chain_status=RevisionChainStatus.verified,
            )
            revision.revision_hash = _scenario_revision_hash(revision)
            verb = "INSERT OR IGNORE" if ignore else "INSERT"
            con.execute(
                f"{verb} INTO scenario_revisions(id, scenario_id, number, reason, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    revision.id,
                    revision.scenario_id,
                    revision.number,
                    reason,
                    revision.model_dump_json(),
                    revision.created_at,
                ),
            )
            return revision
        if ignore:
            existing_row = con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? AND number=?",
                (scenario.id, scenario.revision),
            ).fetchone()
            if existing_row:
                existing = cls._load_revision_row(con, existing_row, None, verify_predecessor=False)
                if (
                    int(scenario_row["latest_revision_number"]) != existing.number
                    or str(scenario_row["latest_revision_hash"]) != existing.revision_hash
                    or str(scenario_row["current_snapshot_hash"]) != existing.scenario_snapshot_hash
                    or content_hash(scenario) != existing.scenario_snapshot_hash
                ):
                    raise DecisionAnalysisIntegrityError(
                        "业务数据聚合与已有修订链头不一致",
                        record_id=scenario.id,
                        record_type="SCENARIO",
                    )
                return existing
        head_number = int(scenario_row["latest_revision_number"])
        head_hash = str(scenario_row["latest_revision_hash"])
        if scenario.revision != head_number + 1:
            raise DecisionAnalysisIntegrityError(
                "业务数据修订号必须从 D000 开始并连续递增",
                record_id=scenario.id,
                record_type="SCENARIO_REVISION",
            )
        previous_hash: str | None = None
        if head_number >= 0:
            previous_row = con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? AND number=?",
                (scenario.id, head_number),
            ).fetchone()
            if not previous_row:
                raise DecisionAnalysisIntegrityError(
                    "业务数据修订链头记录缺失",
                    record_id=scenario.id,
                    record_type="SCENARIO_REVISION",
                )
            previous = cls._load_revision_row(con, previous_row, None, verify_predecessor=False)
            if previous.revision_hash != head_hash:
                raise DecisionAnalysisIntegrityError(
                    "业务数据修订链头哈希不一致",
                    record_id=previous.id,
                    record_type="SCENARIO_REVISION",
                )
            previous_hash = previous.revision_hash
        revision = ScenarioRevision(
            id=f"REV-{scenario.id}-{scenario.revision}-{uuid.uuid4().hex[:6]}",
            scenario_id=scenario.id,
            number=scenario.revision,
            reason=reason,
            scenario=scenario.model_copy(deep=True),
            created_at=_now(),
            scenario_snapshot_hash=content_hash(scenario),
            previous_revision_hash=previous_hash,
            proof_origin=RevisionProofOrigin.native_attested,
            chain_status=RevisionChainStatus.verified,
        )
        revision.revision_hash = _scenario_revision_hash(revision)
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        con.execute(
            f"{verb} INTO scenario_revisions(id, scenario_id, number, reason, payload, proof_origin, chain_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.id,
                revision.scenario_id,
                revision.number,
                reason,
                revision.model_dump_json(),
                revision.proof_origin.value,
                revision.chain_status.value,
                revision.created_at,
            ),
        )
        con.execute(
            "UPDATE scenarios SET current_snapshot_hash=?, latest_revision_number=?, latest_revision_hash=?, "
            "proof_origin=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                revision.scenario_snapshot_hash,
                revision.number,
                revision.revision_hash,
                revision.proof_origin.value,
                scenario.id,
            ),
        )
        return revision

    @classmethod
    def _load_revision_row(
        cls,
        con: sqlite3.Connection,
        row: sqlite3.Row,
        expected_previous_hash: str | None,
        *,
        verify_predecessor: bool = True,
        verify_chain_status: bool = True,
    ) -> ScenarioRevision:
        del cls, con
        try:
            revision = ScenarioRevision.model_validate_json(row["payload"])
            row_keys = set(row.keys())
            proof_origin = RevisionProofOrigin(
                row["proof_origin"] if "proof_origin" in row_keys else RevisionProofOrigin.migration_backfilled
            )
            chain_status = RevisionChainStatus(
                row["chain_status"] if "chain_status" in row_keys else RevisionChainStatus.verified
            )
            relation_valid = (
                revision.id == row["id"]
                and revision.scenario_id == row["scenario_id"]
                and revision.number == row["number"]
                and revision.reason == row["reason"]
                and revision.created_at == row["created_at"]
            )
            chain_valid = not verify_predecessor or revision.previous_revision_hash == expected_previous_hash
            if (
                not relation_valid
                or not chain_valid
                or (verify_chain_status and chain_status is not RevisionChainStatus.verified)
                or revision.scenario.id != revision.scenario_id
                or revision.scenario.revision != revision.number
                or revision.scenario_snapshot_hash != content_hash(revision.scenario)
                or revision.revision_hash != _scenario_revision_hash(revision)
            ):
                raise ValueError("scenario revision proof mismatch")
            revision.proof_origin = proof_origin
            revision.chain_status = chain_status
            revision.self_integrity = AnalysisIntegrityStatus.verified
            revision.effective_integrity = AnalysisIntegrityStatus.verified
            return revision
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionAnalysisIntegrityError(
                "业务数据修订记录完整性校验失败",
                record_id=str(row["id"]),
                record_type="SCENARIO_REVISION",
            ) from error

    @classmethod
    def _load_scenario_head_row(cls, con: sqlite3.Connection, row: sqlite3.Row) -> ScheduleScenario:
        try:
            scenario = ScheduleScenario.model_validate_json(row["payload"])
            if scenario.id != str(row["id"]):
                raise ValueError("scenario relational identity mismatch")
            head_number = int(row["latest_revision_number"])
            head_hash = str(row["latest_revision_hash"])
            snapshot_hash = str(row["current_snapshot_hash"])
            if head_number < 0 or not head_hash or not snapshot_hash:
                raise ValueError("scenario revision head missing")
            latest_row = con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number DESC LIMIT 1",
                (scenario.id,),
            ).fetchone()
            if not latest_row or int(latest_row["number"]) != head_number:
                raise ValueError("scenario latest revision mismatch")
            latest = cls._load_revision_row(con, latest_row, None, verify_predecessor=False)
            if (
                scenario.revision != head_number
                or content_hash(scenario) != snapshot_hash
                or latest.revision_hash != head_hash
                or latest.scenario_snapshot_hash != snapshot_hash
                or content_hash(latest.scenario) != snapshot_hash
            ):
                raise ValueError("scenario head proof mismatch")
            return scenario
        except DecisionAnalysisIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionAnalysisIntegrityError(
                "当前业务数据与修订链头不一致",
                record_id=str(row["id"]),
                record_type="SCENARIO",
                code="SCENARIO_HEAD_INTEGRITY_FAILED",
                details={
                    "current_revision": (
                        int(row["latest_revision_number"]) if "latest_revision_number" in set(row.keys()) else None
                    )
                },
            ) from error

    @staticmethod
    def _set_plan_applicability(
        con: sqlite3.Connection,
        plan_version_id: str,
        scenario_id: str,
        *,
        active: bool | None = None,
        coverage_status: PlanCoverageStatus | None = None,
        applicability: PlanApplicability | None = None,
        evaluated_scenario: ScheduleScenario | None = None,
    ) -> None:
        existing = con.execute(
            "SELECT * FROM plan_applicability WHERE plan_version_id=?",
            (plan_version_id,),
        ).fetchone()
        resolved_active = int(active if active is not None else bool(existing and existing["active"]))
        if applicability is None and existing:
            try:
                applicability = PlanApplicability(
                    route_executable=bool(existing["route_executable"]),
                    coverage_complete=bool(existing["coverage_complete"]),
                    planning_current=bool(existing["planning_current"]),
                    metrics_current=bool(existing["metrics_current"]),
                    commercial_current=bool(existing["commercial_current"]),
                    reoptimization_opportunity=bool(existing["reoptimization_opportunity"]),
                    invalid_assignment_ids=json.loads(existing["invalid_assignment_ids"]),
                    evaluated_scenario_revision=existing["evaluated_scenario_revision"],
                    evaluated_scenario_snapshot_hash=str(existing["evaluated_scenario_snapshot_hash"]),
                    reducer_policy_version=str(existing["reducer_policy_version"]),
                    projection_hash=str(existing["projection_hash"]),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                applicability = applicability_from_legacy_status(PlanCoverageStatus.stale_data_changed)
        if applicability is None:
            applicability = applicability_from_legacy_status(coverage_status or PlanCoverageStatus.current_and_complete)
        elif coverage_status is not None:
            legacy_projection = applicability_from_legacy_status(coverage_status)
            if coverage_status is PlanCoverageStatus.partial_new_demand:
                applicability.coverage_complete = False
                applicability.planning_current = False
                applicability.metrics_current = False
            elif coverage_status is PlanCoverageStatus.stale_data_changed:
                applicability.planning_current = False
                applicability.metrics_current = False
                if not existing:
                    applicability.route_executable = legacy_projection.route_executable
        if evaluated_scenario is None:
            scenario_row = con.execute("SELECT payload FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not scenario_row:
                raise DecisionAnalysisIntegrityError(
                    "方案适用性引用的当前业务数据不存在",
                    record_id=plan_version_id,
                    record_type="PLAN_APPLICABILITY",
                )
            evaluated_scenario = ScheduleScenario.model_validate_json(scenario_row["payload"])
        applicability.evaluated_scenario_revision = evaluated_scenario.revision
        applicability.evaluated_scenario_snapshot_hash = content_hash(evaluated_scenario)
        applicability.reducer_policy_version = "FIELD_SERVICE_PLAN_APPLICABILITY_V2"
        applicability.projection_hash = _plan_applicability_hash(plan_version_id, scenario_id, applicability)
        resolved_coverage = coverage_status_from_applicability(applicability).value
        con.execute(
            """
            INSERT INTO plan_applicability(
                plan_version_id, scenario_id, active, coverage_status,
                route_executable, coverage_complete, planning_current, metrics_current,
                commercial_current, reoptimization_opportunity, invalid_assignment_ids,
                evaluated_scenario_revision, evaluated_scenario_snapshot_hash,
                reducer_policy_version, projection_hash, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_version_id) DO UPDATE SET
                active=excluded.active,
                coverage_status=excluded.coverage_status,
                route_executable=excluded.route_executable,
                coverage_complete=excluded.coverage_complete,
                planning_current=excluded.planning_current,
                metrics_current=excluded.metrics_current,
                commercial_current=excluded.commercial_current,
                reoptimization_opportunity=excluded.reoptimization_opportunity,
                invalid_assignment_ids=excluded.invalid_assignment_ids,
                evaluated_scenario_revision=excluded.evaluated_scenario_revision,
                evaluated_scenario_snapshot_hash=excluded.evaluated_scenario_snapshot_hash,
                reducer_policy_version=excluded.reducer_policy_version,
                projection_hash=excluded.projection_hash,
                updated_at=excluded.updated_at
            """,
            (
                plan_version_id,
                scenario_id,
                resolved_active,
                resolved_coverage,
                int(applicability.route_executable),
                int(applicability.coverage_complete),
                int(applicability.planning_current),
                int(applicability.metrics_current),
                int(applicability.commercial_current),
                int(applicability.reoptimization_opportunity),
                json.dumps(applicability.invalid_assignment_ids, ensure_ascii=False),
                applicability.evaluated_scenario_revision,
                applicability.evaluated_scenario_snapshot_hash,
                applicability.reducer_policy_version,
                applicability.projection_hash,
                _now(),
            ),
        )

    @staticmethod
    def _overlay_plan_applicability(con: sqlite3.Connection, plan: PlanVersion) -> PlanVersion:
        row = con.execute(
            "SELECT * FROM plan_applicability WHERE plan_version_id=?",
            (plan.id,),
        ).fetchone()
        if row:
            try:
                if str(row["scenario_id"]) != plan.scenario_id:
                    raise ValueError("applicability scenario mismatch")
                plan.active = bool(row["active"])
                plan.applicability = PlanApplicability(
                    route_executable=bool(row["route_executable"]),
                    coverage_complete=bool(row["coverage_complete"]),
                    planning_current=bool(row["planning_current"]),
                    metrics_current=bool(row["metrics_current"]),
                    commercial_current=bool(row["commercial_current"]),
                    reoptimization_opportunity=bool(row["reoptimization_opportunity"]),
                    invalid_assignment_ids=json.loads(row["invalid_assignment_ids"]),
                    evaluated_scenario_revision=row["evaluated_scenario_revision"],
                    evaluated_scenario_snapshot_hash=str(row["evaluated_scenario_snapshot_hash"]),
                    reducer_policy_version=str(row["reducer_policy_version"]),
                    projection_hash=str(row["projection_hash"]),
                )
                if plan.applicability.projection_hash != _plan_applicability_hash(
                    plan.id, plan.scenario_id, plan.applicability
                ):
                    raise ValueError("applicability projection hash mismatch")
                if plan.active:
                    scenario_row = con.execute(
                        "SELECT current_snapshot_hash, latest_revision_number FROM scenarios WHERE id=?",
                        (plan.scenario_id,),
                    ).fetchone()
                    if (
                        not scenario_row
                        or plan.applicability.evaluated_scenario_revision != int(scenario_row["latest_revision_number"])
                        or plan.applicability.evaluated_scenario_snapshot_hash
                        != str(scenario_row["current_snapshot_hash"])
                    ):
                        raise ValueError("active applicability evaluated against stale scenario")
                # coverage_status is a compatibility projection for older SQL
                # readers. The multi-axis applicability record is authoritative.
                plan.coverage_status = coverage_status_from_applicability(plan.applicability)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise DecisionAnalysisIntegrityError(
                    "方案适用性记录无法解析",
                    record_id=plan.id,
                    record_type="PLAN_APPLICABILITY",
                ) from error
        else:
            active = con.execute(
                "SELECT active_plan_version_id FROM scenarios WHERE id=?",
                (plan.scenario_id,),
            ).fetchone()
            plan.active = bool(active and active["active_plan_version_id"] == plan.id)
        return plan

    @staticmethod
    def _overlay_plan_attestation(
        plan: PlanVersion,
        requirement: str,
    ) -> PlanVersion:
        plan.attestation_requirement = AttestationRequirement(requirement)
        if plan.attestation_requirement is AttestationRequirement.legacy_migrated:
            plan.integrity_status = AnalysisIntegrityStatus.legacy_unattested
            plan.self_integrity = AnalysisIntegrityStatus.legacy_unattested
            plan.effective_integrity = AnalysisIntegrityStatus.legacy_unattested
            return plan
        artifact = plan.publication_verification_artifact
        required = (
            plan.scenario_snapshot is not None,
            bool(plan.scenario_snapshot_hash),
            bool(plan.published_schedule_hash),
            artifact is not None,
            bool(plan.publication_verification_report_hash),
            bool(plan.publication_manifest_hash),
        )
        if not all(required):
            plan.integrity_status = AnalysisIntegrityStatus.failed
            plan.self_integrity = AnalysisIntegrityStatus.failed
            plan.effective_integrity = AnalysisIntegrityStatus.failed
            return plan
        assert plan.scenario_snapshot is not None and artifact is not None
        context_hash = None
        if plan.publication_planning_context is not None:
            context_hash = content_hash(
                plan.publication_planning_context.model_dump(exclude={"context_fingerprint"}, mode="json")
            )
            if (
                context_hash != plan.publication_planning_context.context_fingerprint
                or context_hash != plan.publication_planning_context_hash
            ):
                plan.integrity_status = AnalysisIntegrityStatus.failed
                plan.self_integrity = AnalysisIntegrityStatus.failed
                plan.effective_integrity = AnalysisIntegrityStatus.failed
                return plan
        artifact_hash = content_hash(artifact.model_dump(exclude={"artifact_hash"}, mode="json"))
        expected_manifest = content_hash(build_plan_manifest_payload(plan))
        plan.integrity_status = (
            AnalysisIntegrityStatus.verified
            if plan.publication_manifest_version == "FIELD_SERVICE_PUBLICATION_MANIFEST_V2"
            and plan.scenario_snapshot_hash == content_hash(plan.scenario_snapshot)
            and plan.published_schedule_hash == content_hash(plan.selected)
            and artifact_hash == artifact.artifact_hash
            and artifact.verified_schedule_hash == plan.published_schedule_hash
            and content_hash(artifact.transaction_verification_report) == plan.publication_verification_report_hash
            and expected_manifest == plan.publication_manifest_hash
            else AnalysisIntegrityStatus.failed
        )
        plan.self_integrity = plan.integrity_status
        plan.effective_integrity = plan.integrity_status
        return plan

    @classmethod
    def _load_plan_row(cls, con: sqlite3.Connection, row: sqlite3.Row) -> PlanVersion:
        try:
            plan = PlanVersion.model_validate_json(row["payload"])
        except (ValueError, TypeError) as error:
            raise DecisionAnalysisIntegrityError(
                "方案记录无法解析",
                record_id=str(row["id"]),
                record_type="PLAN_VERSION",
            ) from error
        row_keys = set(row.keys())
        relational_mismatch = (
            plan.id != str(row["id"])
            or ("scenario_id" in row_keys and plan.scenario_id != str(row["scenario_id"]))
            or ("number" in row_keys and plan.number != int(row["number"]))
            or ("created_at" in row_keys and plan.created_at != str(row["created_at"]))
        )
        if relational_mismatch:
            plan.integrity_status = AnalysisIntegrityStatus.failed
            plan.self_integrity = AnalysisIntegrityStatus.failed
            plan.effective_integrity = AnalysisIntegrityStatus.failed
            return plan
        plan = cls._overlay_plan_attestation(plan, row["attestation_requirement"])
        metadata = con.execute(
            "SELECT label FROM plan_metadata WHERE plan_version_id=?",
            (plan.id,),
        ).fetchone()
        if metadata:
            plan.label = metadata["label"]
        return cls._overlay_plan_applicability(con, plan)

    @staticmethod
    def _require_loaded_plan_for_use(plan: PlanVersion, use_case: PlanUseCase) -> PlanVersion:
        if use_case is PlanUseCase.audit_view:
            return plan
        if plan.effective_integrity is AnalysisIntegrityStatus.legacy_unattested:
            raise PublicationConflict(
                "该历史方案未达到当前发布证明标准，请先重新验证为新版本",
                code="PLAN_REATTESTATION_REQUIRED",
                details={"plan_version_id": plan.id, "use_case": use_case.value},
            )
        if plan.effective_integrity is not AnalysisIntegrityStatus.verified:
            raise PublicationConflict(
                "方案发布证明校验失败，不能用于该业务操作",
                code="PLAN_INTEGRITY_FAILED",
                details={"plan_version_id": plan.id, "use_case": use_case.value},
            )
        return plan

    def list_scenarios(self) -> list[ScheduleScenario]:
        scenarios, _warnings = self.list_scenarios_with_warnings()
        return scenarios

    @staticmethod
    def _record_read_isolation(
        con: sqlite3.Connection,
        source_table: str,
        source_id: str,
        payload: str,
        reason: str,
    ) -> None:
        """Copy an unreadable row to the quarantine ledger without deleting evidence."""
        recorded = con.execute(
            "SELECT 1 FROM migration_orphans WHERE source_table=? AND source_id=? AND reason=?",
            (source_table, source_id, reason),
        ).fetchone()
        if not recorded:
            con.execute(
                """
                INSERT INTO migration_orphans(source_table, source_id, payload, reason, migrated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_table, source_id, payload, reason, _now()),
            )

    def list_scenarios_with_warnings(self) -> tuple[list[ScheduleScenario], list[dict[str, str]]]:
        scenarios: list[ScheduleScenario] = []
        warnings: list[dict[str, str]] = []
        with self._lock, self._connect() as con:
            rows = con.execute("SELECT * FROM scenarios ORDER BY id").fetchall()
            for row in rows:
                try:
                    scenarios.append(self._load_scenario_head_row(con, row))
                except DecisionAnalysisIntegrityError as error:
                    warning = {
                        "record_type": "SCENARIO",
                        "record_id": str(row["id"]),
                        "message": "场景记录无法解析，已从列表隔离",
                    }
                    warnings.append(warning)
                    self._record_read_isolation(
                        con,
                        "scenarios",
                        str(row["id"]),
                        str(row["payload"]),
                        f"read isolation: {type(error).__name__}",
                    )
        return scenarios, warnings

    def list_integrity_issues(self) -> list[dict[str, str]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT source_table, source_id, reason, migrated_at
                FROM migration_orphans
                ORDER BY migrated_at DESC, id DESC
                """
            ).fetchall()
        return [
            {
                "source_table": str(row["source_table"]),
                "source_id": str(row["source_id"]),
                "reason": str(row["reason"]),
                "recorded_at": str(row["migrated_at"]),
            }
            for row in rows
        ]

    def get_scenario(self, scenario_id: str) -> ScheduleScenario | None:
        malformed: DecisionAnalysisIntegrityError | None = None
        scenario: ScheduleScenario | None = None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT * FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
            if row:
                try:
                    scenario = self._load_scenario_head_row(con, row)
                except DecisionAnalysisIntegrityError as error:
                    self._record_read_isolation(
                        con,
                        "scenarios",
                        str(row["id"]),
                        str(row["payload"]),
                        "read isolation: malformed scenario payload",
                    )
                    malformed = error
        if malformed:
            raise malformed
        return scenario

    @staticmethod
    def _legacy_upgrade_scenario(scenario: ScheduleScenario) -> ScheduleScenario:
        config = scenario.solver_config
        if (config.travel_weight, config.sla_late_weight, config.overtime_weight, config.imbalance_weight) in {
            (1, 8, 4, 2),
            (4, 12, 8, 1),
            (4, 12, 12, 1),
        }:
            config.travel_weight, config.sla_late_weight, config.overtime_weight, config.imbalance_weight = (
                4,
                12,
                30,
                1,
            )
        for order in scenario.work_orders:
            if order.id.startswith("WO-EMG-") and not order.is_emergency:
                order.is_emergency = True
                order.reported_at = order.reported_at or min(order.window_start, 600)
                order.drop_penalty = max(order.drop_penalty, 8000)
        return scenario

    def save_scenario(
        self,
        scenario: ScheduleScenario,
        reason: str = "业务数据更新",
        *,
        expected_revision: int | None = None,
        preserve_active_plan: bool = False,
        mark_plan_stale: bool = True,
        plan_coverage_status: PlanCoverageStatus | None = None,
        plan_applicability: PlanApplicability | None = None,
        change_impact: FieldImpact | None = None,
        invalid_assignment_ids: list[str] | None = None,
        expected_active_plan_id: str | None = None,
        check_active_plan: bool = False,
    ) -> None:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM scenarios WHERE id=?",
                (scenario.id,),
            ).fetchone()
            current_scenario = self._load_scenario_head_row(con, row) if row else None
            current_revision = current_scenario.revision if current_scenario else -1
            if expected_revision is None:
                if row:
                    raise ScenarioRevisionConflict(-1, current_revision)
                con.execute(
                    "INSERT INTO scenarios(id, payload, active_plan_version_id, updated_at) VALUES (?, ?, NULL, CURRENT_TIMESTAMP)",
                    (scenario.id, scenario.model_dump_json()),
                )
            else:
                if not row or current_revision != expected_revision:
                    raise ScenarioRevisionConflict(expected_revision, current_revision)
                current_active_plan_id = row["active_plan_version_id"]
                if check_active_plan and current_active_plan_id != expected_active_plan_id:
                    raise ActivePlanConflict(expected_active_plan_id, current_active_plan_id)
                if change_impact is not None and current_active_plan_id:
                    plan_row = con.execute(
                        "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                        "FROM plan_versions WHERE id=? AND scenario_id=?",
                        (current_active_plan_id, scenario.id),
                    ).fetchone()
                    if not plan_row:
                        raise ActivePlanConflict(expected_active_plan_id, current_active_plan_id)
                    active_plan = self._load_plan_row(con, plan_row)
                    assert current_scenario is not None
                    previous_scenario = current_scenario
                    plan_applicability = reduce_plan_applicability(
                        active_plan,
                        previous_scenario,
                        scenario,
                        active_plan.applicability,
                        change_impact,
                        invalid_assignment_ids,
                    )
                    plan_coverage_status = coverage_status_from_applicability(plan_applicability)
                # A data edit makes the selected plan stale but preserves all
                # published history. Replanning can still use the latest plan.
                if preserve_active_plan:
                    active = con.execute(
                        "SELECT active_plan_version_id FROM scenarios WHERE id=?",
                        (scenario.id,),
                    ).fetchone()
                    if active and active["active_plan_version_id"]:
                        self._set_plan_applicability(
                            con,
                            active["active_plan_version_id"],
                            scenario.id,
                            active=True,
                            coverage_status=(
                                plan_coverage_status
                                or (PlanCoverageStatus.stale_data_changed if mark_plan_stale else None)
                            ),
                            applicability=plan_applicability,
                            evaluated_scenario=scenario,
                        )
                    con.execute(
                        "UPDATE scenarios SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (scenario.model_dump_json(), scenario.id),
                    )
                else:
                    active = con.execute(
                        "SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario.id,)
                    ).fetchone()
                    if active and active["active_plan_version_id"]:
                        self._set_plan_applicability(
                            con,
                            active["active_plan_version_id"],
                            scenario.id,
                            active=False,
                            coverage_status=PlanCoverageStatus.stale_data_changed,
                            applicability=plan_applicability,
                            evaluated_scenario=scenario,
                        )
                    con.execute(
                        "UPDATE scenarios SET payload=?, active_plan_version_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (scenario.model_dump_json(), scenario.id),
                    )
            self._insert_revision(con, scenario, reason)

    def get_command_record(self, namespace: str, key: str, fingerprint: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM command_keys WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        if not row:
            return None
        if row["request_fingerprint"] != fingerprint:
            raise PublicationConflict("相同幂等键对应了不同请求")
        return {
            "namespace": row["namespace"],
            "key": row["key"],
            "status": row["status"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "publication_key": row["publication_key"],
            "payload": json.loads(row["payload"]),
        }

    def intake_emergency_work_order(
        self,
        scenario_id: str,
        order: WorkOrder,
        *,
        namespace: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> tuple[ScheduleScenario, bool]:
        """Persist reported demand before any solver work and keep the last plan visible."""
        if order.status is not WorkOrderStatus.pending:
            raise PublicationConflict(
                "新建突发工单只能处于待处理状态",
                code="WORK_ORDER_STATUS_EVENT_REQUIRED",
            )
        if not order.is_emergency or order.reported_at is None:
            raise PublicationConflict("突发工单必须标记为紧急并包含接报时间")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            command = con.execute(
                "SELECT request_fingerprint FROM command_keys WHERE namespace=? AND key=?",
                (namespace, idempotency_key),
            ).fetchone()
            if command and command["request_fingerprint"] != request_fingerprint:
                raise PublicationConflict("相同幂等键对应了不同请求")
            scenario_row = con.execute(
                "SELECT * FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not scenario_row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = self._load_scenario_head_row(con, scenario_row)
            if command:
                return scenario, False
            existing = next((item for item in scenario.work_orders if item.id == order.id), None)
            if existing and existing.model_dump(mode="json") != order.model_dump(mode="json"):
                raise PublicationConflict(f"工单 {order.id} 已存在，但内容与本次请求不同")
            created = existing is None
            if created:
                previous_scenario = scenario.model_copy(deep=True)
                active_plan_id = scenario_row["active_plan_version_id"]
                active_plan: PlanVersion | None = None
                if active_plan_id:
                    plan_row = con.execute(
                        "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                        "FROM plan_versions WHERE id=? AND scenario_id=?",
                        (active_plan_id, scenario.id),
                    ).fetchone()
                    if not plan_row:
                        raise ActivePlanConflict(active_plan_id, active_plan_id)
                    # The persisted applicability still proves the old head at
                    # this point. Load it before advancing D, then rebind the
                    # projection to the new head below.
                    active_plan = self._load_plan_row(con, plan_row)
                expected_revision = scenario.revision
                scenario.work_orders.append(order.model_copy(deep=True))
                scenario.revision += 1
                scenario = ScheduleScenario.model_validate(scenario.model_dump())
                con.execute(
                    "UPDATE scenarios SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND json_extract(payload, '$.revision')=?",
                    (scenario.model_dump_json(), scenario_id, expected_revision),
                )
                if con.execute("SELECT changes()").fetchone()[0] != 1:
                    current = con.execute(
                        "SELECT json_extract(payload, '$.revision') FROM scenarios WHERE id=?", (scenario_id,)
                    ).fetchone()
                    raise ScenarioRevisionConflict(expected_revision, int(current[0]) if current else -1)
                self._insert_revision(con, scenario, f"接收突发工单 {order.id}")
                if active_plan_id and active_plan:
                    applicability = reduce_plan_applicability(
                        active_plan,
                        previous_scenario,
                        scenario,
                        active_plan.applicability,
                        FieldImpact.new_demand,
                    )
                    self._set_plan_applicability(
                        con,
                        active_plan_id,
                        scenario.id,
                        active=True,
                        applicability=applicability,
                        evaluated_scenario=scenario,
                    )
            now = _now()
            payload = json.dumps(
                {"work_order_id": order.id, "scenario_revision": scenario.revision},
                ensure_ascii=False,
                sort_keys=True,
            )
            con.execute(
                """
                INSERT INTO command_keys(namespace, key, request_fingerprint, status, resource_type, resource_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, 'INTAKE_COMMITTED', 'work_order', ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO NOTHING
                """,
                (namespace, idempotency_key, request_fingerprint, order.id, payload, now, now),
            )
            return scenario, created

    def update_command_record(
        self,
        namespace: str,
        key: str,
        fingerprint: str,
        *,
        status: str,
        resource_type: str | None,
        resource_id: str | None,
        payload: dict,
        publication_key: str | None = None,
    ) -> None:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT request_fingerprint, status FROM command_keys WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
            if not row:
                raise PublicationConflict("幂等命令记录不存在")
            if row["request_fingerprint"] != fingerprint:
                raise PublicationConflict("相同幂等键对应了不同请求")
            terminal_statuses = {"COMPLETED", "FAILED", "FAILED_AFTER_LOCK", "FAILED_CONTEXT_CHANGED"}
            if row["status"] in terminal_statuses:
                if row["status"] == status:
                    return
                raise PublicationConflict("幂等命令已进入终态，不能覆盖")
            con.execute(
                "UPDATE command_keys SET status=?, resource_type=?, resource_id=?, publication_key=COALESCE(?, publication_key), payload=?, updated_at=? WHERE namespace=? AND key=?",
                (
                    status,
                    resource_type,
                    resource_id,
                    publication_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    _now(),
                    namespace,
                    key,
                ),
            )

    def begin_command_record(
        self,
        namespace: str,
        key: str,
        fingerprint: str,
        *,
        status: str,
        resource_type: str | None,
        resource_id: str | None,
        payload: dict,
        publication_key: str | None = None,
    ) -> bool:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT request_fingerprint, status FROM command_keys WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
            if row:
                if row["request_fingerprint"] != fingerprint:
                    raise PublicationConflict("相同幂等键对应了不同请求")
                if row["status"] == "FAILED_RETRYABLE":
                    con.execute(
                        "UPDATE command_keys SET status=?, resource_type=?, resource_id=?, publication_key=COALESCE(?, publication_key), payload=?, updated_at=? WHERE namespace=? AND key=?",
                        (
                            status,
                            resource_type,
                            resource_id,
                            publication_key,
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            _now(),
                            namespace,
                            key,
                        ),
                    )
                    return True
                return False
            now = _now()
            con.execute(
                """
                INSERT INTO command_keys(namespace, key, request_fingerprint, status, resource_type, resource_id, publication_key, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    key,
                    fingerprint,
                    status,
                    resource_type,
                    resource_id,
                    publication_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            return True

    def transition_work_order(
        self,
        scenario_id: str,
        work_order_id: str,
        action: Literal["start", "complete"],
        request: WorkOrderExecutionRequest,
        *,
        request_fingerprint: str,
    ) -> WorkOrderExecutionResult:
        if action not in {"start", "complete"}:
            raise ValueError(f"unsupported execution action: {action}")
        namespace = f"work-order-{action}:{scenario_id}:{work_order_id}"
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            command = con.execute(
                "SELECT request_fingerprint, resource_type, resource_id FROM command_keys WHERE namespace=? AND key=?",
                (namespace, request.idempotency_key),
            ).fetchone()
            if command:
                if command["request_fingerprint"] != request_fingerprint:
                    raise PublicationConflict("相同幂等键对应了不同执行请求")
                if command["resource_type"] != "execution_event" or not command["resource_id"]:
                    raise PublicationConflict(
                        "执行命令重放缺少可信事件引用",
                        code="EXECUTION_REPLAY_RESOURCE_MISSING",
                    )
                event_row = con.execute(
                    "SELECT * FROM work_order_execution_events WHERE id=?",
                    (command["resource_id"],),
                ).fetchone()
                if not event_row:
                    raise PublicationConflict(
                        "执行命令引用的事件不存在",
                        code="EXECUTION_REPLAY_EVENT_MISSING",
                        details={"event_id": str(command["resource_id"])},
                    )
                stored_event = self._load_execution_event_row(con, event_row)
                if (
                    stored_event.scenario_id != scenario_id
                    or stored_event.work_order_id != work_order_id
                    or stored_event.action != action
                    or stored_event.idempotency_key != request.idempotency_key
                ):
                    raise PublicationConflict(
                        "执行命令引用了不匹配的事件",
                        code="EXECUTION_REPLAY_IDENTITY_MISMATCH",
                    )
                scenario_row = con.execute(
                    "SELECT * FROM scenarios WHERE id=?",
                    (scenario_id,),
                ).fetchone()
                if not scenario_row:
                    raise KeyError(f"场景 {scenario_id} 不存在")
                current_scenario = self._load_scenario_head_row(con, scenario_row)
                return WorkOrderExecutionResult(
                    scenario=current_scenario,
                    event=stored_event,
                )

            scenario_row = con.execute(
                "SELECT * FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not scenario_row:
                raise KeyError(f"场景 {scenario_id} 不存在")
            scenario = self._load_scenario_head_row(con, scenario_row)
            if scenario.revision != request.expected_revision:
                raise ScenarioRevisionConflict(request.expected_revision, scenario.revision)
            order = next((item for item in scenario.work_orders if item.id == work_order_id), None)
            if not order:
                raise KeyError(f"工单 {work_order_id} 不存在")
            plan_id = scenario_row["active_plan_version_id"]
            if not plan_id:
                raise PublicationConflict("当前没有可执行方案，请先生成并发布方案")
            plan_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=?",
                (plan_id,),
            ).fetchone()
            if not plan_row:
                raise PublicationConflict("当前方案记录不存在，请刷新后重试")
            plan = self._load_plan_row(con, plan_row)
            self._require_loaded_plan_for_use(plan, PlanUseCase.execute)
            if work_order_id in plan.applicability.invalid_assignment_ids:
                raise PublicationConflict(
                    "该工单的发布分配已因业务数据变化失效，请先局部重排",
                    code="INVALID_ASSIGNMENT_CANNOT_START",
                    details={"work_order_id": work_order_id, "plan_version_id": plan.id},
                )
            assignment = next((item for item in plan.selected.assignments if item.work_order_id == work_order_id), None)
            if not assignment:
                raise PublicationConflict("该工单未在当前方案中分配，不能登记执行状态")
            if assignment.technician_id != request.technician_id:
                raise PublicationConflict("执行技师与当前方案分配不一致")
            if request.occurred_at < (order.reported_at or 0):
                raise PublicationConflict("执行时间不能早于工单接报时间")

            expected_status = WorkOrderStatus.pending if action == "start" else WorkOrderStatus.started
            target_status = WorkOrderStatus.started if action == "start" else WorkOrderStatus.completed
            start_event: WorkOrderExecutionEvent | None = None
            if order.status is not expected_status:
                raise PublicationConflict(f"工单当前为 {order.status.value}，不能执行 {action} 操作")
            if action == "start":
                snapshot_fingerprint = assignment.planning_fingerprint
                if not snapshot_fingerprint and plan.scenario_snapshot:
                    snapshot_fingerprint = assignment_planning_fingerprint(
                        plan.scenario_snapshot,
                        assignment,
                        self.travel_provider,
                    )
                current_fingerprint = assignment_planning_fingerprint(
                    scenario,
                    assignment,
                    self.travel_provider,
                )
                if not snapshot_fingerprint or snapshot_fingerprint != current_fingerprint:
                    raise PublicationConflict(
                        "该待处理工单的规划条件已变化，请先局部重排",
                        code="PENDING_ASSIGNMENT_STALE",
                        details={"work_order_id": work_order_id, "plan_version_id": plan.id},
                    )
                ready_at = service_ready_at(order)
                if request.occurred_at < ready_at and not request.early_start_override_reason:
                    raise PublicationConflict(
                        f"开始时间早于客户允许时间 {ready_at}；如客户已同意，请填写提前开始原因",
                        code="EARLY_START_OVERRIDE_REQUIRED",
                        details={"earliest_customer_time": ready_at},
                    )
                assignments_by_order = {item.work_order_id: item for item in plan.selected.assignments}
                for active_order in scenario.work_orders:
                    if active_order.status is not WorkOrderStatus.started:
                        continue
                    active_assignment = assignments_by_order.get(active_order.id)
                    if active_assignment and active_assignment.technician_id == request.technician_id:
                        raise PublicationConflict(f"技师 {request.technician_id} 已有服务中的工单 {active_order.id}")
                route = sorted(
                    [item for item in plan.selected.assignments if item.technician_id == request.technician_id],
                    key=lambda item: item.sequence,
                )
                predecessors = [item for item in route if item.sequence < assignment.sequence]
                scenario_orders = {item.id: item for item in scenario.work_orders}
                missing_predecessors = [
                    item.work_order_id for item in predecessors if item.work_order_id not in scenario_orders
                ]
                if missing_predecessors:
                    raise PublicationConflict(
                        "当前执行路线引用了已删除的前序工单，请先重新排程",
                        code="STALE_ROUTE_PREDECESSOR_MISSING",
                        details={"missing_work_order_ids": missing_predecessors},
                    )
                incomplete = [
                    item.work_order_id
                    for item in predecessors
                    if scenario_orders[item.work_order_id].status is not WorkOrderStatus.completed
                ]
                if incomplete:
                    raise PublicationConflict(f"路线前序工单尚未完成：{', '.join(incomplete)}")
                if predecessors:
                    predecessor = predecessors[-1]
                    predecessor_order = scenario_orders[predecessor.work_order_id]
                    completion_row = con.execute(
                        """
                        SELECT * FROM work_order_execution_events
                        WHERE scenario_id=? AND work_order_id=? AND action='complete'
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        (scenario_id, predecessor.work_order_id),
                    ).fetchone()
                    if not completion_row:
                        raise PublicationConflict(f"路线前序工单 {predecessor.work_order_id} 缺少完成事件")
                    completion = self._load_execution_event_row(con, completion_row)
                    earliest_start = completion.occurred_at + self.travel_provider.minutes(
                        predecessor_order.location,
                        order.location,
                    )
                else:
                    technician = next(item for item in scenario.technicians if item.id == request.technician_id)
                    execution_context = self._execution_source_context(con, scenario, plan.id)
                    projection = next(
                        (
                            item
                            for item in execution_context.technician_projections
                            if item.technician_id == request.technician_id and item.state == "completed"
                        ),
                        None,
                    )
                    origin = projection.effective_location if projection else technician.start_location
                    available_at = projection.available_at if projection else technician.shift_start
                    earliest_start = available_at + self.travel_provider.minutes(origin, order.location)
                if request.occurred_at < earliest_start:
                    raise PublicationConflict(
                        f"开始时间早于实际位置和行程允许的最早时间 {earliest_start}",
                        code="BEFORE_EXECUTION_AVAILABILITY",
                        details={"earliest_start": earliest_start},
                    )
            if action == "complete":
                start_row = con.execute(
                    "SELECT * FROM work_order_execution_events WHERE scenario_id=? AND work_order_id=? AND action='start' ORDER BY occurred_at DESC LIMIT 1",
                    (scenario_id, work_order_id),
                ).fetchone()
                if not start_row:
                    raise PublicationConflict("找不到该工单的开始服务记录")
                try:
                    start_event = self._load_execution_event_row(con, start_row)
                except DecisionAnalysisIntegrityError as error:
                    raise PublicationConflict(
                        "开始服务记录完整性校验失败，不能继续完成工单",
                        code="EXECUTION_EVENT_INTEGRITY_FAILED",
                        details={"event_id": str(start_row["id"])},
                    ) from error
                if start_event.technician_id != request.technician_id:
                    raise PublicationConflict("完成服务的技师与开始服务记录不一致")
                if request.occurred_at <= start_event.occurred_at:
                    raise PublicationConflict(
                        "完成时间必须严格晚于开始时间",
                        code="ZERO_OR_NEGATIVE_ACTUAL_DURATION",
                        details={"actual_start_at": start_event.occurred_at},
                    )

            order.status = target_status
            if target_status is WorkOrderStatus.completed:
                scenario.locked_assignments = [
                    item for item in scenario.locked_assignments if item.work_order_id != work_order_id
                ]
            scenario.revision += 1
            sequence_row = con.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM work_order_execution_events WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()
            start_event_for_identity = start_event if action == "complete" else None
            source_assignment_hash = (
                start_event_for_identity.source_assignment_hash
                if start_event_for_identity
                else assignment.source_assignment_hash or assignment_source_fingerprint(assignment)
            )
            source_sequence = (
                start_event_for_identity.source_sequence
                if start_event_for_identity
                else assignment.source_sequence or assignment.sequence
            )
            event = WorkOrderExecutionEvent(
                id=f"EXEC-{uuid.uuid4().hex[:12]}",
                scenario_id=scenario_id,
                work_order_id=work_order_id,
                technician_id=request.technician_id,
                action=action,
                sequence=int(sequence_row["next_sequence"]),
                occurred_at=request.occurred_at,
                scenario_revision=scenario.revision,
                plan_version_id=plan.id,
                idempotency_key=request.idempotency_key,
                created_at=_now(),
                booking_id=(
                    start_event_for_identity.booking_id
                    if start_event_for_identity and start_event_for_identity.booking_id
                    else f"BOOK-{uuid.uuid4().hex[:12]}"
                ),
                source_assignment_hash=source_assignment_hash,
                source_sequence=source_sequence,
                planned_start_at=(
                    start_event_for_identity.planned_start_at if start_event_for_identity else assignment.start_time
                ),
                planned_finish_at=(
                    start_event_for_identity.planned_finish_at if start_event_for_identity else assignment.finish_time
                ),
                actual_duration_minutes=(
                    request.occurred_at - start_event_for_identity.occurred_at if start_event_for_identity else None
                ),
                customer_window_late_start_minutes=(
                    max(0, request.occurred_at - order.window_end) if action == "start" else 0
                ),
                planned_start_variance_minutes=(
                    request.occurred_at - assignment.start_time if action == "start" else None
                ),
                actual_late_start_minutes=(max(0, request.occurred_at - order.window_end) if action == "start" else 0),
                early_start_override_reason=(request.early_start_override_reason if action == "start" else None),
                estimated_remaining_minutes=(request.estimated_remaining_minutes if action == "start" else None),
                note=request.note,
            )
            event.event_content_hash = _execution_event_content_hash(event)
            result = WorkOrderExecutionResult(scenario=scenario, event=event)
            con.execute(
                "UPDATE scenarios SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (scenario.model_dump_json(), scenario_id),
            )
            self._set_plan_applicability(
                con,
                plan.id,
                scenario_id,
                active=True,
                applicability=plan.applicability,
                evaluated_scenario=scenario,
            )
            self._insert_revision(
                con, scenario, f"工单 {work_order_id} {'开始服务' if action == 'start' else '完成服务'}"
            )
            con.execute(
                """
                INSERT INTO work_order_execution_events(
                    id, scenario_id, work_order_id, action, sequence, occurred_at,
                    technician_id, plan_version_id, booking_id, source_assignment_hash,
                    event_content_hash, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    scenario_id,
                    work_order_id,
                    action,
                    event.sequence,
                    request.occurred_at,
                    event.technician_id,
                    event.plan_version_id,
                    event.booking_id,
                    event.source_assignment_hash,
                    event.event_content_hash,
                    event.model_dump_json(),
                    event.created_at,
                ),
            )
            now = _now()
            con.execute(
                """
                INSERT INTO command_keys(namespace, key, request_fingerprint, status, resource_type, resource_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, 'COMPLETED', 'execution_event', ?, ?, ?, ?)
                """,
                (
                    namespace,
                    request.idempotency_key,
                    request_fingerprint,
                    event.id,
                    json.dumps({"result": result.model_dump(mode="json")}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            return result

    @classmethod
    def _execution_source_context(
        cls,
        con: sqlite3.Connection,
        scenario: ScheduleScenario,
        active_plan_version_id: str | None,
    ) -> ExecutionSourceContext:
        event_rows = con.execute(
            "SELECT * FROM work_order_execution_events WHERE scenario_id=? ORDER BY sequence",
            (scenario.id,),
        ).fetchall()
        verified_events: list[WorkOrderExecutionEvent] = []
        events_by_order_action: dict[tuple[str, str], WorkOrderExecutionEvent] = {}
        for expected_sequence, event_row in enumerate(event_rows, start=1):
            event = cls._load_execution_event_row(con, event_row)
            if event.sequence != expected_sequence:
                raise DecisionAnalysisIntegrityError(
                    "执行事件水位存在断裂，不能用于重排或发布",
                    record_id=event.id,
                    record_type="WORK_ORDER_EXECUTION_EVENT",
                )
            if event.action == "complete":
                start_event = events_by_order_action.get((event.work_order_id, "start"))
                if (
                    not start_event
                    or start_event.sequence >= event.sequence
                    or start_event.booking_id != event.booking_id
                    or start_event.source_assignment_hash != event.source_assignment_hash
                ):
                    raise DecisionAnalysisIntegrityError(
                        "完成事件缺少匹配且更早的开始事件",
                        record_id=event.id,
                        record_type="WORK_ORDER_EXECUTION_EVENT",
                    )
            events_by_order_action[(event.work_order_id, event.action)] = event
            verified_events.append(event)
        watermark = verified_events[-1].sequence if verified_events else 0
        plan: PlanVersion | None = None
        if active_plan_version_id:
            plan_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=? AND scenario_id=?",
                (active_plan_version_id, scenario.id),
            ).fetchone()
            if not plan_row and verified_events:
                raise DecisionAnalysisIntegrityError(
                    "活动方案不存在，执行事件不能建立可信来源上下文",
                    record_id=active_plan_version_id,
                    record_type="PLAN_VERSION",
                )
            if plan_row:
                loaded_plan = cls._load_plan_row(con, plan_row)
                plan = loaded_plan if loaded_plan.effective_integrity is AnalysisIntegrityStatus.verified else None
            if plan_row and not plan and verified_events:
                raise DecisionAnalysisIntegrityError(
                    "活动方案完整性校验失败，执行事件不能用于重排或发布",
                    record_id=active_plan_version_id,
                    record_type="PLAN_VERSION",
                )
        if verified_events and not plan:
            raise DecisionAnalysisIntegrityError(
                "存在执行事件但没有可信活动方案",
                record_id=verified_events[-1].id,
                record_type="WORK_ORDER_EXECUTION_EVENT",
            )
        assignments = {item.work_order_id: item for item in plan.selected.assignments} if plan else {}
        started_sources: list[ExecutionSourceAssignment] = []
        completed_sources: list[ExecutionSourceAssignment] = []
        for order in sorted(scenario.work_orders, key=lambda item: item.id):
            if order.status not in {WorkOrderStatus.started, WorkOrderStatus.completed}:
                continue
            assignment = assignments.get(order.id)
            if order.status is WorkOrderStatus.started and (not assignment or not plan):
                continue
            start_event = events_by_order_action.get((order.id, "start"))
            if not start_event:
                raise DecisionAnalysisIntegrityError(
                    "工单执行状态缺少可信开始事件",
                    record_id=f"{scenario.id}:{order.id}",
                    record_type="SCENARIO_WORK_ORDER_STATUS",
                )
            source_plan: PlanVersion | None = None
            if start_event.plan_version_id:
                source_plan_row = con.execute(
                    "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=? AND scenario_id=?",
                    (start_event.plan_version_id, scenario.id),
                ).fetchone()
                if source_plan_row:
                    source_plan = cls._load_plan_row(con, source_plan_row)
                    if source_plan.effective_integrity is not AnalysisIntegrityStatus.verified:
                        source_plan = None
            historical_assignment = (
                next(
                    (item for item in source_plan.selected.assignments if item.work_order_id == order.id),
                    None,
                )
                if source_plan
                else None
            )
            identity_assignment = historical_assignment or assignment
            if not identity_assignment:
                continue
            source = ExecutionSourceAssignment(
                work_order_id=order.id,
                technician_id=start_event.technician_id,
                source_schedule_id=(source_plan.selected.id if source_plan else plan.selected.id if plan else ""),
                booking_id=start_event.booking_id or None,
                source_assignment_hash=(
                    start_event.source_assignment_hash
                    or identity_assignment.source_assignment_hash
                    or assignment_source_fingerprint(identity_assignment)
                ),
                sequence=(
                    assignment.sequence if assignment else start_event.source_sequence or identity_assignment.sequence
                ),
                source_sequence=(
                    start_event.source_sequence or identity_assignment.source_sequence or identity_assignment.sequence
                ),
                future_sequence=assignment.sequence if assignment else None,
                planned_start_at=start_event.planned_start_at or identity_assignment.start_time,
                planned_finish_at=start_event.planned_finish_at or identity_assignment.finish_time,
                actual_start_at=start_event.occurred_at,
                projected_available_at=start_event.occurred_at
                + (start_event.estimated_remaining_minutes or order.service_duration),
            )
            if order.status is WorkOrderStatus.started:
                if (order.id, "complete") in events_by_order_action:
                    raise DecisionAnalysisIntegrityError(
                        "服务中工单已经存在完成事件",
                        record_id=events_by_order_action[(order.id, "complete")].id,
                        record_type="WORK_ORDER_EXECUTION_EVENT",
                    )
                started_sources.append(source)
            else:
                completion = events_by_order_action.get((order.id, "complete"))
                if not completion:
                    raise DecisionAnalysisIntegrityError(
                        "已完成工单缺少可信完成事件",
                        record_id=f"{scenario.id}:{order.id}",
                        record_type="SCENARIO_WORK_ORDER_STATUS",
                    )
                source.projected_available_at = completion.occurred_at
                completed_sources.append(source)
        latest_by_technician: dict[str, WorkOrderExecutionEvent] = {}
        for event in verified_events:
            latest_by_technician[event.technician_id] = event
        orders = {item.id: item for item in scenario.work_orders}
        for event in verified_events:
            event_order = orders.get(event.work_order_id)
            if not event_order or event_order.status is WorkOrderStatus.pending:
                raise DecisionAnalysisIntegrityError(
                    "执行事件与当前工单状态不一致",
                    record_id=event.id,
                    record_type="WORK_ORDER_EXECUTION_EVENT",
                )
        projections: list[TechnicianExecutionProjection] = []
        for technician_id, event in sorted(latest_by_technician.items()):
            order = orders.get(event.work_order_id)
            if not order:
                continue
            state = "started" if order.status is WorkOrderStatus.started else "completed"
            available_at = (
                event.occurred_at + (event.estimated_remaining_minutes or order.service_duration)
                if state == "started" and event.action == "start"
                else event.occurred_at
            )
            projections.append(
                TechnicianExecutionProjection(
                    technician_id=technician_id,
                    source_work_order_id=order.id,
                    state=state,
                    effective_location=order.location,
                    available_at=available_at,
                    execution_event_sequence=event.sequence,
                    estimated_remaining_minutes=(
                        event.estimated_remaining_minutes or order.service_duration if state == "started" else 0
                    ),
                )
            )
        return ExecutionSourceContext(
            active_plan_version_id=plan.id if plan else active_plan_version_id,
            active_plan_snapshot_hash=plan.scenario_snapshot_hash if plan else None,
            active_schedule_id=plan.selected.id if plan else None,
            execution_event_sequence=watermark,
            started_assignments=started_sources,
            completed_assignments=completed_sources,
            technician_projections=projections,
        )

    def execution_source_context(self, scenario_id: str) -> ExecutionSourceContext:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = self._load_scenario_head_row(con, row)
            return self._execution_source_context(con, scenario, row["active_plan_version_id"])

    def list_execution_events(self, scenario_id: str) -> list[WorkOrderExecutionEvent]:
        malformed: DecisionAnalysisIntegrityError | None = None
        events: list[WorkOrderExecutionEvent] = []
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM work_order_execution_events WHERE scenario_id=? ORDER BY sequence",
                (scenario_id,),
            ).fetchall()
            for row in rows:
                try:
                    event = self._load_execution_event_row(con, row)
                    if event.action == "complete":
                        start = next(
                            (item for item in events if item.work_order_id == event.work_order_id),
                            None,
                        )
                        if (
                            not start
                            or start.action != "start"
                            or start.booking_id != event.booking_id
                            or start.source_assignment_hash != event.source_assignment_hash
                        ):
                            raise ValueError("complete event is not linked to its start event")
                    events.append(event)
                except (DecisionAnalysisIntegrityError, TypeError, ValueError):
                    self._record_read_isolation(
                        con,
                        "work_order_execution_events",
                        str(row["id"]),
                        str(row["payload"]),
                        "read isolation: malformed execution event payload",
                    )
                    malformed = DecisionAnalysisIntegrityError(
                        "执行事件记录无法解析；为避免隐藏执行事实，本次读取已拒绝",
                        record_id=str(row["id"]),
                        record_type="WORK_ORDER_EXECUTION_EVENT",
                    )
                    break
        if malformed:
            raise malformed
        return events

    @classmethod
    def _load_execution_event_row(
        cls,
        con: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> WorkOrderExecutionEvent:
        """Validate an execution event before it can influence another command."""
        try:
            event = WorkOrderExecutionEvent.model_validate_json(row["payload"])
            expected_hash = _execution_event_content_hash(event)
            relation_valid = (
                event.id == row["id"]
                and event.scenario_id == row["scenario_id"]
                and event.work_order_id == row["work_order_id"]
                and event.action == row["action"]
                and event.sequence == row["sequence"]
                and event.occurred_at == row["occurred_at"]
                and event.technician_id == row["technician_id"]
                and event.plan_version_id == row["plan_version_id"]
                and event.booking_id == row["booking_id"]
                and event.source_assignment_hash == row["source_assignment_hash"]
                and event.event_content_hash == row["event_content_hash"] == expected_hash
            )
            if not relation_valid:
                raise ValueError("execution event identity mismatch")
            plan_row = con.execute(
                """
                SELECT id, scenario_id, number, created_at, payload, attestation_requirement
                FROM plan_versions WHERE id=? AND scenario_id=?
                """,
                (event.plan_version_id, event.scenario_id),
            ).fetchone()
            if not plan_row:
                raise ValueError("execution event source plan missing")
            source_plan = cls._load_plan_row(con, plan_row)
            if source_plan.effective_integrity is not AnalysisIntegrityStatus.verified:
                raise ValueError("execution event source plan integrity mismatch")
            source_assignment = next(
                (
                    item
                    for item in source_plan.selected.assignments
                    if item.work_order_id == event.work_order_id and item.technician_id == event.technician_id
                ),
                None,
            )
            if not source_assignment or event.source_assignment_hash != (
                source_assignment.source_assignment_hash or assignment_source_fingerprint(source_assignment)
            ):
                raise ValueError("execution event source assignment mismatch")
            event.self_integrity = AnalysisIntegrityStatus.verified
            event.source_plan_integrity = AnalysisIntegrityStatus.verified
            event.effective_integrity = AnalysisIntegrityStatus.verified
            return event
        except DecisionAnalysisIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionAnalysisIntegrityError(
                "执行事件记录完整性校验失败",
                record_id=str(row["id"]),
                record_type="WORK_ORDER_EXECUTION_EVENT",
            ) from error

    def clone_scenario_idempotently(
        self,
        scenario: ScheduleScenario,
        *,
        namespace: str,
        idempotency_key: str,
        request_fingerprint: str,
        source_version_id: str,
    ) -> ScheduleScenario:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT request_fingerprint, resource_id FROM command_keys WHERE namespace=? AND key=?",
                (namespace, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise PublicationConflict("相同幂等键对应了不同请求")
                row = con.execute("SELECT * FROM scenarios WHERE id=?", (existing["resource_id"],)).fetchone()
                if not row:
                    raise PublicationConflict("幂等克隆记录引用的场景不存在")
                return self._load_scenario_head_row(con, row)
            if con.execute("SELECT 1 FROM scenarios WHERE id=?", (scenario.id,)).fetchone():
                raise PublicationConflict("克隆场景标识已存在")
            con.execute(
                "INSERT INTO scenarios(id, payload, active_plan_version_id, updated_at) VALUES (?, ?, NULL, CURRENT_TIMESTAMP)",
                (scenario.id, scenario.model_dump_json()),
            )
            self._insert_revision(con, scenario, f"从方案 {source_version_id} 克隆")
            now = _now()
            con.execute(
                """
                INSERT INTO command_keys(namespace, key, request_fingerprint, status, resource_type, resource_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, 'COMPLETED', 'scenario', ?, ?, ?, ?)
                """,
                (
                    namespace,
                    idempotency_key,
                    request_fingerprint,
                    scenario.id,
                    json.dumps(
                        {"scenario_id": scenario.id, "source_version_id": source_version_id},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            return scenario

    def list_revisions(self, scenario_id: str) -> list[ScenarioRevision]:
        malformed: DecisionAnalysisIntegrityError | None = None
        revisions: list[ScenarioRevision] = []
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM scenario_revisions WHERE scenario_id=? ORDER BY number", (scenario_id,)
            ).fetchall()
            previous_hash: str | None = None
            expected_number = 0
            ancestor_invalid = False
            for row in rows:
                try:
                    if ancestor_invalid:
                        raise DecisionAnalysisIntegrityError(
                            "业务数据修订依赖无效祖先",
                            record_id=str(row["id"]),
                            record_type="SCENARIO_REVISION",
                        )
                    revision = self._load_revision_row(con, row, previous_hash)
                    if revision.number != expected_number:
                        raise DecisionAnalysisIntegrityError(
                            "业务数据修订必须从 D000 开始并连续递增",
                            record_id=revision.id,
                            record_type="SCENARIO_REVISION",
                        )
                    revisions.append(revision)
                    previous_hash = revision.revision_hash
                    expected_number = revision.number + 1
                except DecisionAnalysisIntegrityError as error:
                    ancestor_invalid = True
                    self._record_read_isolation(
                        con,
                        "scenario_revisions",
                        str(row["id"]),
                        str(row["payload"]),
                        "read isolation: invalid scenario revision proof",
                    )
                    malformed = error
                    break
        if malformed:
            raise malformed
        return revisions

    def next_version(self, scenario_id: str) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(number), 0) AS v FROM plan_versions WHERE scenario_id=?", (scenario_id,)
            ).fetchone()
        return int(row["v"]) + 1

    @staticmethod
    def _version_label(action: str, strategy: str, number: int) -> str:
        names = {
            "baseline": "人工基线",
            "balanced": "均衡优化",
            "completion": "覆盖率优先",
            "punctuality": "准时优先",
            "low_travel": "低行程",
            "low_overtime": "低加班",
            "fair_workload": "工作量公平",
            "stable": "稳定重排",
            "custom": "自定义策略",
        }
        if action == "restore":
            return f"历史恢复 V{number:03d}"
        if action == "reattest":
            return f"重新验证 V{number:03d}"
        return names.get(strategy, action)

    @staticmethod
    def _build_publication_planning_context(
        scenario: ScheduleScenario,
        chosen: ScheduleResult,
        planning: object,
    ) -> PublicationPlanningContext:
        from .models import PlanningContext

        if not isinstance(planning, PlanningContext):
            raise PublicationConflict("重排候选缺少可冻结的发布计划上下文")
        projections = {
            item.technician_id: item
            for item in (
                planning.execution_source_context.technician_projections if planning.execution_source_context else []
            )
        }
        orders = {item.id: item for item in scenario.work_orders}
        routes: dict[str, list] = {}
        for assignment in chosen.assignments:
            routes.setdefault(assignment.technician_id, []).append(assignment)
        frozen_by_id = {item.work_order_id: item for item in planning.frozen_assignments}
        route_entries: list[RouteEntryContext] = []
        for technician in sorted(scenario.technicians, key=lambda item: item.id):
            projection = projections.get(technician.id)
            location = projection.effective_location if projection else technician.start_location
            available_at = max(
                planning.planning_time,
                projection.available_at if projection else technician.shift_start,
            )
            route = sorted(routes.get(technician.id, []), key=lambda item: item.sequence)
            future = next(
                (
                    item
                    for item in route
                    if item.work_order_id not in frozen_by_id
                    and orders.get(item.work_order_id)
                    and orders[item.work_order_id].status is WorkOrderStatus.pending
                ),
                None,
            )
            route_entries.append(
                RouteEntryContext(
                    technician_id=technician.id,
                    location=location,
                    available_at=available_at,
                    return_location=technician.start_location,
                    first_future_work_order_id=future.work_order_id if future else None,
                    source_work_order_id=projection.source_work_order_id if projection else None,
                    source_execution_event_sequence=projection.execution_event_sequence if projection else None,
                )
            )
        chosen_by_id = {item.work_order_id: item for item in chosen.assignments}
        execution_booking_by_order = {
            item.work_order_id: item.booking_id
            for item in (
                (planning.execution_source_context.started_assignments if planning.execution_source_context else [])
                + (planning.execution_source_context.completed_assignments if planning.execution_source_context else [])
            )
            if item.booking_id
        }
        frozen_identities: list[FrozenBookingIdentity] = []
        for frozen in sorted(planning.frozen_assignments, key=lambda item: item.work_order_id):
            assignment = chosen_by_id.get(frozen.work_order_id)
            frozen_identities.append(
                FrozenBookingIdentity(
                    work_order_id=frozen.work_order_id,
                    technician_id=frozen.technician_id,
                    booking_id=execution_booking_by_order.get(frozen.work_order_id),
                    source_sequence=(
                        frozen.source_sequence
                        or (assignment.source_sequence if assignment else None)
                        or (assignment.sequence if assignment else None)
                    ),
                    source_assignment_hash=(
                        frozen.source_assignment_hash or (assignment.source_assignment_hash if assignment else None)
                    ),
                )
            )
        context_payload = {
            "policy_version": "FIELD_SERVICE_PUBLICATION_CONTEXT_V1",
            "scenario_revision": scenario.revision,
            "planning_time": planning.planning_time,
            "execution_event_sequence": (
                planning.execution_source_context.execution_event_sequence if planning.execution_source_context else 0
            ),
            "source_plan_version_id": planning.source_plan_version_id,
            "source_plan_snapshot_hash": planning.source_plan_snapshot_hash,
            "route_entries": [item.model_dump(mode="json") for item in route_entries],
            "frozen_booking_identities": [item.model_dump(mode="json") for item in frozen_identities],
        }
        fingerprint = content_hash(context_payload)
        return PublicationPlanningContext(**context_payload, context_fingerprint=fingerprint)

    def publish_plan(
        self,
        scenario: ScheduleScenario,
        selected: ScheduleResult,
        action: Literal["baseline", "optimize", "replan", "activate", "restore", "experiment_publish", "reattest"],
        *,
        artifacts: list[ScheduleArtifact] | None = None,
        source_version_id: str | None = None,
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
        replace_scenario: bool = False,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        candidate_id: str,
        experiment_id: str | None = None,
        experiment_candidate_id: str | None = None,
        publication_planning_context_override: PublicationPlanningContext | None = None,
        reattestation_mode: ReattestationMode | None = None,
    ) -> PlanVersion:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                publication = con.execute(
                    "SELECT request_fingerprint, plan_version_id FROM publication_keys WHERE key=?", (idempotency_key,)
                ).fetchone()
                if publication:
                    if publication["request_fingerprint"] != (request_fingerprint or ""):
                        raise PublicationConflict("同一实验已经发布了另一个候选方案")
                    existing = con.execute(
                        "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=?",
                        (publication["plan_version_id"],),
                    ).fetchone()
                    if not existing:
                        raise PublicationConflict("幂等发布记录引用的方案不存在")
                    plan = self._load_plan_row(con, existing)
                    self._require_loaded_plan_for_use(plan, PlanUseCase.replay)
                    if experiment_id and experiment_candidate_id:
                        experiment_row = con.execute(
                            "SELECT payload FROM strategy_experiments WHERE id=?",
                            (experiment_id,),
                        ).fetchone()
                        if not experiment_row:
                            raise PublicationConflict("策略实验不存在")
                        experiment = StrategyExperiment.model_validate_json(experiment_row["payload"])
                        if experiment.winner_candidate_id not in {None, experiment_candidate_id}:
                            raise PublicationConflict("策略实验已经发布其他候选")
                        experiment.winner_candidate_id = experiment_candidate_id
                        experiment.winner_plan_version_id = plan.id
                        experiment.published_at = experiment.published_at or plan.created_at
                        con.execute(
                            "UPDATE strategy_experiments SET payload=? WHERE id=?",
                            (experiment.model_dump_json(), experiment.id),
                        )
                    return self._overlay_plan_applicability(con, plan)
            current_row = con.execute("SELECT * FROM scenarios WHERE id=?", (scenario.id,)).fetchone()
            if not current_row:
                raise KeyError(f"scenario {scenario.id} not found")
            current_scenario = self._load_scenario_head_row(con, current_row)
            current_revision = current_scenario.revision
            required_revision = (
                expected_revision
                if expected_revision is not None
                else (scenario.revision - 1 if replace_scenario else scenario.revision)
            )
            if current_revision != required_revision:
                raise ScenarioRevisionConflict(required_revision, current_revision)
            candidate_row = con.execute(
                "SELECT payload FROM schedule_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not candidate_row:
                raise PublicationConflict("候选方案不存在")
            candidate = ScheduleCandidate.model_validate_json(candidate_row["payload"])
            if not candidate.publishable or not candidate.verification_report.publishable:
                raise PublicationConflict("候选方案未通过发布验证")
            if candidate.scenario_id != scenario.id:
                raise PublicationConflict("候选方案不属于当前场景")
            if candidate.scenario_revision != scenario.revision:
                raise PublicationConflict("候选方案的数据修订与待发布场景不一致")
            if candidate.scenario_snapshot_hash != content_hash(scenario):
                raise PublicationConflict("候选方案的数据快照与当前场景不一致")
            if candidate.schedule.model_dump(mode="json") != selected.model_dump(mode="json"):
                raise PublicationConflict("待发布排程与已验证候选不一致")
            if candidate.solver_config_hash != selected.solver_config_hash:
                raise PublicationConflict("候选方案的求解配置指纹与排程不一致")
            selected_policy_fingerprint = selected.solver_policy.fingerprint if selected.solver_policy else ""
            if not selected_policy_fingerprint:
                raise PublicationConflict("候选方案缺少完整求解政策快照")
            if candidate.solver_policy_fingerprint != selected_policy_fingerprint:
                raise PublicationConflict("候选方案的求解政策指纹与排程不一致")
            expected_context_hash = content_hash(candidate.planning_context) if candidate.planning_context else None
            if candidate.planning_context_hash != expected_context_hash:
                raise PublicationConflict("候选方案的计划上下文指纹不一致")
            run_row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (candidate.run_id,)).fetchone()
            if not run_row:
                raise PublicationConflict("候选方案缺少求解记录")
            candidate_run = ScheduleRun.model_validate_json(run_row["payload"])
            if (
                candidate_run.scenario_id != candidate.scenario_id
                or candidate_run.scenario_revision != candidate.scenario_revision
                or candidate_run.scenario_snapshot_hash != candidate.scenario_snapshot_hash
            ):
                raise PublicationConflict("求解记录与候选方案的数据谱系不一致")
            if candidate_run.source_plan_version_id != candidate.source_plan_version_id:
                raise PublicationConflict("求解记录与候选方案的来源版本不一致")
            if candidate_run.expected_active_plan_version_id != candidate.expected_active_plan_version_id:
                raise PublicationConflict("求解记录与候选方案的活动版本前置条件不一致")
            if (
                not candidate.reservation_id
                or not candidate.reservation_hash
                or candidate_run.reservation_id != candidate.reservation_id
                or candidate_run.reservation_hash != candidate.reservation_hash
            ):
                raise PublicationConflict(
                    "求解记录或候选缺少一致的规划预留",
                    code="PLANNING_RESERVATION_REQUIRED",
                )
            reservation_row = con.execute(
                "SELECT payload, reservation_hash FROM planning_reservations WHERE id=?",
                (candidate.reservation_id,),
            ).fetchone()
            if not reservation_row:
                raise PublicationConflict(
                    "候选引用的规划预留不存在",
                    code="PLANNING_RESERVATION_MISSING",
                    details={"reservation_id": candidate.reservation_id},
                )
            reservation = PlanningReservation.model_validate_json(reservation_row["payload"])
            if (
                reservation.reservation_hash != reservation_row["reservation_hash"]
                or reservation.reservation_hash != _planning_reservation_hash(reservation)
                or reservation.reservation_hash != candidate.reservation_hash
                or reservation.scenario_id != scenario.id
                or reservation.active_plan_version_id != candidate.expected_active_plan_version_id
                or reservation.source_plan_version_id != candidate.source_plan_version_id
            ):
                raise PublicationConflict(
                    "候选规划预留证明失败",
                    code="PLANNING_RESERVATION_INTEGRITY_FAILED",
                    details={"reservation_id": candidate.reservation_id},
                )
            expected_reservation_revision = scenario.revision - 1 if replace_scenario else scenario.revision
            if (
                reservation.scenario_revision != expected_reservation_revision
                or reservation.scenario_snapshot_hash != content_hash(current_scenario)
                or reservation.scenario_snapshot_hash != str(current_row["current_snapshot_hash"])
            ):
                raise PublicationConflict(
                    "规划预留的数据快照与发布事务不一致",
                    code="PLANNING_RESERVATION_SCENARIO_CONFLICT",
                    details={"reservation_id": reservation.id},
                )
            current_active_plan_id = current_row["active_plan_version_id"]
            if candidate.expected_active_plan_version_id != current_active_plan_id:
                raise PublicationConflict(
                    "求解期间活动方案已变化，结果未发布，请重新运行",
                    code="ACTIVE_PLAN_CHANGED_DURING_COMMAND",
                    details={
                        "expected_active_plan_id": candidate.expected_active_plan_version_id,
                        "current_active_plan_id": current_active_plan_id,
                        "reservation_id": reservation.id,
                    },
                )
            if current_active_plan_id:
                active_row = con.execute(
                    "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                    "FROM plan_versions WHERE id=? AND scenario_id=?",
                    (current_active_plan_id, scenario.id),
                ).fetchone()
                if not active_row:
                    raise PublicationConflict("活动方案记录不存在", code="ACTIVE_PLAN_INTEGRITY_FAILED")
                active_plan = self._load_plan_row(con, active_row)
                active_use_case = (
                    PlanUseCase.audit_view
                    if action == "reattest" and active_plan.id == source_version_id
                    else PlanUseCase.replay
                )
                self._require_loaded_plan_for_use(active_plan, active_use_case)
                if active_plan.publication_manifest_hash != reservation.active_plan_manifest_hash:
                    raise PublicationConflict(
                        "活动方案证明与规划预留不一致",
                        code="ACTIVE_PLAN_CHANGED_DURING_COMMAND",
                        details={"reservation_id": reservation.id, "current_active_plan_id": current_active_plan_id},
                    )
            elif reservation.active_plan_manifest_hash is not None:
                raise PublicationConflict(
                    "规划预留的活动方案已不存在",
                    code="ACTIVE_PLAN_CHANGED_DURING_COMMAND",
                    details={"reservation_id": reservation.id},
                )
            current_execution_context = self._execution_source_context(
                con,
                current_scenario,
                current_active_plan_id,
            )
            if (
                reservation.execution_watermark != current_execution_context.execution_event_sequence
                or reservation.execution_context_hash != content_hash(current_execution_context)
            ):
                raise PublicationConflict(
                    "规划期间执行事实已变化，结果未发布",
                    code="EXECUTION_CONTEXT_CHANGED_DURING_COMMAND",
                    details={"reservation_id": reservation.id},
                )
            if candidate_run.solver_config_hash != candidate.solver_config_hash:
                raise PublicationConflict("求解记录与候选方案的求解配置指纹不一致")
            if candidate_run.solver_policy_fingerprint != candidate.solver_policy_fingerprint:
                raise PublicationConflict("求解记录与候选方案的求解政策指纹不一致")
            policy = selected.solver_policy
            if policy is None:
                raise PublicationConflict("候选方案缺少求解政策")
            if selected.solver_config_hash != content_hash(policy.solver_config):
                raise PublicationConflict("求解政策与实际求解配置不一致")
            expected_policy_limit = (
                candidate_run.requested_time_limit_ms if selected.solver_name == "ortools-routing" else None
            )
            if policy.time_limit_ms != expected_policy_limit:
                raise PublicationConflict("求解政策与求解记录的时间限制不一致")
            if candidate_run.action == "replan" and candidate.planning_context is None:
                raise PublicationConflict("重排候选缺少计划上下文")
            if (
                candidate_run.candidate_id != candidate.id
                or candidate_run.planning_context_hash != candidate.planning_context_hash
            ):
                raise PublicationConflict("求解记录与候选方案的计划上下文不一致")
            publication_source: PlanVersion | None = None
            if source_version_id:
                source_row = con.execute(
                    "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=?",
                    (source_version_id,),
                ).fetchone()
                if not source_row:
                    raise PublicationConflict("来源方案不存在")
                publication_source = self._load_plan_row(con, source_row)
                source_use_case = PlanUseCase.audit_view if action == "reattest" else PlanUseCase.replay
                self._require_loaded_plan_for_use(publication_source, source_use_case)
                if publication_source.scenario_id != scenario.id:
                    raise PublicationConflict("来源方案不属于当前场景")
                if candidate.source_plan_version_id and candidate.source_plan_version_id != publication_source.id:
                    raise PublicationConflict("候选方案引用了另一个来源版本")
                if (
                    reservation.source_plan_version_id != publication_source.id
                    or reservation.source_plan_manifest_hash != publication_source.publication_manifest_hash
                ):
                    raise PublicationConflict(
                        "来源方案与规划预留不一致",
                        code="SOURCE_PLAN_CHANGED_DURING_COMMAND",
                        details={"reservation_id": reservation.id, "resource_id": publication_source.id},
                    )
            elif candidate.source_plan_version_id:
                raise PublicationConflict("候选方案声明了来源版本，但发布请求未携带来源")
            elif reservation.source_plan_version_id is not None:
                raise PublicationConflict("规划预留声明了来源方案，但发布请求未携带来源")
            verification_source = publication_source
            if selected.kind == "replan" and action in {"activate", "restore", "reattest"} and publication_source:
                stability_baseline_id = (
                    publication_source.stability_baseline_version_id or publication_source.source_version_id
                )
                if stability_baseline_id:
                    stability_row = con.execute(
                        "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=? AND scenario_id=?",
                        (stability_baseline_id, scenario.id),
                    ).fetchone()
                    if not stability_row:
                        raise PublicationConflict("稳定性基准方案不存在")
                    verification_source = self._load_plan_row(con, stability_row)
                    self._require_loaded_plan_for_use(verification_source, PlanUseCase.replay)
            transaction_verification = verify_schedule(
                scenario,
                selected,
                verification_source.selected if verification_source and selected.kind == "replan" else None,
                candidate.planning_context,
                self.travel_provider,
                self._execution_source_context(
                    con,
                    current_scenario,
                    current_row["active_plan_version_id"],
                ),
            )
            if not transaction_verification.publishable:
                codes = ", ".join(item.code for item in transaction_verification.errors)
                raise PublicationConflict(f"发布事务复核失败：{codes}")
            row = con.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS v FROM plan_versions WHERE scenario_id=?", (scenario.id,)
            ).fetchone()
            number = int(row["v"])
            chosen = selected.model_copy(deep=True)
            chosen.version = number
            chosen.scenario_id = scenario.id
            chosen.scenario_revision = scenario.revision
            plan_id = f"PV-{scenario.id}-{number}-{uuid.uuid4().hex[:6]}"
            persisted_artifacts: list[ScheduleArtifact] = []
            for artifact in artifacts or []:
                item = artifact.model_copy(deep=True)
                item.schedule.version = number
                item.schedule.scenario_id = scenario.id
                item.schedule.scenario_revision = scenario.revision
                persisted_artifacts.append(item)
            if not any(item.schedule.id == chosen.id for item in persisted_artifacts):
                persisted_artifacts.append(
                    ScheduleArtifact(
                        id=f"ART-{uuid.uuid4().hex[:10]}", role="selected", strategy=chosen.strategy, schedule=chosen
                    )
                )
            source_hash = None
            if publication_source:
                source_hash = publication_source.scenario_snapshot_hash or (
                    content_hash(publication_source.scenario_snapshot) if publication_source.scenario_snapshot else None
                )
            publication_context: PublicationPlanningContext | None = None
            publication_context_hash: str | None = None
            if chosen.kind == "replan":
                publication_context = (
                    publication_planning_context_override.model_copy(deep=True)
                    if publication_planning_context_override
                    else self._build_publication_planning_context(scenario, chosen, candidate.planning_context)
                )
                publication_context_hash = content_hash(
                    publication_context.model_dump(exclude={"context_fingerprint"}, mode="json")
                )
                if (
                    publication_context.context_fingerprint != publication_context_hash
                    or publication_context.scenario_revision != scenario.revision
                ):
                    raise PublicationConflict("发布计划上下文与当前方案不一致")
            verification_report_payload = transaction_verification.model_dump(exclude={"checked_at"}, mode="json")
            verification_report_hash = content_hash(verification_report_payload)
            verification_artifact_payload = {
                "policy_version": "FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
                "candidate_snapshot": candidate.model_dump(mode="json"),
                "planning_context_snapshot": (
                    candidate.planning_context.model_dump(mode="json")
                    if candidate.planning_context
                    else publication_context.model_dump(mode="json")
                    if publication_context
                    else None
                ),
                "transaction_verification_report": verification_report_payload,
                "verified_schedule_hash": content_hash(chosen),
            }
            verification_artifact = PublicationVerificationArtifact(
                **verification_artifact_payload,
                artifact_hash=content_hash(verification_artifact_payload),
            )
            plan = PlanVersion(
                id=plan_id,
                scenario_id=scenario.id,
                number=number,
                action=action,
                label=label or self._version_label(action, chosen.strategy, number),
                data_revision=scenario.revision,
                source_version_id=source_version_id,
                lineage_source_version_id=source_version_id,
                stability_baseline_version_id=(
                    (publication_source.stability_baseline_version_id or publication_source.source_version_id)
                    if chosen.kind == "replan" and action in {"activate", "restore", "reattest"} and publication_source
                    else source_version_id
                    if chosen.kind == "replan"
                    else None
                ),
                relation=relation,
                active=True,
                created_at=_now(),
                scenario_snapshot=scenario.model_copy(deep=True),
                selected=chosen,
                artifacts=persisted_artifacts,
                candidate_id=candidate_id,
                scenario_snapshot_hash=content_hash(scenario),
                published_schedule_hash=content_hash(chosen),
                publication_verification_policy_version="FIELD_SERVICE_PUBLICATION_VERIFICATION_V2",
                publication_verification_report_hash=verification_report_hash,
                publication_verification_artifact=verification_artifact,
                publication_planning_context=publication_context,
                publication_planning_context_hash=publication_context_hash,
                publication_manifest_hash="pending",
                publication_manifest_version="FIELD_SERVICE_PUBLICATION_MANIFEST_V2",
                source_plan_snapshot_hash=source_hash,
                attestation_requirement=AttestationRequirement.required,
                integrity_status=AnalysisIntegrityStatus.verified,
                self_integrity=AnalysisIntegrityStatus.verified,
                effective_integrity=AnalysisIntegrityStatus.verified,
                schedule_integrity=AnalysisIntegrityStatus.verified,
                source_solver_provenance=(
                    SourceSolverProvenance(
                        claimed_solver_name=publication_source.selected.solver_name,
                        claimed_solver_version=publication_source.selected.solver_version,
                        claimed_policy_snapshot=publication_source.selected.solver_policy,
                        integrity=(
                            AnalysisIntegrityStatus.legacy_unattested
                            if action == "reattest"
                            or publication_source.effective_integrity is AnalysisIntegrityStatus.legacy_unattested
                            else AnalysisIntegrityStatus.verified
                        ),
                    )
                    if publication_source
                    else None
                ),
                inherited_source_solver_policy=(
                    publication_source.selected.solver_policy.model_copy(deep=True)
                    if publication_source and publication_source.selected.solver_policy
                    else None
                ),
                replay_validation_policy=("FIELD_SERVICE_REATTESTATION_V1" if action == "reattest" else None),
                reattestation_mode=reattestation_mode if action == "reattest" else None,
            )
            plan.publication_manifest_hash = content_hash(build_plan_manifest_payload(plan))
            con.execute(
                "UPDATE plan_applicability SET active=0, updated_at=? WHERE scenario_id=? AND active=1",
                (_now(), scenario.id),
            )
            persisted_plan = plan.model_copy(deep=True)
            persisted_plan.active = False
            con.execute(
                "INSERT INTO plan_versions(id, scenario_id, number, attestation_requirement, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.id,
                    plan.scenario_id,
                    number,
                    AttestationRequirement.required.value,
                    persisted_plan.model_dump_json(),
                    plan.created_at,
                ),
            )
            con.execute(
                "INSERT INTO plan_metadata(plan_version_id, label, note, tags, updated_at) VALUES (?, ?, '', '[]', ?)",
                (plan.id, plan.label, _now()),
            )
            self._set_plan_applicability(
                con,
                plan.id,
                scenario.id,
                active=True,
                coverage_status=PlanCoverageStatus.current_and_complete,
                evaluated_scenario=scenario,
            )
            con.execute(
                "INSERT INTO schedules(id, scenario_id, kind, version, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chosen.id, chosen.scenario_id, chosen.kind, number, chosen.model_dump_json(), chosen.created_at),
            )
            for artifact in persisted_artifacts:
                con.execute(
                    "INSERT OR REPLACE INTO schedule_artifacts(id, plan_version_id, experiment_id, role, payload, created_at) VALUES (?, ?, NULL, ?, ?, ?)",
                    (artifact.id, plan.id, artifact.role, artifact.model_dump_json(), plan.created_at),
                )
            if replace_scenario:
                con.execute(
                    "INSERT INTO scenarios(id, payload, active_plan_version_id, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, active_plan_version_id=excluded.active_plan_version_id, updated_at=CURRENT_TIMESTAMP",
                    (scenario.id, scenario.model_dump_json(), plan.id),
                )
                revision_reason = (
                    f"恢复方案 V{number:03d}" if action == "restore" else f"突发工单局部重排 V{number:03d}"
                )
                self._insert_revision(con, scenario, revision_reason)
            else:
                con.execute(
                    "UPDATE scenarios SET active_plan_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (plan.id, scenario.id),
                )
            if idempotency_key:
                con.execute(
                    "INSERT INTO publication_keys(key, request_fingerprint, plan_version_id, created_at) VALUES (?, ?, ?, ?)",
                    (idempotency_key, request_fingerprint or "", plan.id, plan.created_at),
                )
            if experiment_id or experiment_candidate_id:
                if not experiment_id or not experiment_candidate_id:
                    raise PublicationConflict("实验发布缺少实验或候选标识")
                experiment_row = con.execute(
                    "SELECT payload FROM strategy_experiments WHERE id=?",
                    (experiment_id,),
                ).fetchone()
                if not experiment_row:
                    raise PublicationConflict("策略实验不存在")
                experiment = StrategyExperiment.model_validate_json(experiment_row["payload"])
                if experiment.scenario_id != scenario.id:
                    raise PublicationConflict("策略实验不属于当前场景")
                if not any(item.id == experiment_candidate_id for item in experiment.candidates):
                    raise PublicationConflict("策略实验候选不存在")
                if experiment.winner_candidate_id not in {None, experiment_candidate_id}:
                    raise PublicationConflict("策略实验已经发布其他候选")
                experiment.winner_candidate_id = experiment_candidate_id
                experiment.winner_plan_version_id = plan.id
                experiment.published_at = plan.created_at
                con.execute(
                    "UPDATE strategy_experiments SET payload=? WHERE id=?",
                    (experiment.model_dump_json(), experiment.id),
                )
            return plan

    def reserve_plan_command(
        self,
        scenario_id: str,
        action: Literal["baseline", "optimize", "replan", "activate", "restore", "reattest", "experiment"],
        *,
        expected_revision: int,
        expected_active_plan_version_id: str | None,
        check_active_plan: bool,
        source_mode: Literal["NONE", "ACTIVE_OR_LATEST", "EXPLICIT"],
        source_plan_version_id: str | None,
        source_use_case: PlanUseCase = PlanUseCase.replay,
        command_fingerprint: str,
        requested_time_limit_seconds: float = 0,
        solver_name: str = "ortools-routing",
        solver_config_hash: str,
        run_id: str | None = None,
        started_at: str | None = None,
    ) -> tuple[PlanningReservation, ScheduleRun]:
        """Freeze every planning input and create its run in one SQLite write snapshot."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            scenario_row = con.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not scenario_row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = self._load_scenario_head_row(con, scenario_row)
            if scenario.revision != expected_revision:
                raise ScenarioRevisionConflict(expected_revision, scenario.revision)
            active_plan_id = scenario_row["active_plan_version_id"]
            if check_active_plan and active_plan_id != expected_active_plan_version_id:
                raise PublicationConflict(
                    "命令开始前活动方案已变化，请刷新后重试",
                    code="ACTIVE_PLAN_CHANGED_DURING_COMMAND",
                    details={
                        "expected_active_plan_id": expected_active_plan_version_id,
                        "current_active_plan_id": active_plan_id,
                    },
                )
            active_plan: PlanVersion | None = None
            if active_plan_id:
                active_row = con.execute(
                    "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                    "FROM plan_versions WHERE id=? AND scenario_id=?",
                    (active_plan_id, scenario_id),
                ).fetchone()
                if not active_row:
                    raise PublicationConflict(
                        "活动方案记录不存在",
                        code="ACTIVE_PLAN_INTEGRITY_FAILED",
                        details={"current_active_plan_id": active_plan_id},
                    )
                active_plan = self._load_plan_row(con, active_row)
                active_use_case = (
                    PlanUseCase.audit_view
                    if action == "reattest" and active_plan.id == source_plan_version_id
                    else PlanUseCase.replay
                )
                self._require_loaded_plan_for_use(active_plan, active_use_case)

            resolved_source_id = source_plan_version_id
            if source_mode == "NONE":
                resolved_source_id = None
            elif source_mode == "ACTIVE_OR_LATEST":
                resolved_source_id = active_plan_id
                if resolved_source_id is None:
                    latest = con.execute(
                        "SELECT id FROM plan_versions WHERE scenario_id=? ORDER BY number DESC LIMIT 1",
                        (scenario_id,),
                    ).fetchone()
                    resolved_source_id = str(latest["id"]) if latest else None
            elif source_mode == "EXPLICIT" and not resolved_source_id:
                raise PublicationConflict(
                    "命令缺少明确来源方案",
                    code="SOURCE_PLAN_REQUIRED",
                )

            source_plan: PlanVersion | None = None
            if resolved_source_id:
                if active_plan and resolved_source_id == active_plan.id:
                    source_plan = active_plan.model_copy(deep=True)
                else:
                    source_row = con.execute(
                        "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                        "FROM plan_versions WHERE id=? AND scenario_id=?",
                        (resolved_source_id, scenario_id),
                    ).fetchone()
                    if not source_row:
                        raise PublicationConflict(
                            "来源方案不存在",
                            code="SOURCE_PLAN_NOT_FOUND",
                            details={"resource_id": resolved_source_id},
                        )
                    source_plan = self._load_plan_row(con, source_row)
                self._require_loaded_plan_for_use(source_plan, source_use_case)

            execution_context = self._execution_source_context(con, scenario, active_plan_id)
            resolved_run_id = run_id or f"RUN-{uuid.uuid4().hex[:12]}"
            reservation_id = f"RES-{resolved_run_id}"
            existing_reservation_row = con.execute(
                "SELECT payload, reservation_hash FROM planning_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if existing_reservation_row:
                reservation = PlanningReservation.model_validate_json(existing_reservation_row["payload"])
                if (
                    reservation.reservation_hash != existing_reservation_row["reservation_hash"]
                    or reservation.reservation_hash != _planning_reservation_hash(reservation)
                    or reservation.command_fingerprint != command_fingerprint
                    or reservation.scenario_id != scenario_id
                    or reservation.action != action
                ):
                    raise PublicationConflict(
                        "规划预留与恢复请求不一致",
                        code="PLANNING_RESERVATION_CONFLICT",
                        details={"reservation_id": reservation_id},
                    )
                run_row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (resolved_run_id,)).fetchone()
                if not run_row:
                    raise PublicationConflict(
                        "规划预留引用的求解记录不存在",
                        code="PLANNING_RESERVATION_RUN_MISSING",
                        details={"reservation_id": reservation_id},
                    )
                run = ScheduleRun.model_validate_json(run_row["payload"])
                if run.status is ScheduleRunStatus.failed and run.termination_reason == "APPLICATION_RESTARTED":
                    run.status = ScheduleRunStatus.running
                    run.termination_reason = None
                    run.finished_at = None
                    con.execute(
                        "UPDATE schedule_runs SET status=?, payload=? WHERE id=?",
                        (run.status.value, run.model_dump_json(), run.id),
                    )
                return reservation, run

            now = started_at or _now()
            reservation = PlanningReservation(
                id=reservation_id,
                scenario_id=scenario_id,
                action=action,
                scenario_revision=scenario.revision,
                scenario_snapshot_hash=content_hash(scenario),
                scenario_snapshot=scenario.model_copy(deep=True),
                active_plan_version_id=active_plan.id if active_plan else None,
                active_plan_manifest_hash=active_plan.publication_manifest_hash if active_plan else None,
                source_plan_version_id=source_plan.id if source_plan else None,
                source_plan_manifest_hash=source_plan.publication_manifest_hash if source_plan else None,
                source_plan=source_plan.model_copy(deep=True) if source_plan else None,
                execution_watermark=execution_context.execution_event_sequence,
                execution_context_hash=content_hash(execution_context),
                execution_context=execution_context,
                command_fingerprint=command_fingerprint,
                created_at=now,
            )
            reservation.reservation_hash = _planning_reservation_hash(reservation)
            requested_ms = int(round(requested_time_limit_seconds * 1000))
            run = ScheduleRun(
                id=resolved_run_id,
                scenario_id=scenario_id,
                action=action,
                scenario_revision=scenario.revision,
                scenario_snapshot_hash=reservation.scenario_snapshot_hash,
                source_plan_version_id=reservation.source_plan_version_id,
                source_plan_snapshot_hash=(source_plan.scenario_snapshot_hash if source_plan else None),
                expected_active_plan_version_id=reservation.active_plan_version_id,
                reservation_id=reservation.id,
                reservation_hash=reservation.reservation_hash,
                solver_name=solver_name,
                solver_version="pending",
                solver_config_hash=solver_config_hash,
                requested_time_limit_ms=requested_ms,
                effective_time_limit_ms=requested_ms,
                status=ScheduleRunStatus.running,
                started_at=now,
            )
            con.execute(
                "INSERT INTO planning_reservations(id, scenario_id, reservation_hash, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (reservation.id, scenario_id, reservation.reservation_hash, reservation.model_dump_json(), now),
            )
            con.execute(
                "INSERT INTO schedule_runs(id, scenario_id, status, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (run.id, run.scenario_id, run.status.value, run.model_dump_json(), run.started_at),
            )
            return reservation, run

    def bind_schedule_run_context(
        self,
        run: ScheduleRun,
        reservation: PlanningReservation,
        planning_context: PlanningContext | None,
    ) -> ScheduleRun:
        context_hash = content_hash(planning_context) if planning_context is not None else None
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            reservation_row = con.execute(
                "SELECT payload, reservation_hash FROM planning_reservations WHERE id=?",
                (reservation.id,),
            ).fetchone()
            if not reservation_row:
                raise PublicationConflict("规划预留不存在", code="PLANNING_RESERVATION_MISSING")
            stored_reservation = PlanningReservation.model_validate_json(reservation_row["payload"])
            if (
                stored_reservation.reservation_hash != reservation_row["reservation_hash"]
                or stored_reservation.reservation_hash != _planning_reservation_hash(stored_reservation)
                or stored_reservation.reservation_hash != reservation.reservation_hash
            ):
                raise PublicationConflict("规划预留证明失败", code="PLANNING_RESERVATION_INTEGRITY_FAILED")
            run_row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (run.id,)).fetchone()
            if not run_row:
                raise PublicationConflict("求解记录不存在")
            stored_run = ScheduleRun.model_validate_json(run_row["payload"])
            if (
                stored_run.reservation_id != reservation.id
                or stored_run.reservation_hash != reservation.reservation_hash
            ):
                raise PublicationConflict("求解记录引用了其他规划预留")
            if stored_run.planning_context_hash is not None:
                if stored_run.planning_context_hash != context_hash:
                    raise PublicationConflict("求解记录的计划上下文不可修改")
                return stored_run
            stored_run.planning_context = planning_context
            stored_run.planning_context_hash = context_hash
            con.execute(
                "UPDATE schedule_runs SET payload=? WHERE id=?",
                (stored_run.model_dump_json(), stored_run.id),
            )
            return stored_run

    def get_planning_reservation(self, reservation_id: str) -> PlanningReservation | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT payload, reservation_hash FROM planning_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if not row:
                return None
            reservation = PlanningReservation.model_validate_json(row["payload"])
            if reservation.reservation_hash != row[
                "reservation_hash"
            ] or reservation.reservation_hash != _planning_reservation_hash(reservation):
                raise DecisionAnalysisIntegrityError(
                    "规划预留完整性校验失败",
                    record_id=reservation_id,
                    record_type="PLANNING_RESERVATION",
                )
            return reservation

    def save_schedule_run(self, run: ScheduleRun) -> ScheduleRun:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing_row = con.execute(
                "SELECT payload FROM schedule_runs WHERE id=?",
                (run.id,),
            ).fetchone()
            if existing_row:
                existing = ScheduleRun.model_validate_json(existing_row["payload"])
                terminal = {
                    ScheduleRunStatus.optimal,
                    ScheduleRunStatus.feasible,
                    ScheduleRunStatus.time_limit_feasible,
                    ScheduleRunStatus.time_limit_no_solution,
                    ScheduleRunStatus.infeasible,
                    ScheduleRunStatus.no_solution,
                    ScheduleRunStatus.invalid_model,
                    ScheduleRunStatus.failed,
                    ScheduleRunStatus.cancelled,
                }
                if existing.status in terminal:
                    if existing.model_dump(mode="json") != run.model_dump(mode="json"):
                        raise PublicationConflict("求解记录已进入终态，不能覆盖")
                    return existing
            con.execute(
                "INSERT INTO schedule_runs(id, scenario_id, status, payload, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload",
                (run.id, run.scenario_id, run.status.value, run.model_dump_json(), run.started_at),
            )
        return run

    def resume_interrupted_schedule_run(self, run: ScheduleRun) -> ScheduleRun:
        """Resume exactly the same run after startup marked it interrupted."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (run.id,)).fetchone()
            if not row:
                raise PublicationConflict("待恢复的求解记录不存在")
            stored = ScheduleRun.model_validate_json(row["payload"])
            if stored.status is not ScheduleRunStatus.failed or stored.termination_reason != "APPLICATION_RESTARTED":
                raise PublicationConflict("只有因应用重启中断的求解记录可以恢复")
            expected = stored.model_copy(
                update={
                    "status": ScheduleRunStatus.running,
                    "termination_reason": None,
                    "finished_at": None,
                }
            )
            if expected.model_dump(mode="json") != run.model_dump(mode="json"):
                raise PublicationConflict("恢复请求与原求解输入不一致")
            con.execute(
                "UPDATE schedule_runs SET status=?, payload=? WHERE id=?",
                (run.status.value, run.model_dump_json(), run.id),
            )
        return run

    def complete_schedule_run(
        self, run: ScheduleRun, candidate: ScheduleCandidate
    ) -> tuple[ScheduleRun, ScheduleCandidate]:
        if candidate.run_id != run.id or candidate.scenario_id != run.scenario_id:
            raise PublicationConflict("候选方案与求解记录不匹配")
        if (
            not run.reservation_id
            or not run.reservation_hash
            or candidate.reservation_id != run.reservation_id
            or candidate.reservation_hash != run.reservation_hash
        ):
            raise PublicationConflict("候选方案与求解记录的规划预留不匹配")
        if run.status in {ScheduleRunStatus.queued, ScheduleRunStatus.running} or run.candidate_id != candidate.id:
            raise PublicationConflict("完成求解记录前必须设置终态和对应候选")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run_row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (run.id,)).fetchone()
            if not run_row:
                raise PublicationConflict("求解记录不存在")
            stored = ScheduleRun.model_validate_json(run_row["payload"])
            reservation_row = con.execute(
                "SELECT payload, reservation_hash FROM planning_reservations WHERE id=?",
                (run.reservation_id,),
            ).fetchone()
            if not reservation_row:
                raise PublicationConflict("求解记录引用的规划预留不存在")
            reservation = PlanningReservation.model_validate_json(reservation_row["payload"])
            if (
                reservation.reservation_hash != reservation_row["reservation_hash"]
                or reservation.reservation_hash != _planning_reservation_hash(reservation)
                or reservation.reservation_hash != run.reservation_hash
            ):
                raise PublicationConflict("求解记录引用的规划预留证明失败")
            if stored.status not in {ScheduleRunStatus.queued, ScheduleRunStatus.running}:
                if stored.candidate_id == candidate.id:
                    existing = con.execute(
                        "SELECT payload FROM schedule_candidates WHERE id=?", (candidate.id,)
                    ).fetchone()
                    if existing:
                        return stored, ScheduleCandidate.model_validate_json(existing["payload"])
                raise PublicationConflict("求解记录已经结束，不能再次完成")
            con.execute(
                "INSERT INTO schedule_candidates(id, run_id, scenario_id, publishable, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    candidate.id,
                    candidate.run_id,
                    candidate.scenario_id,
                    int(candidate.publishable),
                    candidate.model_dump_json(),
                    candidate.created_at,
                ),
            )
            con.execute(
                "UPDATE schedule_runs SET status=?, payload=? WHERE id=?",
                (run.status.value, run.model_dump_json(), run.id),
            )
        return run, candidate

    def get_schedule_run(self, run_id: str) -> ScheduleRun | None:
        malformed: DecisionAnalysisIntegrityError | None = None
        run: ScheduleRun | None = None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT id, payload FROM schedule_runs WHERE id=?", (run_id,)).fetchone()
            if row:
                try:
                    run = ScheduleRun.model_validate_json(row["payload"])
                except (TypeError, ValueError):
                    self._record_read_isolation(
                        con, "schedule_runs", str(row["id"]), str(row["payload"]), "read isolation: malformed run"
                    )
                    malformed = DecisionAnalysisIntegrityError(
                        "求解记录无法解析，已隔离原始证据",
                        record_id=str(row["id"]),
                        record_type="SCHEDULE_RUN",
                    )
        if malformed:
            raise malformed
        return run

    def list_schedule_runs(self, scenario_id: str) -> list[ScheduleRun]:
        runs: list[ScheduleRun] = []
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT id, payload FROM schedule_runs WHERE scenario_id=? ORDER BY created_at", (scenario_id,)
            ).fetchall()
            for row in rows:
                try:
                    runs.append(ScheduleRun.model_validate_json(row["payload"]))
                except (TypeError, ValueError):
                    self._record_read_isolation(
                        con, "schedule_runs", str(row["id"]), str(row["payload"]), "read isolation: malformed run"
                    )
        return runs

    def save_schedule_candidate(self, candidate: ScheduleCandidate) -> ScheduleCandidate:
        with self._lock, self._connect() as con:
            existing = con.execute("SELECT payload FROM schedule_candidates WHERE id=?", (candidate.id,)).fetchone()
            if existing:
                stored = ScheduleCandidate.model_validate_json(existing["payload"])
                if stored.model_dump(mode="json") != candidate.model_dump(mode="json"):
                    raise PublicationConflict("已保存的求解候选不可修改")
                return stored
            con.execute(
                "INSERT INTO schedule_candidates(id, run_id, scenario_id, publishable, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    candidate.id,
                    candidate.run_id,
                    candidate.scenario_id,
                    int(candidate.publishable),
                    candidate.model_dump_json(),
                    candidate.created_at,
                ),
            )
        return candidate

    def get_schedule_candidate(self, candidate_id: str) -> ScheduleCandidate | None:
        malformed: DecisionAnalysisIntegrityError | None = None
        candidate: ScheduleCandidate | None = None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT id, payload FROM schedule_candidates WHERE id=?", (candidate_id,)).fetchone()
            if row:
                try:
                    candidate = ScheduleCandidate.model_validate_json(row["payload"])
                except (TypeError, ValueError):
                    self._record_read_isolation(
                        con,
                        "schedule_candidates",
                        str(row["id"]),
                        str(row["payload"]),
                        "read isolation: malformed candidate",
                    )
                    malformed = DecisionAnalysisIntegrityError(
                        "候选方案记录无法解析，已隔离原始证据",
                        record_id=str(row["id"]),
                        record_type="SCHEDULE_CANDIDATE",
                    )
        if malformed:
            raise malformed
        return candidate

    def reserve_decision_analysis_run(
        self,
        run: DecisionAnalysisRun,
        *,
        force_new: bool = False,
    ) -> tuple[DecisionAnalysisRun, bool]:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not force_new:
                existing = con.execute(
                    """
                    SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                           input_hash, status, started_at, finished_at, analysis_manifest_hash
                    FROM decision_analysis_runs
                    WHERE plan_version_id=? AND analysis_type=? AND input_hash=?
                    ORDER BY number DESC LIMIT 1
                    """,
                    (run.plan_version_id, run.analysis_type, run.input_hash),
                ).fetchone()
                if existing:
                    return (self._load_analysis_row(con, existing), False)
            plan_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=? AND scenario_id=?",
                (run.plan_version_id, run.scenario_id),
            ).fetchone()
            if not plan_row:
                raise PublicationConflict("经营分析引用的方案不存在")
            plan = self._require_loaded_plan_for_use(self._load_plan_row(con, plan_row), PlanUseCase.analyze)
            row = con.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS number FROM decision_analysis_runs WHERE scenario_id=?",
                (run.scenario_id,),
            ).fetchone()
            saved = run.model_copy(deep=True)
            saved.number = int(row["number"])
            saved.id = f"AN-{saved.scenario_id}-{saved.number}-{uuid.uuid4().hex[:6]}"
            saved.logical_analysis_id = saved.logical_analysis_id or saved.id
            if force_new and run.logical_analysis_id:
                attempt_rows = con.execute(
                    "SELECT attempt_number FROM decision_analysis_attempts WHERE logical_analysis_id=?",
                    (saved.logical_analysis_id,),
                ).fetchall()
                saved.attempt_number = max((int(item["attempt_number"]) for item in attempt_rows), default=0) + 1
            reservation_payload = {
                "policy_version": "FIELD_SERVICE_ANALYSIS_RESERVATION_V1",
                "analysis_id": saved.id,
                "input_hash": saved.input_hash,
                "plan_manifest_hash": plan.publication_manifest_hash,
                "started_at": saved.created_at,
            }
            saved.reservation_manifest = AnalysisReservationManifest(
                **reservation_payload,
                reservation_hash=content_hash(reservation_payload),
            )
            saved.self_integrity = AnalysisIntegrityStatus.verified
            saved.parent_plan_integrity = AnalysisIntegrityStatus.verified
            saved.effective_integrity = AnalysisIntegrityStatus.verified
            saved.integrity_status = AnalysisIntegrityStatus.verified
            con.execute(
                """
                INSERT INTO decision_analysis_runs(
                    id, scenario_id, number, plan_version_id, analysis_type, input_hash,
                    status, started_at, finished_at, analysis_manifest_hash,
                    attestation_requirement, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    saved.id,
                    saved.scenario_id,
                    saved.number,
                    saved.plan_version_id,
                    saved.analysis_type,
                    saved.input_hash,
                    saved.status,
                    saved.created_at,
                    AttestationRequirement.required.value,
                    saved.model_dump_json(),
                    saved.created_at,
                ),
            )
            con.execute(
                "INSERT INTO decision_analysis_attempts(logical_analysis_id, attempt_number, analysis_run_id) VALUES (?, ?, ?)",
                (saved.logical_analysis_id, saved.attempt_number, saved.id),
            )
            return saved, True

    def finish_decision_analysis_run(
        self,
        run: DecisionAnalysisRun,
        *,
        artifacts: list[DecisionAnalysisArtifact] | None = None,
    ) -> DecisionAnalysisRun:
        if run.status not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise PublicationConflict("经营分析只能结束为完成、失败或中断")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs WHERE id=? AND scenario_id=?
                """,
                (run.id, run.scenario_id),
            ).fetchone()
            if not row:
                raise PublicationConflict("经营分析记录不存在")
            stored = DecisionAnalysisRun.model_validate_json(row["payload"])
            if stored.input_hash != run.input_hash or stored.plan_version_id != run.plan_version_id:
                raise PublicationConflict("经营分析终态与预留输入不一致")
            if row["status"] != "RUNNING":
                return self._load_analysis_row(con, row)
            parent_plan_integrity = AnalysisIntegrityStatus.verified
            parent_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                "FROM plan_versions WHERE id=? AND scenario_id=?",
                (stored.plan_version_id, stored.scenario_id),
            ).fetchone()
            try:
                current_parent = self._load_plan_row(con, parent_row) if parent_row else None
            except DecisionAnalysisIntegrityError:
                current_parent = None
            expected_parent_manifest = stored.input_manifest.plan_manifest_hash if stored.input_manifest else None
            if run.status == "COMPLETED" and (
                current_parent is None
                or current_parent.effective_integrity is not AnalysisIntegrityStatus.verified
                or current_parent.publication_manifest_hash != expected_parent_manifest
            ):
                parent_plan_integrity = AnalysisIntegrityStatus.failed
                run.status = "FAILED"
                run.result = None
                run.error = {
                    "code": "PARENT_PLAN_CHANGED_DURING_ANALYSIS",
                    "message": "经营分析计算期间父方案证明发生变化，结果未写入",
                    "failure_stage": "FINALIZATION",
                }
                run.finished_at = _now()
                artifacts = []
            artifact_manifest: list[dict[str, str]] = []
            for artifact in artifacts or []:
                expected_artifact_hash = content_hash(_artifact_hash_payload(artifact))
                if artifact.artifact_hash != expected_artifact_hash:
                    raise PublicationConflict("容量反事实证据指纹不一致")
                artifact_manifest.append(
                    {
                        "artifact_id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "artifact_hash": artifact.artifact_hash,
                    }
                )
            run.attestation_requirement = AttestationRequirement.required
            run.reservation_manifest = stored.reservation_manifest
            run.result_hash = content_hash(run.result) if run.result is not None else None
            run.result_manifest = None
            run.failure_manifest = None
            if run.status == "COMPLETED":
                if run.result_hash is None or not run.finished_at:
                    raise PublicationConflict("完成的经营分析缺少结果或完成时间")
                run.result_manifest = DecisionResultManifest(
                    result_hash=run.result_hash,
                    finished_at=run.finished_at,
                )
            else:
                if not run.error or not run.finished_at:
                    raise PublicationConflict("失败或中断的经营分析缺少错误证明")
                failure_status: Literal["FAILED", "INTERRUPTED"] = "FAILED" if run.status == "FAILED" else "INTERRUPTED"
                run.failure_manifest = AnalysisFailureManifest(
                    status=failure_status,
                    error_hash=content_hash(run.error),
                    error_code=str(run.error.get("code", "ANALYSIS_FAILED")),
                    failure_stage=str(run.error.get("failure_stage", "EXECUTION")),
                    finished_at=run.finished_at,
                )
            run.artifact_manifest = sorted(artifact_manifest, key=lambda item: item["artifact_id"])
            run.analysis_manifest_hash = content_hash(_analysis_manifest_payload(run))
            run.integrity_status = AnalysisIntegrityStatus.verified
            run.self_integrity = AnalysisIntegrityStatus.verified
            run.parent_plan_integrity = parent_plan_integrity
            run.effective_integrity = _effective_integrity(run.self_integrity, parent_plan_integrity)
            con.execute(
                """
                UPDATE decision_analysis_runs
                SET status=?, finished_at=?, analysis_manifest_hash=?, payload=?
                WHERE id=? AND status='RUNNING'
                """,
                (run.status, run.finished_at, run.analysis_manifest_hash, run.model_dump_json(), run.id),
            )
            if con.execute("SELECT changes()").fetchone()[0] != 1:
                raise PublicationConflict("经营分析状态已变化，不能重复写入终态")
            for artifact in artifacts or []:
                if artifact.analysis_run_id != run.id or artifact.scenario_id != run.scenario_id:
                    raise PublicationConflict("容量反事实证据与经营分析不一致")
                con.execute(
                    """
                    INSERT INTO decision_analysis_artifacts(
                        id, scenario_id, analysis_run_id, option_id, attestation_requirement, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.scenario_id,
                        artifact.analysis_run_id,
                        (
                            artifact.option_id
                            if isinstance(artifact, CapacityCounterfactualArtifact)
                            else "SCENARIO_SET"
                            if isinstance(artifact, SimulationScenarioSetArtifact)
                            else "TRIAL_OUTCOMES"
                        ),
                        AttestationRequirement.required.value,
                        artifact.model_dump_json(),
                        artifact.created_at,
                    ),
                )
        return run

    @classmethod
    def _validate_decision_analysis_integrity(
        cls,
        con: sqlite3.Connection,
        run: DecisionAnalysisRun,
        requirement: str,
        relational_input_hash: str,
        relational_status: str,
        relational_analysis_manifest_hash: str | None,
        relational_id: str,
        relational_scenario_id: str,
        relational_number: int,
        relational_plan_version_id: str,
        relational_analysis_type: str,
        relational_created_at: str,
        relational_started_at: str,
        relational_finished_at: str | None,
    ) -> DecisionAnalysisRun:
        checked = run.model_copy(deep=True)
        checked.attestation_requirement = AttestationRequirement(requirement)
        if (
            checked.id != relational_id
            or checked.scenario_id != relational_scenario_id
            or checked.number != relational_number
            or checked.plan_version_id != relational_plan_version_id
            or checked.analysis_type != relational_analysis_type
            or checked.created_at != relational_created_at
            or checked.created_at != relational_started_at
            or checked.input_hash != relational_input_hash
            or checked.status != relational_status
            or checked.finished_at != relational_finished_at
        ):
            return _failed_integrity_copy(checked, "analysis relational identity or state mismatch")
        plan_row = con.execute(
            "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=? AND scenario_id=?",
            (checked.plan_version_id, checked.scenario_id),
        ).fetchone()
        if not plan_row:
            return _failed_integrity_copy(checked, "referenced plan is missing")
        try:
            plan = cls._load_plan_row(con, plan_row)
        except DecisionAnalysisIntegrityError:
            return _failed_integrity_copy(checked, "referenced plan is malformed")
        checked.parent_plan_integrity = plan.effective_integrity
        if plan.effective_integrity is AnalysisIntegrityStatus.failed:
            return _failed_integrity_copy(checked, "referenced plan integrity failed")
        if checked.attestation_requirement is AttestationRequirement.legacy_migrated:
            checked.self_integrity = AnalysisIntegrityStatus.legacy_unattested
            checked.effective_integrity = _effective_integrity(
                checked.self_integrity,
                checked.parent_plan_integrity,
            )
            checked.integrity_status = checked.effective_integrity
            return checked
        if checked.runtime_manifest is None or checked.input_manifest is None:
            return _failed_integrity_copy(checked, "required input or runtime manifest is missing")
        from .decision import schedule_signature

        if checked.scenario_snapshot_hash != plan.scenario_snapshot_hash or checked.schedule_hash != schedule_signature(
            plan.selected
        ):
            return _failed_integrity_copy(checked, "referenced plan content changed after analysis")
        expected_input_manifest = build_decision_input_manifest(
            analysis_type=checked.analysis_type,
            request_snapshot=checked.request_snapshot,
            policy_snapshot=checked.policy_snapshot,
            analysis_context=_analysis_context_from_run(checked),
            plan_manifest_hash=plan.publication_manifest_hash,
            runtime_manifest=checked.runtime_manifest,
            scenario_snapshot_hash=checked.scenario_snapshot_hash,
            schedule_hash=checked.schedule_hash,
            travel_model_fingerprint=checked.travel_model_fingerprint,
        )
        if (
            checked.input_manifest != expected_input_manifest
            or checked.input_hash != expected_input_manifest.semantic_input_hash
        ):
            return _failed_integrity_copy(checked, "decision input manifest mismatch")
        if checked.status == "RUNNING":
            reservation = checked.reservation_manifest
            expected_reservation_payload = {
                "policy_version": "FIELD_SERVICE_ANALYSIS_RESERVATION_V1",
                "analysis_id": checked.id,
                "input_hash": checked.input_hash,
                "plan_manifest_hash": plan.publication_manifest_hash,
                "started_at": checked.created_at,
            }
            if (
                reservation is None
                or reservation.model_dump(exclude={"reservation_hash"}, mode="json") != expected_reservation_payload
                or reservation.reservation_hash != content_hash(expected_reservation_payload)
                or checked.result is not None
                or checked.result_manifest is not None
                or checked.failure_manifest is not None
                or checked.analysis_manifest_hash is not None
                or relational_analysis_manifest_hash is not None
            ):
                return _failed_integrity_copy(checked, "running reservation manifest mismatch")
            checked.self_integrity = AnalysisIntegrityStatus.verified
            checked.effective_integrity = _effective_integrity(
                checked.self_integrity,
                checked.parent_plan_integrity,
            )
            checked.integrity_status = checked.effective_integrity
            return checked
        if not checked.analysis_manifest_hash or checked.analysis_manifest_hash != relational_analysis_manifest_hash:
            return _failed_integrity_copy(checked, "required analysis manifest is missing")
        if checked.status == "COMPLETED":
            if (
                checked.result is None
                or checked.result_hash is None
                or checked.result_manifest is None
                or checked.failure_manifest is not None
                or checked.result_hash != content_hash(checked.result)
                or checked.result_manifest.result_hash != checked.result_hash
                or checked.result_manifest.finished_at != checked.finished_at
            ):
                return _failed_integrity_copy(checked, "completed result manifest mismatch")
        else:
            if (
                checked.error is None
                or checked.failure_manifest is None
                or checked.result is not None
                or checked.result_manifest is not None
                or checked.failure_manifest.status != checked.status
                or checked.failure_manifest.error_hash != content_hash(checked.error)
                or checked.failure_manifest.error_code != str(checked.error.get("code", "ANALYSIS_FAILED"))
                or checked.failure_manifest.finished_at != checked.finished_at
            ):
                return _failed_integrity_copy(checked, "failure manifest mismatch")
        artifact_rows = con.execute(
            "SELECT id, payload, attestation_requirement FROM decision_analysis_artifacts WHERE analysis_run_id=?",
            (checked.id,),
        ).fetchall()
        actual_artifacts: dict[str, str] = {}
        for row in artifact_rows:
            try:
                artifact = _parse_decision_artifact(json.loads(row["payload"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return _failed_integrity_copy(checked, f"artifact {row['id']} is malformed")
            artifact.attestation_requirement = AttestationRequirement(row["attestation_requirement"])
            if artifact.attestation_requirement is not AttestationRequirement.required or not artifact.artifact_hash:
                return _failed_integrity_copy(checked, f"artifact {row['id']} lacks required attestation")
            if (
                artifact.id != row["id"]
                or artifact.analysis_run_id != checked.id
                or artifact.scenario_id != checked.scenario_id
            ):
                return _failed_integrity_copy(checked, f"artifact {row['id']} relational identity mismatch")
            expected = content_hash(_artifact_hash_payload(artifact))
            if artifact.artifact_hash != expected:
                return _failed_integrity_copy(checked, f"artifact {row['id']} hash mismatch")
            actual_artifacts[row["id"]] = artifact.artifact_hash
        declared = {item.get("artifact_id", ""): item.get("artifact_hash", "") for item in checked.artifact_manifest}
        if declared != actual_artifacts:
            return _failed_integrity_copy(checked, "artifact manifest mismatch")
        if content_hash(_analysis_manifest_payload(checked)) != checked.analysis_manifest_hash:
            return _failed_integrity_copy(checked, "analysis manifest mismatch")
        checked.self_integrity = AnalysisIntegrityStatus.verified
        checked.effective_integrity = _effective_integrity(
            checked.self_integrity,
            checked.parent_plan_integrity,
        )
        checked.integrity_status = checked.effective_integrity
        return checked

    @classmethod
    def _load_analysis_row(cls, con: sqlite3.Connection, row: sqlite3.Row) -> DecisionAnalysisRun:
        try:
            run = DecisionAnalysisRun.model_validate_json(row["payload"])
        except (TypeError, ValueError) as error:
            raise DecisionAnalysisIntegrityError(
                "经营分析记录无法解析",
                record_id=str(row["id"]),
                record_type="DECISION_ANALYSIS_RUN",
            ) from error
        return cls._validate_decision_analysis_integrity(
            con,
            run,
            row["attestation_requirement"],
            row["input_hash"],
            row["status"],
            row["analysis_manifest_hash"],
            str(row["id"]),
            str(row["scenario_id"]),
            int(row["number"]),
            str(row["plan_version_id"]),
            str(row["analysis_type"]),
            str(row["created_at"]),
            str(row["started_at"]),
            str(row["finished_at"]) if row["finished_at"] is not None else None,
        )

    def list_verified_decision_analysis_runs(
        self,
        scenario_id: str,
        plan_version_id: str,
    ) -> list[DecisionAnalysisRun]:
        runs: list[DecisionAnalysisRun] = []
        with self._lock, self._connect() as con:
            rows = con.execute(
                """
                SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs
                WHERE scenario_id=? AND plan_version_id=? ORDER BY number
                """,
                (scenario_id, plan_version_id),
            ).fetchall()
            for row in rows:
                try:
                    runs.append(self._load_analysis_row(con, row))
                except DecisionAnalysisIntegrityError as error:
                    self._record_read_isolation(
                        con,
                        "decision_analysis_runs",
                        str(row["id"]),
                        str(row["payload"]),
                        f"read isolation: {error.record_type}",
                    )
        return runs

    def get_verified_decision_analysis_run(
        self,
        scenario_id: str,
        analysis_id: str,
    ) -> DecisionAnalysisRun | None:
        normalized = analysis_id[1:] if analysis_id.upper().startswith("A") else analysis_id
        numeric = int(normalized) if normalized.isdigit() else -1
        with self._connect() as con:
            row = con.execute(
                """
                SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs
                WHERE scenario_id=? AND (id=? OR number=?)
                """,
                (scenario_id, analysis_id, numeric),
            ).fetchone()
            return self._load_analysis_row(con, row) if row else None

    def list_decision_analysis_runs(self, scenario_id: str, plan_version_id: str) -> list[DecisionAnalysisRun]:
        return self.list_verified_decision_analysis_runs(scenario_id, plan_version_id)

    def get_decision_analysis_run(self, scenario_id: str, analysis_id: str) -> DecisionAnalysisRun | None:
        return self.get_verified_decision_analysis_run(scenario_id, analysis_id)

    @classmethod
    def _load_artifact_row(
        cls,
        con: sqlite3.Connection,
        row: sqlite3.Row,
        parent: DecisionAnalysisRun,
    ) -> DecisionAnalysisArtifact:
        try:
            artifact = _parse_decision_artifact(json.loads(row["payload"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise DecisionAnalysisIntegrityError(
                "经营分析证据无法解析",
                record_id=str(row["id"]),
                record_type="DECISION_ANALYSIS_ARTIFACT",
            ) from error
        artifact.attestation_requirement = AttestationRequirement(row["attestation_requirement"])
        relation_valid = (
            artifact.id == row["id"]
            and artifact.scenario_id == row["scenario_id"]
            and artifact.analysis_run_id == row["analysis_run_id"]
        )
        expected_hash = content_hash(_artifact_hash_payload(artifact))
        artifact.self_integrity = (
            AnalysisIntegrityStatus.legacy_unattested
            if artifact.attestation_requirement is AttestationRequirement.legacy_migrated
            else AnalysisIntegrityStatus.verified
            if relation_valid and artifact.artifact_hash and artifact.artifact_hash == expected_hash
            else AnalysisIntegrityStatus.failed
        )
        artifact.parent_analysis_integrity = parent.effective_integrity
        artifact.effective_integrity = _effective_integrity(
            artifact.self_integrity,
            artifact.parent_analysis_integrity,
        )
        artifact.integrity_status = artifact.effective_integrity
        return artifact

    def list_decision_analysis_artifacts(
        self,
        scenario_id: str,
        analysis_run_id: str,
    ) -> list[DecisionAnalysisArtifact]:
        with self._connect() as con:
            parent_row = con.execute(
                """
                SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs WHERE scenario_id=? AND id=?
                """,
                (scenario_id, analysis_run_id),
            ).fetchone()
            if not parent_row:
                return []
            parent = self._load_analysis_row(con, parent_row)
            rows = con.execute(
                """
                SELECT id, scenario_id, analysis_run_id, payload, attestation_requirement
                FROM decision_analysis_artifacts
                WHERE scenario_id=? AND analysis_run_id=? ORDER BY option_id
                """,
                (scenario_id, analysis_run_id),
            ).fetchall()
            return [self._load_artifact_row(con, row, parent) for row in rows]

    def get_decision_analysis_artifact(
        self,
        scenario_id: str,
        analysis_run_id: str,
        artifact_id: str,
    ) -> DecisionAnalysisArtifact | None:
        with self._connect() as con:
            parent_row = con.execute(
                """
                SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs WHERE scenario_id=? AND id=?
                """,
                (scenario_id, analysis_run_id),
            ).fetchone()
            if not parent_row:
                return None
            parent = self._load_analysis_row(con, parent_row)
            row = con.execute(
                """
                SELECT id, scenario_id, analysis_run_id, payload, attestation_requirement
                FROM decision_analysis_artifacts
                WHERE scenario_id=? AND analysis_run_id=? AND id=?
                """,
                (scenario_id, analysis_run_id, artifact_id),
            ).fetchone()
            return self._load_artifact_row(con, row, parent) if row else None

    @classmethod
    def _comparison_dependency_integrity(
        cls,
        con: sqlite3.Connection,
        comparison: RiskComparisonRun,
    ) -> AnalysisIntegrityStatus:
        dependencies: list[AnalysisIntegrityStatus] = []
        for analysis_id, manifest_hash, trial_id, trial_hash, scenario_id, scenario_hash in (
            (
                comparison.before_analysis_id,
                comparison.before_analysis_manifest_hash,
                comparison.before_trial_artifact_id,
                comparison.before_trial_artifact_hash,
                comparison.before_scenario_artifact_id,
                comparison.before_scenario_artifact_hash,
            ),
            (
                comparison.after_analysis_id,
                comparison.after_analysis_manifest_hash,
                comparison.after_trial_artifact_id,
                comparison.after_trial_artifact_hash,
                comparison.after_scenario_artifact_id,
                comparison.after_scenario_artifact_hash,
            ),
        ):
            run_row = con.execute(
                """
                    SELECT id, scenario_id, number, plan_version_id, analysis_type, created_at, payload, attestation_requirement,
                       input_hash, status, started_at, finished_at, analysis_manifest_hash
                FROM decision_analysis_runs WHERE scenario_id=? AND id=?
                """,
                (comparison.scenario_id, analysis_id),
            ).fetchone()
            if not run_row:
                return AnalysisIntegrityStatus.failed
            run = cls._load_analysis_row(con, run_row)
            dependencies.append(run.effective_integrity)
            if (
                run.status != "COMPLETED"
                or run.analysis_manifest_hash != manifest_hash
                or run.effective_integrity is not AnalysisIntegrityStatus.verified
            ):
                return _effective_integrity(*dependencies, AnalysisIntegrityStatus.failed)
            for artifact_id, artifact_hash in ((trial_id, trial_hash), (scenario_id, scenario_hash)):
                artifact_row = con.execute(
                    """
                    SELECT id, scenario_id, analysis_run_id, payload, attestation_requirement
                    FROM decision_analysis_artifacts
                    WHERE scenario_id=? AND analysis_run_id=? AND id=?
                    """,
                    (comparison.scenario_id, run.id, artifact_id),
                ).fetchone()
                if not artifact_row:
                    return AnalysisIntegrityStatus.failed
                artifact = cls._load_artifact_row(con, artifact_row, run)
                dependencies.append(artifact.effective_integrity)
                if artifact.artifact_hash != artifact_hash:
                    return AnalysisIntegrityStatus.failed
        return _effective_integrity(*dependencies) if dependencies else AnalysisIntegrityStatus.failed

    def save_risk_comparison(
        self,
        comparison: RiskComparisonRun,
        *,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> RiskComparisonRun:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing_key = con.execute(
                "SELECT * FROM risk_comparison_runs WHERE scenario_id=? AND idempotency_key=?",
                (comparison.scenario_id, idempotency_key),
            ).fetchone()
            if existing_key:
                if existing_key["request_fingerprint"] != request_fingerprint:
                    raise PublicationConflict(
                        "相同幂等键对应了不同风险比较请求",
                        code="IDEMPOTENCY_KEY_REUSED",
                    )
                return self._load_risk_comparison_row(con, existing_key)
            expected_input_hash = content_hash(_risk_comparison_input_payload(comparison))
            if comparison.comparison_input_hash != expected_input_hash:
                raise PublicationConflict("风险比较输入指纹不一致")
            if self._comparison_dependency_integrity(con, comparison) is not AnalysisIntegrityStatus.verified:
                raise PublicationConflict("风险比较引用的分析或 trial 证据未通过完整性校验")
            semantic_hash = content_hash(_risk_comparison_hash_payload(comparison))
            existing_semantic = con.execute(
                "SELECT * FROM risk_comparison_runs WHERE scenario_id=? AND comparison_hash=?",
                (comparison.scenario_id, semantic_hash),
            ).fetchone()
            if existing_semantic:
                return self._load_risk_comparison_row(con, existing_semantic)
            row = con.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS number FROM risk_comparison_runs WHERE scenario_id=?",
                (comparison.scenario_id,),
            ).fetchone()
            saved = comparison.model_copy(deep=True)
            saved.number = int(row["number"])
            saved.id = f"RC-{saved.scenario_id}-{saved.number}-{uuid.uuid4().hex[:6]}"
            saved.comparison_hash = semantic_hash
            saved.attestation_requirement = AttestationRequirement.required
            saved.self_integrity = AnalysisIntegrityStatus.verified
            saved.effective_integrity = AnalysisIntegrityStatus.verified
            saved.integrity_status = AnalysisIntegrityStatus.verified
            saved.business_result_available = True
            if saved.result is None:
                raise PublicationConflict("风险比较缺少可证明的业务结果")
            con.execute(
                """
                INSERT INTO risk_comparison_runs(
                    id, scenario_id, number, comparison_hash, comparison_input_hash,
                    idempotency_key, request_fingerprint, attestation_requirement, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    saved.id,
                    saved.scenario_id,
                    saved.number,
                    saved.comparison_hash,
                    saved.comparison_input_hash,
                    idempotency_key,
                    request_fingerprint,
                    AttestationRequirement.required.value,
                    saved.model_dump_json(),
                    saved.created_at,
                ),
            )
            return saved

    @classmethod
    def _load_risk_comparison_row(cls, con: sqlite3.Connection, row: sqlite3.Row) -> RiskComparisonRun:
        try:
            comparison = RiskComparisonRun.model_validate_json(row["payload"])
        except (TypeError, ValueError) as error:
            raise DecisionAnalysisIntegrityError(
                "配对风险比较记录无法解析",
                record_id=str(row["id"]),
                record_type="RISK_COMPARISON_RUN",
            ) from error
        comparison.attestation_requirement = AttestationRequirement(row["attestation_requirement"])
        relation_valid = (
            comparison.id == row["id"]
            and comparison.scenario_id == row["scenario_id"]
            and comparison.number == row["number"]
            and comparison.created_at == row["created_at"]
            and comparison.comparison_input_hash == row["comparison_input_hash"]
            and comparison.comparison_hash == row["comparison_hash"]
        )
        expected_input_hash = content_hash(_risk_comparison_input_payload(comparison))
        expected_hash = content_hash(_risk_comparison_hash_payload(comparison))
        comparison.self_integrity = (
            AnalysisIntegrityStatus.legacy_unattested
            if comparison.attestation_requirement is AttestationRequirement.legacy_migrated
            else AnalysisIntegrityStatus.verified
            if relation_valid
            and comparison.comparison_input_hash == expected_input_hash
            and comparison.comparison_hash == expected_hash
            else AnalysisIntegrityStatus.failed
        )
        dependency_integrity = (
            AnalysisIntegrityStatus.legacy_unattested
            if comparison.attestation_requirement is AttestationRequirement.legacy_migrated
            else cls._comparison_dependency_integrity(con, comparison)
        )
        comparison.effective_integrity = _effective_integrity(
            comparison.self_integrity,
            dependency_integrity,
        )
        comparison.integrity_status = comparison.effective_integrity
        comparison.business_result_available = comparison.effective_integrity is AnalysisIntegrityStatus.verified
        if not comparison.business_result_available:
            comparison.result = None
            comparison.paired_sla_delta = None
            comparison.paired_all_demand_sla_delta = None
            comparison.paired_emergency_completion_delta = None
            comparison.paired_emergency_on_time_delta = None
            comparison.paired_overtime_delta = None
            comparison.paired_unserved_delta = None
            comparison.paired_disruption_delta = None
            comparison.delta = {}
        return comparison

    def get_risk_comparison(self, scenario_id: str, comparison_id: str) -> RiskComparisonRun | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM risk_comparison_runs WHERE scenario_id=? AND id=?",
                (scenario_id, comparison_id),
            ).fetchone()
            if not row:
                return None
            return self._load_risk_comparison_row(con, row)

    def risk_comparison_for_idempotency(
        self,
        scenario_id: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> RiskComparisonRun | None:
        """Resolve a comparison command before running its expensive child analyses."""
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM risk_comparison_runs WHERE scenario_id=? AND idempotency_key=?",
                (scenario_id, idempotency_key),
            ).fetchone()
            if not row:
                return None
            if row["request_fingerprint"] != request_fingerprint:
                raise PublicationConflict(
                    "相同幂等键对应了不同风险比较请求",
                    code="IDEMPOTENCY_KEY_REUSED",
                )
            comparison = self._load_risk_comparison_row(con, row)
            if comparison.effective_integrity is not AnalysisIntegrityStatus.verified:
                raise PublicationConflict(
                    "已有风险比较的依赖证明已失效，不能作为幂等业务结果重放",
                    code="RISK_COMPARISON_INTEGRITY_FAILED",
                    details={"comparison_id": comparison.id},
                )
            return comparison

    def published_for_key(self, key: str, fingerprint: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT request_fingerprint, plan_version_id FROM publication_keys WHERE key=?", (key,)
            ).fetchone()
            if not row:
                return None
            if row["request_fingerprint"] != fingerprint:
                raise PublicationConflict("相同幂等键对应了不同请求")
            plan_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=?",
                (row["plan_version_id"],),
            ).fetchone()
            if not plan_row:
                raise PublicationConflict("幂等发布记录引用的方案不存在")
            return self._require_loaded_plan_for_use(self._load_plan_row(con, plan_row), PlanUseCase.replay)

    def plan_for_publication_key(self, key: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT plan_version_id FROM publication_keys WHERE key=?",
                (key,),
            ).fetchone()
            if not row:
                return None
            plan_row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE id=?",
                (row["plan_version_id"],),
            ).fetchone()
            if not plan_row:
                raise PublicationConflict("幂等发布记录引用的方案不存在")
            return self._require_loaded_plan_for_use(self._load_plan_row(con, plan_row), PlanUseCase.replay)

    def list_plan_versions_with_warnings(
        self,
        scenario_id: str,
        include_snapshots: bool = False,
    ) -> tuple[list[PlanVersion], list[dict[str, str]]]:
        warnings: list[dict[str, str]] = []
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE scenario_id=? ORDER BY number",
                (scenario_id,),
            ).fetchall()
            plans = []
            for row in rows:
                try:
                    plan = self._load_plan_row(con, row)
                except DecisionAnalysisIntegrityError as error:
                    warnings.append(
                        {
                            "record_type": error.record_type,
                            "record_id": error.record_id,
                            "message": "方案记录无法解析，已从列表隔离",
                        }
                    )
                    self._record_read_isolation(
                        con,
                        "plan_versions",
                        str(row["id"]),
                        str(row["payload"]),
                        f"read isolation: {error.record_type}",
                    )
                    continue
                if not include_snapshots:
                    plan.scenario_snapshot = None
                    plan.artifacts = []
                plans.append(plan)
        return plans, warnings

    def list_plan_versions(self, scenario_id: str, include_snapshots: bool = False) -> list[PlanVersion]:
        plans, _warnings = self.list_plan_versions_with_warnings(scenario_id, include_snapshots)
        return plans

    def get_plan_version(self, scenario_id: str, version_id: str) -> PlanVersion | None:
        normalized = version_id[1:] if version_id.upper().startswith("V") else version_id
        numeric = int(normalized) if normalized.isdigit() else -1
        with self._connect() as con:
            row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE scenario_id=? AND (id=? OR number=?)",
                (scenario_id, version_id, numeric),
            ).fetchone()
            if not row:
                return None
            return self._load_plan_row(con, row)

    def require_plan_for_use(
        self,
        scenario_id: str,
        version_id: str,
        use_case: PlanUseCase,
    ) -> PlanVersion:
        plan = self.get_plan_version(scenario_id, version_id)
        if plan is None:
            raise PublicationConflict(
                "方案版本不存在",
                code="PLAN_NOT_FOUND",
                details={"plan_version_id": version_id, "use_case": use_case.value},
            )
        return self._require_loaded_plan_for_use(plan, use_case)

    def active_plan_version(self, scenario_id: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        return (
            self.get_plan_version(scenario_id, row["active_plan_version_id"])
            if row and row["active_plan_version_id"]
            else None
        )

    def operational_view(self, scenario_id: str) -> ScenarioOperationalView:
        with self._lock, self._connect() as con:
            scenario_row = con.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not scenario_row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = self._load_scenario_head_row(con, scenario_row)
            plan: PlanVersion | None = None
            active_plan_id = scenario_row["active_plan_version_id"]
            if active_plan_id:
                plan_row = con.execute(
                    "SELECT id, scenario_id, number, created_at, payload, attestation_requirement "
                    "FROM plan_versions WHERE id=? AND scenario_id=?",
                    (active_plan_id, scenario_id),
                ).fetchone()
                if not plan_row:
                    raise DecisionAnalysisIntegrityError(
                        "活动方案记录不存在",
                        record_id=str(active_plan_id),
                        record_type="PLAN_VERSION",
                    )
                plan = self._load_plan_row(con, plan_row)
                self._require_loaded_plan_for_use(plan, PlanUseCase.execute)
            assignments = {item.work_order_id: item for item in plan.selected.assignments} if plan else {}
            unassigned = {item.work_order_id: item for item in plan.selected.unassigned} if plan else {}
            invalid = set(plan.applicability.invalid_assignment_ids) if plan else set()
            work_order_views: list[OperationalWorkOrderView] = []
            for order in scenario.work_orders:
                assignment = assignments.get(order.id)
                blocking_reason: str | None = None
                if order.status is WorkOrderStatus.completed:
                    disposition = CurrentWorkOrderDisposition.completed
                    blocking_reason = "WORK_ORDER_ALREADY_COMPLETED"
                elif order.status is WorkOrderStatus.started:
                    disposition = CurrentWorkOrderDisposition.started
                    blocking_reason = "WORK_ORDER_ALREADY_STARTED"
                elif order.id in invalid:
                    disposition = CurrentWorkOrderDisposition.assigned_invalid
                    blocking_reason = "INVALID_ASSIGNMENT_CANNOT_START"
                elif assignment is not None:
                    disposition = CurrentWorkOrderDisposition.assigned_valid
                elif order.id in unassigned:
                    disposition = CurrentWorkOrderDisposition.plan_unassigned
                    blocking_reason = unassigned[order.id].reason.value
                else:
                    disposition = CurrentWorkOrderDisposition.new_uncovered
                    blocking_reason = "NEW_DEMAND_NOT_IN_ACTIVE_PLAN" if plan else "NO_ACTIVE_PLAN"
                work_order_views.append(
                    OperationalWorkOrderView(
                        work_order_id=order.id,
                        disposition=disposition,
                        assignment=assignment,
                        start_allowed=disposition is CurrentWorkOrderDisposition.assigned_valid,
                        blocking_reason_code=blocking_reason,
                    )
                )
            active_views = [
                item for item in work_order_views if item.disposition is not CurrentWorkOrderDisposition.completed
            ]
            valid_assigned_count = sum(
                item.disposition in {CurrentWorkOrderDisposition.assigned_valid, CurrentWorkOrderDisposition.started}
                for item in active_views
            )
            active_demand_count = len(active_views)
            metrics = OperationalMetrics(
                active_demand_count=active_demand_count,
                valid_assigned_count=valid_assigned_count,
                invalid_assignment_count=sum(
                    item.disposition is CurrentWorkOrderDisposition.assigned_invalid for item in active_views
                ),
                plan_unassigned_count=sum(
                    item.disposition is CurrentWorkOrderDisposition.plan_unassigned for item in active_views
                ),
                new_uncovered_count=sum(
                    item.disposition is CurrentWorkOrderDisposition.new_uncovered for item in active_views
                ),
                current_actionable_coverage_rate=(
                    valid_assigned_count / active_demand_count if active_demand_count else 1.0
                ),
            )
            return ScenarioOperationalView(
                scenario_id=scenario.id,
                scenario_revision=scenario.revision,
                scenario_snapshot_hash=content_hash(scenario),
                active_plan_version_id=plan.id if plan else None,
                plan_applicability=plan.applicability if plan else None,
                work_orders=work_order_views,
                current_metrics=metrics,
            )

    def latest_plan_version(self, scenario_id: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM plan_versions WHERE scenario_id=? ORDER BY number DESC LIMIT 1", (scenario_id,)
            ).fetchone()
        return self.get_plan_version(scenario_id, row["id"]) if row else None

    def rename_plan_version(self, scenario_id: str, version_id: str, label: str) -> PlanVersion | None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT id, scenario_id, number, created_at, payload, attestation_requirement FROM plan_versions WHERE scenario_id=? AND id=?",
                (scenario_id, version_id),
            ).fetchone()
            if not row:
                return None
            current = self._load_plan_row(con, row)
            if current.integrity_status is AnalysisIntegrityStatus.failed:
                raise PublicationConflict("方案发布证明校验失败，不能重命名")
            con.execute(
                "UPDATE plan_metadata SET label=?, updated_at=? WHERE plan_version_id=?",
                (label, _now(), current.id),
            )
        return self.get_plan_version(scenario_id, version_id)

    def save_schedule(self, result: ScheduleResult) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO schedules(id, scenario_id, kind, version, payload) VALUES (?, ?, ?, ?, ?)",
                (result.id, result.scenario_id, result.kind, result.version, result.model_dump_json()),
            )

    def get_schedule(self, schedule_id: str) -> ScheduleResult | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return ScheduleResult.model_validate_json(row["payload"]) if row else None

    def latest_schedule(
        self, scenario_id: str, kind: str | None = None, scenario_revision: int | None = None
    ) -> ScheduleResult | None:
        for result in reversed(self.list_schedules(scenario_id)):
            if kind and result.kind != kind:
                continue
            if scenario_revision is not None and result.scenario_revision != scenario_revision:
                continue
            return result
        return None

    def list_schedules(self, scenario_id: str) -> list[ScheduleResult]:
        return [
            plan.selected
            for plan in self.list_plan_versions(scenario_id)
            if plan.effective_integrity is AnalysisIntegrityStatus.verified
        ]

    def list_profiles(self, include_stable: bool = True) -> list[StrategyProfile]:
        with self._connect() as con:
            rows = con.execute("SELECT payload FROM strategy_profiles ORDER BY builtin DESC, created_at, id").fetchall()
        profiles = [StrategyProfile.model_validate_json(row["payload"]) for row in rows]
        return profiles if include_stable else [profile for profile in profiles if profile.id != "stable"]

    def get_profile(self, profile_id: str) -> StrategyProfile | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM strategy_profiles WHERE id=?", (profile_id,)).fetchone()
        return StrategyProfile.model_validate_json(row["payload"]) if row else None

    def save_profile(self, request: StrategyProfileCreate, profile_id: str | None = None) -> StrategyProfile:
        existing = self.get_profile(profile_id) if profile_id else None
        if existing and existing.builtin:
            raise ValueError("内置策略不能修改")
        identifier = profile_id or f"custom-{uuid.uuid4().hex[:8]}"
        profile = StrategyProfile(
            id=identifier,
            name=request.name,
            description=request.description,
            builtin=False,
            weights=request.weights,
            time_limit_seconds=request.time_limit_seconds,
            created_at=existing.created_at if existing else _now(),
        )
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO strategy_profiles(id, builtin, payload, created_at) VALUES (?, 0, ?, ?)",
                (profile.id, profile.model_dump_json(), profile.created_at),
            )
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        profile = self.get_profile(profile_id)
        if not profile:
            return False
        if profile.builtin:
            raise ValueError("内置策略不能删除")
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM strategy_profiles WHERE id=?", (profile_id,))
        return True

    def save_experiment(self, experiment: StrategyExperiment) -> None:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM strategy_experiments WHERE id=?", (experiment.id,)).fetchone()
            if row:
                current = StrategyExperiment.model_validate_json(row["payload"])
                terminal = {"CANCELLED", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "INTERRUPTED"}
                if current.status in terminal and experiment.status != current.status:
                    return
                if current.status == "CANCEL_REQUESTED":
                    if experiment.status in terminal:
                        experiment.status = "CANCELLED"
                        experiment.error = "实验已由用户取消"
                        experiment.finished_at = experiment.finished_at or _now()
                    else:
                        experiment.status = "CANCEL_REQUESTED"
                    experiment.cancel_requested_at = current.cancel_requested_at
            con.execute(
                "INSERT OR REPLACE INTO strategy_experiments(id, scenario_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (experiment.id, experiment.scenario_id, experiment.model_dump_json(), experiment.created_at),
            )
            for candidate in experiment.candidates:
                artifact = ScheduleArtifact(
                    id=candidate.id, role="candidate", strategy=candidate.profile_id, schedule=candidate.schedule
                )
                con.execute(
                    "INSERT OR REPLACE INTO schedule_artifacts(id, plan_version_id, experiment_id, role, payload, created_at) VALUES (?, NULL, ?, 'candidate', ?, ?)",
                    (artifact.id, experiment.id, artifact.model_dump_json(), experiment.created_at),
                )

    def queue_experiment(self, experiment: StrategyExperiment) -> tuple[StrategyExperiment, bool]:
        """Persist a queued experiment, coalescing an identical active request."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT payload FROM strategy_experiments WHERE scenario_id=? ORDER BY created_at DESC",
                (experiment.scenario_id,),
            ).fetchall()
            for row in rows:
                existing = StrategyExperiment.model_validate_json(row["payload"])
                if (
                    existing.status in {"QUEUED", "RUNNING"}
                    and existing.fingerprint
                    and existing.fingerprint == experiment.fingerprint
                ):
                    return existing, False
            con.execute(
                "INSERT INTO strategy_experiments(id, scenario_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (experiment.id, experiment.scenario_id, experiment.model_dump_json(), experiment.created_at),
            )
            return experiment, True

    def active_experiment_by_fingerprint(self, scenario_id: str, fingerprint: str) -> StrategyExperiment | None:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM strategy_experiments WHERE scenario_id=? ORDER BY created_at DESC",
                (scenario_id,),
            ).fetchall()
        for row in rows:
            experiment = StrategyExperiment.model_validate_json(row["payload"])
            if experiment.status in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"} and experiment.fingerprint == fingerprint:
                return experiment
        return None

    def get_experiment(self, experiment_id: str) -> StrategyExperiment | None:
        malformed: DecisionAnalysisIntegrityError | None = None
        experiment: StrategyExperiment | None = None
        with self._lock, self._connect() as con:
            row = con.execute("SELECT id, payload FROM strategy_experiments WHERE id=?", (experiment_id,)).fetchone()
            if row:
                try:
                    experiment = StrategyExperiment.model_validate_json(row["payload"])
                except (TypeError, ValueError):
                    self._record_read_isolation(
                        con,
                        "strategy_experiments",
                        str(row["id"]),
                        str(row["payload"]),
                        "read isolation: malformed experiment",
                    )
                    malformed = DecisionAnalysisIntegrityError(
                        "策略实验记录无法解析，已隔离原始证据",
                        record_id=str(row["id"]),
                        record_type="STRATEGY_EXPERIMENT",
                    )
        if malformed:
            raise malformed
        return experiment

    def request_experiment_cancel(self, experiment_id: str) -> StrategyExperiment | None:
        """Atomically record cooperative cancellation without clobbering a terminal result."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM strategy_experiments WHERE id=?", (experiment_id,)).fetchone()
            if not row:
                return None
            experiment = StrategyExperiment.model_validate_json(row["payload"])
            if experiment.status in {"QUEUED", "RUNNING"}:
                experiment.status = "CANCEL_REQUESTED"
                experiment.cancel_requested_at = _now()
                con.execute(
                    "UPDATE strategy_experiments SET payload=? WHERE id=?",
                    (experiment.model_dump_json(), experiment.id),
                )
            return experiment
