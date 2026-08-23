from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .fixtures import all_fixtures
from .models import (
    PlanVersion,
    ScenarioRevision,
    ScheduleArtifact,
    ScheduleResult,
    ScheduleScenario,
    StrategyExperiment,
    StrategyProfile,
    StrategyProfileCreate,
    StrategyWeights,
)


SCHEMA_VERSION = 2


class ScenarioRevisionConflict(RuntimeError):
    def __init__(self, expected: int, current: int):
        super().__init__(f"scenario revision changed: expected {expected}, current {current}")
        self.expected = expected
        self.current = current


class PublicationConflict(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


BUILTIN_PROFILES: tuple[StrategyProfile, ...] = (
    StrategyProfile(id="balanced", name="均衡", description="兼顾完成率、准时、行程和加班", builtin=True, weights=StrategyWeights(overtime_weight=30), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="completion", name="完成率优先", description="显著提高未分配代价，接受更多行程和加班以完成工单", builtin=True, weights=StrategyWeights(travel_weight=1, sla_late_weight=1, overtime_weight=1, imbalance_weight=1, replan_change_weight=60, unassigned_penalty_scale=5), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="punctuality", name="准时优先", description="显著提高 SLA 延迟代价，必要时减少接单", builtin=True, weights=StrategyWeights(travel_weight=2, sla_late_weight=200, overtime_weight=30, imbalance_weight=2, replan_change_weight=100, unassigned_penalty_scale=1), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="low_travel", name="低行程", description="优先压缩跨区域移动，接受少量完成率取舍", builtin=True, weights=StrategyWeights(travel_weight=30, sla_late_weight=8, overtime_weight=8, imbalance_weight=1, replan_change_weight=80, unassigned_penalty_scale=.8), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="low_overtime", name="低加班", description="强约束路线在正常班次内结束", builtin=True, weights=StrategyWeights(travel_weight=2, sla_late_weight=5, overtime_weight=500, imbalance_weight=1, replan_change_weight=90, unassigned_penalty_scale=.5), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="fair_workload", name="工作量公平", description="以适度完成率和行程取舍降低技师服务量差异", builtin=True, weights=StrategyWeights(travel_weight=3, sla_late_weight=10, overtime_weight=8, imbalance_weight=10, replan_change_weight=90, unassigned_penalty_scale=1.3), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
    StrategyProfile(id="stable", name="稳定优先", description="局部重排时尽量保留技师和顺序", builtin=True, weights=StrategyWeights(travel_weight=4, sla_late_weight=16, overtime_weight=10, imbalance_weight=2, replan_change_weight=260, unassigned_penalty_scale=1), time_limit_seconds=2, created_at="2026-08-23T00:00:00+00:00"),
)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _backup_legacy(self, con: sqlite3.Connection) -> Path | None:
        row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedules'").fetchone()
        if not row or not con.execute("SELECT 1 FROM schedules LIMIT 1").fetchone():
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

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as con:
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                self._backup_legacy(con)
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    active_plan_version_id TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS scenario_revisions (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number)
                );
                CREATE TABLE IF NOT EXISTS plan_versions (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scenario_id, number)
                );
                CREATE TABLE IF NOT EXISTS schedule_artifacts (
                    id TEXT PRIMARY KEY,
                    plan_version_id TEXT,
                    experiment_id TEXT,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_keys (
                    key TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    plan_version_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_scenario ON schedules(scenario_id, version);
                CREATE INDEX IF NOT EXISTS idx_plan_versions_scenario ON plan_versions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_revisions_scenario ON scenario_revisions(scenario_id, number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_plan ON schedule_artifacts(plan_version_id);
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(scenarios)")}
            if "active_plan_version_id" not in columns:
                con.execute("ALTER TABLE scenarios ADD COLUMN active_plan_version_id TEXT")
            if version < SCHEMA_VERSION:
                # Legacy schedules lack a restorable business snapshot. The user chose a clean restart.
                con.execute("DELETE FROM schedules")
                con.execute("DELETE FROM plan_versions")
                con.execute("DELETE FROM schedule_artifacts")
                con.execute("DELETE FROM strategy_experiments")
                con.execute("DELETE FROM publication_keys")
                con.execute("UPDATE scenarios SET active_plan_version_id=NULL")
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.execute("UPDATE strategy_experiments SET payload=json_set(payload, '$.status', 'INTERRUPTED', '$.error', '应用重启，实验已中断') WHERE json_extract(payload, '$.status') IN ('QUEUED', 'RUNNING')")
        self.seed_fixtures()
        self.seed_profiles()

    def seed_fixtures(self) -> None:
        with self._lock, self._connect() as con:
            for scenario in all_fixtures().values():
                con.execute("INSERT OR IGNORE INTO scenarios(id, payload) VALUES (?, ?)", (scenario.id, scenario.model_dump_json()))
                self._insert_revision(con, scenario, "内置场景初始化", ignore=True)

    def seed_profiles(self) -> None:
        with self._lock, self._connect() as con:
            for profile in BUILTIN_PROFILES:
                con.execute(
                    "INSERT INTO strategy_profiles(id, builtin, payload, created_at) VALUES (?, 1, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, builtin=1",
                    (profile.id, profile.model_dump_json(), profile.created_at),
                )

    @staticmethod
    def _insert_revision(con: sqlite3.Connection, scenario: ScheduleScenario, reason: str, ignore: bool = False) -> ScenarioRevision:
        revision = ScenarioRevision(id=f"REV-{scenario.id}-{scenario.revision}-{uuid.uuid4().hex[:6]}", scenario_id=scenario.id, number=scenario.revision, reason=reason, scenario=scenario.model_copy(deep=True), created_at=_now())
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        con.execute(f"{verb} INTO scenario_revisions(id, scenario_id, number, reason, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)", (revision.id, revision.scenario_id, revision.number, reason, revision.model_dump_json(), revision.created_at))
        return revision

    def list_scenarios(self) -> list[ScheduleScenario]:
        with self._connect() as con:
            rows = con.execute("SELECT payload FROM scenarios ORDER BY id").fetchall()
        return [self._upgrade_scenario(ScheduleScenario.model_validate_json(row["payload"])) for row in rows]

    def get_scenario(self, scenario_id: str) -> ScheduleScenario | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM scenarios WHERE id = ?", (scenario_id,)).fetchone()
        return self._upgrade_scenario(ScheduleScenario.model_validate_json(row["payload"])) if row else None

    @staticmethod
    def _upgrade_scenario(scenario: ScheduleScenario) -> ScheduleScenario:
        config = scenario.solver_config
        if (config.travel_weight, config.sla_late_weight, config.overtime_weight, config.imbalance_weight) in {(1, 8, 4, 2), (4, 12, 8, 1), (4, 12, 12, 1)}:
            config.travel_weight, config.sla_late_weight, config.overtime_weight, config.imbalance_weight = (4, 12, 30, 1)
        for order in scenario.work_orders:
            if order.id.startswith("WO-EMG-") and not order.is_emergency:
                order.is_emergency = True
                order.reported_at = order.reported_at or min(order.window_start, 600)
                order.drop_penalty = max(order.drop_penalty, 8000)
        return scenario

    def save_scenario(self, scenario: ScheduleScenario, reason: str = "业务数据更新", *, expected_revision: int | None = None) -> None:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM scenarios WHERE id=?", (scenario.id,)).fetchone()
            current_revision = ScheduleScenario.model_validate_json(row["payload"]).revision if row else -1
            if expected_revision is None:
                if row:
                    raise ScenarioRevisionConflict(-1, current_revision)
                con.execute("INSERT INTO scenarios(id, payload, active_plan_version_id, updated_at) VALUES (?, ?, NULL, CURRENT_TIMESTAMP)", (scenario.id, scenario.model_dump_json()))
            else:
                if not row or current_revision != expected_revision:
                    raise ScenarioRevisionConflict(expected_revision, current_revision)
                # A data edit makes the selected plan stale but preserves all
                # published history. Replanning can still use the latest plan.
                con.execute("UPDATE scenarios SET payload=?, active_plan_version_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (scenario.model_dump_json(), scenario.id))
            self._insert_revision(con, scenario, reason)

    def list_revisions(self, scenario_id: str) -> list[ScenarioRevision]:
        with self._connect() as con:
            rows = con.execute("SELECT payload FROM scenario_revisions WHERE scenario_id=? ORDER BY number", (scenario_id,)).fetchall()
        return [ScenarioRevision.model_validate_json(row["payload"]) for row in rows]

    def next_version(self, scenario_id: str) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COALESCE(MAX(number), 0) AS v FROM plan_versions WHERE scenario_id=?", (scenario_id,)).fetchone()
        return int(row["v"]) + 1

    @staticmethod
    def _version_label(action: str, strategy: str, number: int) -> str:
        names = {"baseline": "人工基线", "balanced": "均衡优化", "completion": "完成率优先", "punctuality": "准时优先", "low_travel": "低行程", "low_overtime": "低加班", "fair_workload": "工作量公平", "stable": "稳定重排", "custom": "自定义策略"}
        return f"历史恢复 V{number:03d}" if action == "restore" else names.get(strategy, action)

    def publish_plan(
        self,
        scenario: ScheduleScenario,
        selected: ScheduleResult,
        action: str,
        *,
        artifacts: list[ScheduleArtifact] | None = None,
        source_version_id: str | None = None,
        relation: str = "new",
        label: str | None = None,
        replace_scenario: bool = False,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> PlanVersion:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                publication = con.execute("SELECT request_fingerprint, plan_version_id FROM publication_keys WHERE key=?", (idempotency_key,)).fetchone()
                if publication:
                    if publication["request_fingerprint"] != (request_fingerprint or ""):
                        raise PublicationConflict("同一实验已经发布了另一个候选方案")
                    existing = con.execute("SELECT payload FROM plan_versions WHERE id=?", (publication["plan_version_id"],)).fetchone()
                    if not existing:
                        raise PublicationConflict("幂等发布记录引用的方案不存在")
                    plan = PlanVersion.model_validate_json(existing["payload"])
                    active = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (plan.scenario_id,)).fetchone()
                    plan.active = bool(active and active["active_plan_version_id"] == plan.id)
                    return plan
            current_row = con.execute("SELECT payload FROM scenarios WHERE id=?", (scenario.id,)).fetchone()
            if not current_row:
                raise KeyError(f"scenario {scenario.id} not found")
            current_revision = ScheduleScenario.model_validate_json(current_row["payload"]).revision
            required_revision = expected_revision if expected_revision is not None else (scenario.revision - 1 if replace_scenario else scenario.revision)
            if current_revision != required_revision:
                raise ScenarioRevisionConflict(required_revision, current_revision)
            row = con.execute("SELECT COALESCE(MAX(number), 0) + 1 AS v FROM plan_versions WHERE scenario_id=?", (scenario.id,)).fetchone()
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
                persisted_artifacts.append(ScheduleArtifact(id=f"ART-{uuid.uuid4().hex[:10]}", role="selected", strategy=chosen.strategy, schedule=chosen))
            plan = PlanVersion(id=plan_id, scenario_id=scenario.id, number=number, action=action, label=label or self._version_label(action, chosen.strategy, number), data_revision=scenario.revision, source_version_id=source_version_id, relation=relation, active=True, created_at=_now(), scenario_snapshot=scenario.model_copy(deep=True), selected=chosen, artifacts=persisted_artifacts)
            con.execute("UPDATE plan_versions SET payload=json_set(payload, '$.active', 0) WHERE scenario_id=?", (scenario.id,))
            con.execute("INSERT INTO plan_versions(id, scenario_id, number, payload, created_at) VALUES (?, ?, ?, ?, ?)", (plan.id, plan.scenario_id, number, plan.model_dump_json(), plan.created_at))
            con.execute("INSERT INTO schedules(id, scenario_id, kind, version, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)", (chosen.id, chosen.scenario_id, chosen.kind, number, chosen.model_dump_json(), chosen.created_at))
            for artifact in persisted_artifacts:
                con.execute("INSERT OR REPLACE INTO schedule_artifacts(id, plan_version_id, experiment_id, role, payload, created_at) VALUES (?, ?, NULL, ?, ?, ?)", (artifact.id, plan.id, artifact.role, artifact.model_dump_json(), plan.created_at))
            if replace_scenario:
                con.execute("INSERT INTO scenarios(id, payload, active_plan_version_id, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, active_plan_version_id=excluded.active_plan_version_id, updated_at=CURRENT_TIMESTAMP", (scenario.id, scenario.model_dump_json(), plan.id))
                self._insert_revision(con, scenario, f"恢复方案 V{number:03d}")
            else:
                con.execute("UPDATE scenarios SET active_plan_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (plan.id, scenario.id))
            if idempotency_key:
                con.execute("INSERT INTO publication_keys(key, request_fingerprint, plan_version_id, created_at) VALUES (?, ?, ?, ?)", (idempotency_key, request_fingerprint or "", plan.id, plan.created_at))
            return plan

    def list_plan_versions(self, scenario_id: str, include_snapshots: bool = False) -> list[PlanVersion]:
        with self._connect() as con:
            rows = con.execute("SELECT payload FROM plan_versions WHERE scenario_id=? ORDER BY number", (scenario_id,)).fetchall()
            active = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        active_id = active["active_plan_version_id"] if active else None
        plans = []
        for row in rows:
            plan = PlanVersion.model_validate_json(row["payload"])
            plan.active = plan.id == active_id
            if not include_snapshots:
                plan.scenario_snapshot = None
                plan.artifacts = []
            plans.append(plan)
        return plans

    def get_plan_version(self, scenario_id: str, version_id: str) -> PlanVersion | None:
        normalized = version_id[1:] if version_id.upper().startswith("V") else version_id
        numeric = int(normalized) if normalized.isdigit() else -1
        with self._connect() as con:
            row = con.execute("SELECT payload FROM plan_versions WHERE scenario_id=? AND (id=? OR number=?)", (scenario_id, version_id, numeric)).fetchone()
            active = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if not row:
            return None
        plan = PlanVersion.model_validate_json(row["payload"])
        plan.active = bool(active and active["active_plan_version_id"] == plan.id)
        return plan

    def active_plan_version(self, scenario_id: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute("SELECT active_plan_version_id FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        return self.get_plan_version(scenario_id, row["active_plan_version_id"]) if row and row["active_plan_version_id"] else None

    def latest_plan_version(self, scenario_id: str) -> PlanVersion | None:
        with self._connect() as con:
            row = con.execute("SELECT id FROM plan_versions WHERE scenario_id=? ORDER BY number DESC LIMIT 1", (scenario_id,)).fetchone()
        return self.get_plan_version(scenario_id, row["id"]) if row else None

    def rename_plan_version(self, scenario_id: str, version_id: str, label: str) -> PlanVersion | None:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT payload FROM plan_versions WHERE scenario_id=? AND id=?", (scenario_id, version_id)).fetchone()
            if not row:
                return None
            plan = PlanVersion.model_validate_json(row["payload"])
            plan.label = label.strip()
            con.execute("UPDATE plan_versions SET payload=? WHERE id=?", (plan.model_dump_json(), plan.id))
        return self.get_plan_version(scenario_id, version_id)

    def save_schedule(self, result: ScheduleResult) -> None:
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO schedules(id, scenario_id, kind, version, payload) VALUES (?, ?, ?, ?, ?)", (result.id, result.scenario_id, result.kind, result.version, result.model_dump_json()))

    def get_schedule(self, schedule_id: str) -> ScheduleResult | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return ScheduleResult.model_validate_json(row["payload"]) if row else None

    def latest_schedule(self, scenario_id: str, kind: str | None = None, scenario_revision: int | None = None) -> ScheduleResult | None:
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
        profile = StrategyProfile(id=identifier, name=request.name.strip(), description=request.description.strip(), builtin=False, weights=request.weights, time_limit_seconds=request.time_limit_seconds, created_at=existing.created_at if existing else _now())
        with self._lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO strategy_profiles(id, builtin, payload, created_at) VALUES (?, 0, ?, ?)", (profile.id, profile.model_dump_json(), profile.created_at))
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
            con.execute("INSERT OR REPLACE INTO strategy_experiments(id, scenario_id, payload, created_at) VALUES (?, ?, ?, ?)", (experiment.id, experiment.scenario_id, experiment.model_dump_json(), experiment.created_at))
            for candidate in experiment.candidates:
                artifact = ScheduleArtifact(id=candidate.id, role="candidate", strategy=candidate.profile_id, schedule=candidate.schedule)
                con.execute("INSERT OR REPLACE INTO schedule_artifacts(id, plan_version_id, experiment_id, role, payload, created_at) VALUES (?, NULL, ?, 'candidate', ?, ?)", (artifact.id, experiment.id, artifact.model_dump_json(), experiment.created_at))

    def queue_experiment(self, experiment: StrategyExperiment) -> tuple[StrategyExperiment, bool]:
        """Persist a queued experiment, coalescing an identical active request."""
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT payload FROM strategy_experiments WHERE scenario_id=? ORDER BY created_at DESC", (experiment.scenario_id,)).fetchall()
            for row in rows:
                existing = StrategyExperiment.model_validate_json(row["payload"])
                if (
                    existing.status in {"QUEUED", "RUNNING"}
                    and existing.data_revision == experiment.data_revision
                    and existing.profile_ids == experiment.profile_ids
                    and existing.requested_time_limit_seconds == experiment.requested_time_limit_seconds
                ):
                    return existing, False
            con.execute("INSERT INTO strategy_experiments(id, scenario_id, payload, created_at) VALUES (?, ?, ?, ?)", (experiment.id, experiment.scenario_id, experiment.model_dump_json(), experiment.created_at))
            return experiment, True

    def get_experiment(self, experiment_id: str) -> StrategyExperiment | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM strategy_experiments WHERE id=?", (experiment_id,)).fetchone()
        return StrategyExperiment.model_validate_json(row["payload"]) if row else None
