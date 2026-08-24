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

from .fixtures import all_fixtures
from .hashing import content_hash
from .models import (
    AnalysisIntegrityStatus,
    CapacityCounterfactualArtifact,
    DecisionAnalysisArtifact,
    DecisionAnalysisRun,
    ExecutionSourceAssignment,
    ExecutionSourceContext,
    FrozenBookingIdentity,
    PlanCoverageStatus,
    PlanVersion,
    PublicationPlanningContext,
    PublicationVerificationArtifact,
    RouteEntryContext,
    ScenarioRevision,
    ScheduleArtifact,
    ScheduleCandidate,
    ScheduleResult,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleScenario,
    SimulationScenarioSetArtifact,
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
from .timeutils import service_ready_at
from .travel import DEFAULT_TRAVEL_PROVIDER, TravelTimeProvider
from .verification import verify_schedule

SCHEMA_VERSION = 17


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


class PublicationConflict(RuntimeError):
    def __init__(self, message: str, *, code: str = "PUBLICATION_CONFLICT", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scenario_id, number),
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
            INSERT INTO scenario_revisions_new SELECT r.* FROM scenario_revisions r WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=r.scenario_id);
            INSERT INTO plan_versions_new SELECT p.* FROM plan_versions p WHERE EXISTS (SELECT 1 FROM scenarios x WHERE x.id=p.scenario_id);
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

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as con:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{version} is newer than this application supports (v{SCHEMA_VERSION})"
                )
            if version < SCHEMA_VERSION:
                self._backup_legacy(con)
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    active_plan_version_id TEXT,
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
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS plan_versions (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number),
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS plan_applicability (
                    plan_version_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    coverage_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS decision_analysis_runs (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    plan_version_id TEXT NOT NULL,
                    analysis_type TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
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
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_scenario ON schedules(scenario_id, version);
                CREATE INDEX IF NOT EXISTS idx_plan_versions_scenario ON plan_versions(scenario_id, number);
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
            if "sequence" not in event_columns:
                con.execute("ALTER TABLE work_order_execution_events ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
            command_columns = {row[1] for row in con.execute("PRAGMA table_info(command_keys)")}
            if "publication_key" not in command_columns:
                con.execute("ALTER TABLE command_keys ADD COLUMN publication_key TEXT")
                con.execute("UPDATE command_keys SET publication_key=key WHERE namespace='schedule-solve'")
                con.execute(
                    "UPDATE command_keys SET publication_key=namespace || ':' || key WHERE namespace LIKE '%:replan'"
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
                    DROP TABLE IF EXISTS decision_analysis_artifacts;
                    ALTER TABLE decision_analysis_runs RENAME TO decision_analysis_runs_legacy;
                    CREATE TABLE decision_analysis_runs (
                        id TEXT PRIMARY KEY,
                        scenario_id TEXT NOT NULL,
                        number INTEGER NOT NULL,
                        plan_version_id TEXT NOT NULL,
                        analysis_type TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(scenario_id, number),
                        FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                        FOREIGN KEY(plan_version_id) REFERENCES plan_versions(id) ON DELETE CASCADE
                    );
                    INSERT INTO decision_analysis_runs SELECT * FROM decision_analysis_runs_legacy;
                    DROP TABLE decision_analysis_runs_legacy;
                    CREATE TABLE decision_analysis_artifacts (
                        id TEXT PRIMARY KEY,
                        scenario_id TEXT NOT NULL,
                        analysis_run_id TEXT NOT NULL,
                        option_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(analysis_run_id, option_id),
                        FOREIGN KEY(scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE,
                        FOREIGN KEY(analysis_run_id) REFERENCES decision_analysis_runs(id) ON DELETE CASCADE
                    );
                    COMMIT;
                    """
                )
                con.execute("PRAGMA foreign_keys = ON")
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
                analysis = DecisionAnalysisRun.model_validate_json(analysis_row["payload"])
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
                # Legacy schedules lack a restorable business snapshot. The user chose a clean restart.
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
            if version < 12:
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
                CREATE INDEX IF NOT EXISTS idx_revisions_scenario ON scenario_revisions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_plan ON schedule_artifacts(plan_version_id);
                CREATE INDEX IF NOT EXISTS idx_runs_scenario ON schedule_runs(scenario_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_run ON schedule_candidates(run_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_run_unique ON schedule_candidates(run_id);
                CREATE INDEX IF NOT EXISTS idx_commands_status ON command_keys(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_execution_events_sequence ON work_order_execution_events(scenario_id, sequence);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_events_sequence_unique ON work_order_execution_events(scenario_id, sequence);
                """
            )
            con.execute("PRAGMA journal_mode = WAL")
            con.execute(
                "UPDATE strategy_experiments SET payload=json_set(payload, '$.status', 'INTERRUPTED', '$.error', '应用重启，实验已中断') WHERE json_extract(payload, '$.status') IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')"
            )
            interrupted = con.execute(
                "SELECT payload FROM schedule_runs WHERE status IN ('QUEUED', 'RUNNING')"
            ).fetchall()
            for row in interrupted:
                run = ScheduleRun.model_validate_json(row["payload"])
                run.status = ScheduleRunStatus.failed
                run.termination_reason = "APPLICATION_RESTARTED"
                run.finished_at = _now()
                con.execute(
                    "UPDATE schedule_runs SET status=?, payload=? WHERE id=?",
                    (run.status.value, run.model_dump_json(), run.id),
                )
            interrupted_analyses = con.execute("SELECT id, payload FROM decision_analysis_runs").fetchall()
            for row in interrupted_analyses:
                analysis = DecisionAnalysisRun.model_validate_json(row["payload"])
                if analysis.status != "RUNNING":
                    continue
                analysis.status = "INTERRUPTED"
                analysis.error = {
                    "code": "APPLICATION_RESTARTED",
                    "message": "应用在经营分析完成前重启",
                }
                analysis.finished_at = _now()
                con.execute(
                    "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                    (analysis.model_dump_json(), analysis.id),
                )
            abandoned_commands = con.execute(
                """
                SELECT namespace, key, publication_key, payload
                FROM command_keys
                WHERE status IN ('RUNNING', 'REPLAN_RUNNING')
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

    def seed_fixtures(self) -> None:
        with self._lock, self._connect() as con:
            for scenario in all_fixtures().values():
                con.execute(
                    "INSERT OR IGNORE INTO scenarios(id, payload) VALUES (?, ?)",
                    (scenario.id, scenario.model_dump_json()),
                )
                self._insert_revision(con, scenario, "内置场景初始化", ignore=True)

    def seed_profiles(self) -> None:
        with self._lock, self._connect() as con:
            for profile in BUILTIN_PROFILES:
                con.execute(
                    "INSERT INTO strategy_profiles(id, builtin, payload, created_at) VALUES (?, 1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, builtin=1",
                    (profile.id, profile.model_dump_json(), profile.created_at),
                )

    @staticmethod
    def _insert_revision(
        con: sqlite3.Connection, scenario: ScheduleScenario, reason: str, ignore: bool = False
    ) -> ScenarioRevision:
        revision = ScenarioRevision(
            id=f"REV-{scenario.id}-{scenario.revision}-{uuid.uuid4().hex[:6]}",
            scenario_id=scenario.id,
            number=scenario.revision,
            reason=reason,
            scenario=scenario.model_copy(deep=True),
            created_at=_now(),
        )
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        con.execute(
            f"{verb} INTO scenario_revisions(id, scenario_id, number, reason, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
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

    @staticmethod
    def _set_plan_applicability(
        con: sqlite3.Connection,
        plan_version_id: str,
        scenario_id: str,
        *,
        active: bool | None = None,
        coverage_status: PlanCoverageStatus | None = None,
    ) -> None:
        existing = con.execute(
            "SELECT active, coverage_status FROM plan_applicability WHERE plan_version_id=?",
            (plan_version_id,),
        ).fetchone()
        resolved_active = int(active if active is not None else bool(existing and existing["active"]))
        resolved_coverage = (
            coverage_status.value
            if coverage_status is not None
            else existing["coverage_status"]
            if existing
            else PlanCoverageStatus.current_and_complete.value
        )
        con.execute(
            """
            INSERT INTO plan_applicability(plan_version_id, scenario_id, active, coverage_status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plan_version_id) DO UPDATE SET
                active=excluded.active,
                coverage_status=excluded.coverage_status,
                updated_at=excluded.updated_at
            """,
            (plan_version_id, scenario_id, resolved_active, resolved_coverage, _now()),
        )

    @staticmethod
    def _overlay_plan_applicability(con: sqlite3.Connection, plan: PlanVersion) -> PlanVersion:
        row = con.execute(
            "SELECT active, coverage_status FROM plan_applicability WHERE plan_version_id=?",
            (plan.id,),
        ).fetchone()
        if row:
            plan.active = bool(row["active"])
            plan.coverage_status = PlanCoverageStatus(row["coverage_status"])
        else:
            active = con.execute(
                "SELECT active_plan_version_id FROM scenarios WHERE id=?",
                (plan.scenario_id,),
            ).fetchone()
            plan.active = bool(active and active["active_plan_version_id"] == plan.id)
        return plan

    def list_scenarios(self) -> list[ScheduleScenario]:
        with self._connect() as con:
            rows = con.execute("SELECT payload FROM scenarios ORDER BY id").fetchall()
        return [ScheduleScenario.model_validate_json(row["payload"]) for row in rows]

    def get_scenario(self, scenario_id: str) -> ScheduleScenario | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        return ScheduleScenario.model_validate_json(row["payload"]) if row else None

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
    ) -> None:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM scenarios WHERE id=?", (scenario.id,)).fetchone()
            current_revision = ScheduleScenario.model_validate_json(row["payload"]).revision if row else -1
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
                            coverage_status=PlanCoverageStatus.stale_data_changed,
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
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            command = con.execute(
                "SELECT request_fingerprint FROM command_keys WHERE namespace=? AND key=?",
                (namespace, idempotency_key),
            ).fetchone()
            if command and command["request_fingerprint"] != request_fingerprint:
                raise PublicationConflict("相同幂等键对应了不同请求")
            scenario_row = con.execute(
                "SELECT payload, active_plan_version_id FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not scenario_row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = ScheduleScenario.model_validate_json(scenario_row["payload"])
            if command:
                return scenario, False
            existing = next((item for item in scenario.work_orders if item.id == order.id), None)
            if existing and existing.model_dump(mode="json") != order.model_dump(mode="json"):
                raise PublicationConflict(f"工单 {order.id} 已存在，但内容与本次请求不同")
            created = existing is None
            if created:
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
                active_plan_id = scenario_row["active_plan_version_id"]
                if active_plan_id:
                    self._set_plan_applicability(
                        con,
                        active_plan_id,
                        scenario.id,
                        active=True,
                        coverage_status=PlanCoverageStatus.partial_new_demand,
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
                "SELECT request_fingerprint, payload FROM command_keys WHERE namespace=? AND key=?",
                (namespace, request.idempotency_key),
            ).fetchone()
            if command:
                if command["request_fingerprint"] != request_fingerprint:
                    raise PublicationConflict("相同幂等键对应了不同执行请求")
                payload = json.loads(command["payload"])
                stored = WorkOrderExecutionResult.model_validate(payload["result"])
                scenario_row = con.execute(
                    "SELECT payload FROM scenarios WHERE id=?",
                    (scenario_id,),
                ).fetchone()
                if not scenario_row:
                    raise KeyError(f"场景 {scenario_id} 不存在")
                current_scenario = ScheduleScenario.model_validate_json(scenario_row["payload"])
                return WorkOrderExecutionResult(
                    scenario=current_scenario,
                    event=stored.event,
                )

            scenario_row = con.execute(
                "SELECT payload, active_plan_version_id FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not scenario_row:
                raise KeyError(f"场景 {scenario_id} 不存在")
            scenario = ScheduleScenario.model_validate_json(scenario_row["payload"])
            if scenario.revision != request.expected_revision:
                raise ScenarioRevisionConflict(request.expected_revision, scenario.revision)
            order = next((item for item in scenario.work_orders if item.id == work_order_id), None)
            if not order:
                raise KeyError(f"工单 {work_order_id} 不存在")
            plan_id = scenario_row["active_plan_version_id"]
            if not plan_id:
                raise PublicationConflict("当前没有可执行方案，请先生成并发布方案")
            plan_row = con.execute("SELECT payload FROM plan_versions WHERE id=?", (plan_id,)).fetchone()
            if not plan_row:
                raise PublicationConflict("当前方案记录不存在，请刷新后重试")
            plan = PlanVersion.model_validate_json(plan_row["payload"])
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
                        SELECT payload FROM work_order_execution_events
                        WHERE scenario_id=? AND work_order_id=? AND action='complete'
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        (scenario_id, predecessor.work_order_id),
                    ).fetchone()
                    if not completion_row:
                        raise PublicationConflict(f"路线前序工单 {predecessor.work_order_id} 缺少完成事件")
                    completion = WorkOrderExecutionEvent.model_validate_json(completion_row["payload"])
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
                    "SELECT payload FROM work_order_execution_events WHERE scenario_id=? AND work_order_id=? AND action='start' ORDER BY occurred_at DESC LIMIT 1",
                    (scenario_id, work_order_id),
                ).fetchone()
                if not start_row:
                    raise PublicationConflict("找不到该工单的开始服务记录")
                start_event = WorkOrderExecutionEvent.model_validate_json(start_row["payload"])
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
                actual_late_start_minutes=(max(0, request.occurred_at - order.window_end) if action == "start" else 0),
                early_start_override_reason=(request.early_start_override_reason if action == "start" else None),
                estimated_remaining_minutes=(request.estimated_remaining_minutes if action == "start" else None),
                note=request.note,
            )
            result = WorkOrderExecutionResult(scenario=scenario, event=event)
            con.execute(
                "UPDATE scenarios SET payload=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (scenario.model_dump_json(), scenario_id),
            )
            self._insert_revision(
                con, scenario, f"工单 {work_order_id} {'开始服务' if action == 'start' else '完成服务'}"
            )
            con.execute(
                "INSERT INTO work_order_execution_events(id, scenario_id, work_order_id, action, sequence, occurred_at, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    scenario_id,
                    work_order_id,
                    action,
                    event.sequence,
                    request.occurred_at,
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

    @staticmethod
    def _execution_source_context(
        con: sqlite3.Connection,
        scenario: ScheduleScenario,
        active_plan_version_id: str | None,
    ) -> ExecutionSourceContext:
        sequence_row = con.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM work_order_execution_events WHERE scenario_id=?",
            (scenario.id,),
        ).fetchone()
        watermark = int(sequence_row["sequence"])
        plan: PlanVersion | None = None
        if active_plan_version_id:
            plan_row = con.execute(
                "SELECT payload FROM plan_versions WHERE id=? AND scenario_id=?",
                (active_plan_version_id, scenario.id),
            ).fetchone()
            if plan_row:
                plan = PlanVersion.model_validate_json(plan_row["payload"])
        assignments = {item.work_order_id: item for item in plan.selected.assignments} if plan else {}
        started_sources: list[ExecutionSourceAssignment] = []
        completed_sources: list[ExecutionSourceAssignment] = []
        for order in sorted(scenario.work_orders, key=lambda item: item.id):
            if order.status not in {WorkOrderStatus.started, WorkOrderStatus.completed}:
                continue
            assignment = assignments.get(order.id)
            if order.status is WorkOrderStatus.started and (not assignment or not plan):
                continue
            event_row = con.execute(
                """
                SELECT payload FROM work_order_execution_events
                WHERE scenario_id=? AND work_order_id=? AND action='start'
                ORDER BY sequence DESC LIMIT 1
                """,
                (scenario.id, order.id),
            ).fetchone()
            start_event = WorkOrderExecutionEvent.model_validate_json(event_row["payload"]) if event_row else None
            if not start_event:
                continue
            source_plan: PlanVersion | None = None
            if start_event.plan_version_id:
                source_plan_row = con.execute(
                    "SELECT payload FROM plan_versions WHERE id=? AND scenario_id=?",
                    (start_event.plan_version_id, scenario.id),
                ).fetchone()
                if source_plan_row:
                    source_plan = PlanVersion.model_validate_json(source_plan_row["payload"])
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
                started_sources.append(source)
            else:
                completion_row = con.execute(
                    """
                    SELECT payload FROM work_order_execution_events
                    WHERE scenario_id=? AND work_order_id=? AND action='complete'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (scenario.id, order.id),
                ).fetchone()
                if completion_row:
                    completion = WorkOrderExecutionEvent.model_validate_json(completion_row["payload"])
                    source.projected_available_at = completion.occurred_at
                completed_sources.append(source)
        event_rows = con.execute(
            "SELECT payload FROM work_order_execution_events WHERE scenario_id=? ORDER BY sequence",
            (scenario.id,),
        ).fetchall()
        latest_by_technician: dict[str, WorkOrderExecutionEvent] = {}
        for event_row in event_rows:
            event = WorkOrderExecutionEvent.model_validate_json(event_row["payload"])
            latest_by_technician[event.technician_id] = event
        orders = {item.id: item for item in scenario.work_orders}
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
                "SELECT payload, active_plan_version_id FROM scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"scenario {scenario_id} not found")
            scenario = ScheduleScenario.model_validate_json(row["payload"])
            return self._execution_source_context(con, scenario, row["active_plan_version_id"])

    def list_execution_events(self, scenario_id: str) -> list[WorkOrderExecutionEvent]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM work_order_execution_events WHERE scenario_id=? ORDER BY sequence",
                (scenario_id,),
            ).fetchall()
        return [WorkOrderExecutionEvent.model_validate_json(row["payload"]) for row in rows]

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
                row = con.execute("SELECT payload FROM scenarios WHERE id=?", (existing["resource_id"],)).fetchone()
                if not row:
                    raise PublicationConflict("幂等克隆记录引用的场景不存在")
                return ScheduleScenario.model_validate_json(row["payload"])
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
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM scenario_revisions WHERE scenario_id=? ORDER BY number", (scenario_id,)
            ).fetchall()
        return [ScenarioRevision.model_validate_json(row["payload"]) for row in rows]

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
        return f"历史恢复 V{number:03d}" if action == "restore" else names.get(strategy, action)

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
        frozen_identities: list[FrozenBookingIdentity] = []
        for frozen in sorted(planning.frozen_assignments, key=lambda item: item.work_order_id):
            assignment = chosen_by_id.get(frozen.work_order_id)
            frozen_identities.append(
                FrozenBookingIdentity(
                    work_order_id=frozen.work_order_id,
                    technician_id=frozen.technician_id,
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
        action: Literal["baseline", "optimize", "replan", "activate", "restore", "experiment_publish"],
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
                        "SELECT payload FROM plan_versions WHERE id=?", (publication["plan_version_id"],)
                    ).fetchone()
                    if not existing:
                        raise PublicationConflict("幂等发布记录引用的方案不存在")
                    plan = PlanVersion.model_validate_json(existing["payload"])
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
            current_row = con.execute(
                "SELECT payload, active_plan_version_id FROM scenarios WHERE id=?", (scenario.id,)
            ).fetchone()
            if not current_row:
                raise KeyError(f"scenario {scenario.id} not found")
            current_scenario = ScheduleScenario.model_validate_json(current_row["payload"])
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
                    "SELECT payload FROM plan_versions WHERE id=?", (source_version_id,)
                ).fetchone()
                if not source_row:
                    raise PublicationConflict("来源方案不存在")
                publication_source = PlanVersion.model_validate_json(source_row["payload"])
                if publication_source.scenario_id != scenario.id:
                    raise PublicationConflict("来源方案不属于当前场景")
                if candidate.source_plan_version_id and candidate.source_plan_version_id != publication_source.id:
                    raise PublicationConflict("候选方案引用了另一个来源版本")
            elif candidate.source_plan_version_id:
                raise PublicationConflict("候选方案声明了来源版本，但发布请求未携带来源")
            verification_source = publication_source
            if selected.kind == "replan" and action in {"activate", "restore"} and publication_source:
                stability_baseline_id = (
                    publication_source.stability_baseline_version_id or publication_source.source_version_id
                )
                if stability_baseline_id:
                    stability_row = con.execute(
                        "SELECT payload FROM plan_versions WHERE id=? AND scenario_id=?",
                        (stability_baseline_id, scenario.id),
                    ).fetchone()
                    if not stability_row:
                        raise PublicationConflict("稳定性基准方案不存在")
                    verification_source = PlanVersion.model_validate_json(stability_row["payload"])
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
            publication_manifest_hash = content_hash(
                {
                    "policy_version": "FIELD_SERVICE_PUBLICATION_MANIFEST_V1",
                    "scenario_snapshot_hash": content_hash(scenario),
                    "published_schedule_hash": content_hash(chosen),
                    "publication_planning_context_hash": publication_context_hash,
                    "publication_verification_artifact_hash": verification_artifact.artifact_hash,
                }
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
                    if chosen.kind == "replan" and action in {"activate", "restore"} and publication_source
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
                publication_manifest_hash=publication_manifest_hash,
                source_plan_snapshot_hash=source_hash,
            )
            con.execute(
                "UPDATE plan_applicability SET active=0, updated_at=? WHERE scenario_id=? AND active=1",
                (_now(), scenario.id),
            )
            persisted_plan = plan.model_copy(deep=True)
            persisted_plan.active = False
            con.execute(
                "INSERT INTO plan_versions(id, scenario_id, number, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (plan.id, plan.scenario_id, number, persisted_plan.model_dump_json(), plan.created_at),
            )
            self._set_plan_applicability(
                con,
                plan.id,
                scenario.id,
                active=True,
                coverage_status=PlanCoverageStatus.current_and_complete,
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
        if run.status in {ScheduleRunStatus.queued, ScheduleRunStatus.running} or run.candidate_id != candidate.id:
            raise PublicationConflict("完成求解记录前必须设置终态和对应候选")
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            run_row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (run.id,)).fetchone()
            if not run_row:
                raise PublicationConflict("求解记录不存在")
            stored = ScheduleRun.model_validate_json(run_row["payload"])
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
        with self._connect() as con:
            row = con.execute("SELECT payload FROM schedule_runs WHERE id=?", (run_id,)).fetchone()
        return ScheduleRun.model_validate_json(row["payload"]) if row else None

    def list_schedule_runs(self, scenario_id: str) -> list[ScheduleRun]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM schedule_runs WHERE scenario_id=? ORDER BY created_at", (scenario_id,)
            ).fetchall()
        return [ScheduleRun.model_validate_json(row["payload"]) for row in rows]

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
        with self._connect() as con:
            row = con.execute("SELECT payload FROM schedule_candidates WHERE id=?", (candidate_id,)).fetchone()
        return ScheduleCandidate.model_validate_json(row["payload"]) if row else None

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
                    SELECT payload FROM decision_analysis_runs
                    WHERE plan_version_id=? AND analysis_type=? AND input_hash=?
                    ORDER BY number DESC LIMIT 1
                    """,
                    (run.plan_version_id, run.analysis_type, run.input_hash),
                ).fetchone()
                if existing:
                    return DecisionAnalysisRun.model_validate_json(existing["payload"]), False
            plan = con.execute(
                "SELECT 1 FROM plan_versions WHERE id=? AND scenario_id=?",
                (run.plan_version_id, run.scenario_id),
            ).fetchone()
            if not plan:
                raise PublicationConflict("经营分析引用的方案不存在")
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
            con.execute(
                """
                INSERT INTO decision_analysis_runs(
                    id, scenario_id, number, plan_version_id, analysis_type, input_hash, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    saved.id,
                    saved.scenario_id,
                    saved.number,
                    saved.plan_version_id,
                    saved.analysis_type,
                    saved.input_hash,
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
                "SELECT payload FROM decision_analysis_runs WHERE id=? AND scenario_id=?",
                (run.id, run.scenario_id),
            ).fetchone()
            if not row:
                raise PublicationConflict("经营分析记录不存在")
            stored = DecisionAnalysisRun.model_validate_json(row["payload"])
            if stored.input_hash != run.input_hash or stored.plan_version_id != run.plan_version_id:
                raise PublicationConflict("经营分析终态与预留输入不一致")
            if stored.status != "RUNNING":
                return self._validate_decision_analysis_integrity(con, stored)
            artifact_manifest: list[dict[str, str]] = []
            for artifact in artifacts or []:
                expected_artifact_hash = content_hash(
                    artifact.model_dump(exclude={"artifact_hash", "integrity_status"}, mode="json")
                )
                if artifact.artifact_hash != expected_artifact_hash:
                    raise PublicationConflict("容量反事实证据指纹不一致")
                artifact_manifest.append(
                    {
                        "artifact_id": artifact.id,
                        "artifact_type": artifact.artifact_type,
                        "artifact_hash": artifact.artifact_hash,
                    }
                )
            run.result_hash = content_hash(run.result) if run.result is not None else None
            run.artifact_manifest = sorted(artifact_manifest, key=lambda item: item["artifact_id"])
            manifest_payload = {
                "policy_version": "FIELD_SERVICE_ANALYSIS_MANIFEST_V1",
                "analysis_id": run.id,
                "input_hash": run.input_hash,
                "result_hash": run.result_hash,
                "artifact_manifest": run.artifact_manifest,
                "status": run.status,
                "build_sha": run.build_sha,
                "algorithm_version": run.algorithm_version,
                "schedule_hash": run.schedule_hash,
            }
            run.analysis_manifest_hash = content_hash(manifest_payload)
            run.integrity_status = AnalysisIntegrityStatus.verified
            con.execute(
                "UPDATE decision_analysis_runs SET payload=? WHERE id=?",
                (run.model_dump_json(), run.id),
            )
            for artifact in artifacts or []:
                if artifact.analysis_run_id != run.id or artifact.scenario_id != run.scenario_id:
                    raise PublicationConflict("容量反事实证据与经营分析不一致")
                con.execute(
                    """
                    INSERT INTO decision_analysis_artifacts(
                        id, scenario_id, analysis_run_id, option_id, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.scenario_id,
                        artifact.analysis_run_id,
                        artifact.option_id if isinstance(artifact, CapacityCounterfactualArtifact) else "SCENARIO_SET",
                        artifact.model_dump_json(),
                        artifact.created_at,
                    ),
                )
        return run

    @staticmethod
    def _validate_decision_analysis_integrity(
        con: sqlite3.Connection,
        run: DecisionAnalysisRun,
    ) -> DecisionAnalysisRun:
        checked = run.model_copy(deep=True)
        if not checked.analysis_manifest_hash:
            checked.integrity_status = AnalysisIntegrityStatus.legacy_unattested
            return checked
        if checked.result_hash != (content_hash(checked.result) if checked.result is not None else None):
            checked.integrity_status = AnalysisIntegrityStatus.failed
            return checked
        artifact_rows = con.execute(
            "SELECT id, payload FROM decision_analysis_artifacts WHERE analysis_run_id=?",
            (checked.id,),
        ).fetchall()
        actual_artifacts: dict[str, str] = {}
        for row in artifact_rows:
            payload = json.loads(row["payload"])
            artifact = (
                SimulationScenarioSetArtifact.model_validate(payload)
                if payload.get("artifact_type") == "SIMULATION_SCENARIO_SET"
                else CapacityCounterfactualArtifact.model_validate(payload)
            )
            expected = content_hash(artifact.model_dump(exclude={"artifact_hash", "integrity_status"}, mode="json"))
            if artifact.artifact_hash != expected:
                artifact.integrity_status = AnalysisIntegrityStatus.failed
                checked.integrity_status = AnalysisIntegrityStatus.failed
                return checked
            artifact.integrity_status = AnalysisIntegrityStatus.verified
            actual_artifacts[row["id"]] = artifact.artifact_hash
        declared = {item.get("artifact_id", ""): item.get("artifact_hash", "") for item in checked.artifact_manifest}
        if declared != actual_artifacts:
            checked.integrity_status = AnalysisIntegrityStatus.failed
            return checked
        manifest_payload = {
            "policy_version": "FIELD_SERVICE_ANALYSIS_MANIFEST_V1",
            "analysis_id": checked.id,
            "input_hash": checked.input_hash,
            "result_hash": checked.result_hash,
            "artifact_manifest": checked.artifact_manifest,
            "status": checked.status,
            "build_sha": checked.build_sha,
            "algorithm_version": checked.algorithm_version,
            "schedule_hash": checked.schedule_hash,
        }
        checked.integrity_status = (
            AnalysisIntegrityStatus.verified
            if content_hash(manifest_payload) == checked.analysis_manifest_hash
            else AnalysisIntegrityStatus.failed
        )
        return checked

    def list_decision_analysis_runs(self, scenario_id: str, plan_version_id: str) -> list[DecisionAnalysisRun]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT payload FROM decision_analysis_runs
                WHERE scenario_id=? AND plan_version_id=? ORDER BY number
                """,
                (scenario_id, plan_version_id),
            ).fetchall()
            return [
                self._validate_decision_analysis_integrity(con, DecisionAnalysisRun.model_validate_json(row["payload"]))
                for row in rows
            ]

    def get_decision_analysis_run(self, scenario_id: str, analysis_id: str) -> DecisionAnalysisRun | None:
        normalized = analysis_id[1:] if analysis_id.upper().startswith("A") else analysis_id
        numeric = int(normalized) if normalized.isdigit() else -1
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload FROM decision_analysis_runs
                WHERE scenario_id=? AND (id=? OR number=?)
                """,
                (scenario_id, analysis_id, numeric),
            ).fetchone()
            return (
                self._validate_decision_analysis_integrity(con, DecisionAnalysisRun.model_validate_json(row["payload"]))
                if row
                else None
            )

    def list_decision_analysis_artifacts(
        self,
        scenario_id: str,
        analysis_run_id: str,
    ) -> list[DecisionAnalysisArtifact]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT payload FROM decision_analysis_artifacts
                WHERE scenario_id=? AND analysis_run_id=? ORDER BY option_id
                """,
                (scenario_id, analysis_run_id),
            ).fetchall()
        artifacts: list[DecisionAnalysisArtifact] = []
        for row in rows:
            payload = json.loads(row["payload"])
            artifact = (
                SimulationScenarioSetArtifact.model_validate(payload)
                if payload.get("artifact_type") == "SIMULATION_SCENARIO_SET"
                else CapacityCounterfactualArtifact.model_validate(payload)
            )
            expected_hash = content_hash(
                artifact.model_dump(exclude={"artifact_hash", "integrity_status"}, mode="json")
            )
            artifact.integrity_status = (
                AnalysisIntegrityStatus.legacy_unattested
                if not artifact.artifact_hash
                else AnalysisIntegrityStatus.verified
                if artifact.artifact_hash == expected_hash
                else AnalysisIntegrityStatus.failed
            )
            artifacts.append(artifact)
        return artifacts

    def get_decision_analysis_artifact(
        self,
        scenario_id: str,
        analysis_run_id: str,
        artifact_id: str,
    ) -> DecisionAnalysisArtifact | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload FROM decision_analysis_artifacts
                WHERE scenario_id=? AND analysis_run_id=? AND id=?
                """,
                (scenario_id, analysis_run_id, artifact_id),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        artifact = (
            SimulationScenarioSetArtifact.model_validate(payload)
            if payload.get("artifact_type") == "SIMULATION_SCENARIO_SET"
            else CapacityCounterfactualArtifact.model_validate(payload)
        )
        expected_hash = content_hash(artifact.model_dump(exclude={"artifact_hash", "integrity_status"}, mode="json"))
        artifact.integrity_status = (
            AnalysisIntegrityStatus.legacy_unattested
            if not artifact.artifact_hash
            else AnalysisIntegrityStatus.verified
            if artifact.artifact_hash == expected_hash
            else AnalysisIntegrityStatus.failed
        )
        return artifact

    def published_for_key(self, key: str, fingerprint: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT request_fingerprint, plan_version_id FROM publication_keys WHERE key=?", (key,)
            ).fetchone()
            if not row:
                return None
            if row["request_fingerprint"] != fingerprint:
                raise PublicationConflict("相同幂等键对应了不同请求")
            plan_row = con.execute("SELECT payload FROM plan_versions WHERE id=?", (row["plan_version_id"],)).fetchone()
            if not plan_row:
                raise PublicationConflict("幂等发布记录引用的方案不存在")
            plan = PlanVersion.model_validate_json(plan_row["payload"])
            return self._overlay_plan_applicability(con, plan)

    def plan_for_publication_key(self, key: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT plan_version_id FROM publication_keys WHERE key=?",
                (key,),
            ).fetchone()
            if not row:
                return None
            plan_row = con.execute(
                "SELECT payload FROM plan_versions WHERE id=?",
                (row["plan_version_id"],),
            ).fetchone()
            if not plan_row:
                raise PublicationConflict("幂等发布记录引用的方案不存在")
            return self._overlay_plan_applicability(con, PlanVersion.model_validate_json(plan_row["payload"]))

    def list_plan_versions(self, scenario_id: str, include_snapshots: bool = False) -> list[PlanVersion]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM plan_versions WHERE scenario_id=? ORDER BY number", (scenario_id,)
            ).fetchall()
            plans = []
            for row in rows:
                plan = self._overlay_plan_applicability(con, PlanVersion.model_validate_json(row["payload"]))
                if not include_snapshots:
                    plan.scenario_snapshot = None
                    plan.artifacts = []
                plans.append(plan)
        return plans

    def get_plan_version(self, scenario_id: str, version_id: str) -> PlanVersion | None:
        normalized = version_id[1:] if version_id.upper().startswith("V") else version_id
        numeric = int(normalized) if normalized.isdigit() else -1
        with self._connect() as con:
            row = con.execute(
                "SELECT payload FROM plan_versions WHERE scenario_id=? AND (id=? OR number=?)",
                (scenario_id, version_id, numeric),
            ).fetchone()
            if not row:
                return None
            return self._overlay_plan_applicability(con, PlanVersion.model_validate_json(row["payload"]))

    def active_plan_version(self, scenario_id: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        return (
            self.get_plan_version(scenario_id, row["active_plan_version_id"])
            if row and row["active_plan_version_id"]
            else None
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
                "SELECT payload FROM plan_versions WHERE scenario_id=? AND id=?", (scenario_id, version_id)
            ).fetchone()
            if not row:
                return None
            current = PlanVersion.model_validate_json(row["payload"])
            plan = PlanVersion.model_validate({**current.model_dump(), "label": label})
            con.execute("UPDATE plan_versions SET payload=? WHERE id=?", (plan.model_dump_json(), plan.id))
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
        return [plan.selected for plan in self.list_plan_versions(scenario_id)]

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
        with self._connect() as con:
            row = con.execute("SELECT payload FROM strategy_experiments WHERE id=?", (experiment_id,)).fetchone()
        return StrategyExperiment.model_validate_json(row["payload"]) if row else None

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
