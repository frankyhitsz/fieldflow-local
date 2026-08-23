import importlib
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from backend.fixtures import get_fixture
from backend.hashing import content_hash
from backend.models import ScheduleCandidate, ScheduleRun, ScheduleRunStatus
from backend.scheduler import baseline_schedule
from backend.storage import PublicationConflict, ScenarioRevisionConflict, Store
from backend.verification import verify_schedule


def _verified_candidate(store: Store, scenario, result) -> str:
    now = datetime.now(UTC).isoformat()
    suffix = uuid.uuid4().hex[:10]
    run = ScheduleRun(
        id=f"RUN-{suffix}", scenario_id=scenario.id, action="baseline",
        scenario_revision=scenario.revision, scenario_snapshot_hash=content_hash(scenario),
        solver_name=result.solver_name, solver_version=result.solver_version,
        solver_config_hash=result.solver_config_hash, requested_time_limit_ms=0,
        effective_time_limit_ms=0, status=ScheduleRunStatus.feasible,
        solution_found=True, started_at=now, finished_at=now,
    )
    store.save_schedule_run(run)
    report = verify_schedule(scenario, result)
    candidate = ScheduleCandidate(
        id=f"CAND-{suffix}", run_id=run.id, scenario_id=scenario.id,
        scenario_revision=scenario.revision, scenario_snapshot_hash=content_hash(scenario),
        solver_config_hash=result.solver_config_hash, schedule=result,
        verification_report=report, publishable=report.publishable, created_at=now,
    )
    store.save_schedule_candidate(candidate)
    return candidate.id


def _wait_for_experiment(client: TestClient, scenario_id: str, experiment_id: str) -> dict:
    for _ in range(120):
        payload = client.get(f"/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}").json()
        if payload["status"] not in {"QUEUED", "RUNNING"}:
            return payload
        time.sleep(.05)
    raise AssertionError("strategy experiment did not finish")


def test_restore_is_non_destructive_and_experiment_candidates_do_not_consume_versions(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "versions.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        optimized = client.post("/api/scenarios/main/optimize", json={"strategy": "low_travel", "time_limit_seconds": 1}).json()
        assert (baseline["version"], optimized["version"]) == (1, 2)

        edited = client.put("/api/scenarios/main/work-orders/WO-1021", json={"title": "临时修改"}).json()
        assert edited["revision"] == 1

        plans = client.get("/api/scenarios/main/plan-versions").json()
        restored_response = client.post(
            f"/api/scenarios/main/plan-versions/{plans[0]['id']}/restore",
            json={"expected_revision": 1},
        )
        assert restored_response.status_code == 200
        restored = restored_response.json()
        assert restored["number"] == 3
        assert restored["action"] == "restore"
        assert restored["source_version_id"] == plans[0]["id"]
        scenario = client.get("/api/scenarios/main").json()
        assert scenario["revision"] == 2
        assert next(item for item in scenario["work_orders"] if item["id"] == "WO-1021")["title"] != "临时修改"
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]

        renamed = client.patch(f"/api/scenarios/main/plan-versions/{restored['id']}", json={"label": "午前恢复方案"})
        assert renamed.status_code == 200
        assert renamed.json()["label"] == "午前恢复方案"
        assert client.get(f"/api/scenarios/main/comparison?before={plans[0]['id']}&after={plans[1]['id']}").status_code == 200
        assert client.get(f"/api/scenarios/main/plan-versions/{restored['id']}/report").status_code == 200

        profiles = client.get("/api/strategy-profiles").json()
        assert {"low_overtime", "fair_workload", "stable"}.issubset({item["id"] for item in profiles})
        experiment_response = client.post("/api/scenarios/main/strategy-experiments", json={
            "dataset": "current", "profile_ids": ["balanced", "low_travel", "low_overtime"], "time_limit_seconds": 1,
        })
        assert experiment_response.status_code == 202
        duplicate_response = client.post("/api/scenarios/main/strategy-experiments", json={
            "dataset": "current", "profile_ids": ["balanced", "low_travel", "low_overtime"], "time_limit_seconds": 1,
        })
        assert duplicate_response.status_code == 202
        assert duplicate_response.json()["id"] == experiment_response.json()["id"]
        experiment = _wait_for_experiment(client, "main", experiment_response.json()["id"])
        assert experiment["status"] == "COMPLETED"
        assert len(experiment["candidates"]) == 3
        assert len({tuple((item["work_order_id"], item["technician_id"], item["sequence"]) for item in candidate["schedule"]["assignments"]) for candidate in experiment["candidates"]}) >= 2
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]

        candidate = next(item for item in experiment["candidates"] if item["publishable"])
        published = client.post(f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish", json={"candidate_id": candidate["id"], "expected_revision": 2})
        assert published.status_code == 200
        assert published.json()["number"] == 4
        retried = client.post(f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish", json={"candidate_id": candidate["id"], "expected_revision": 2})
        assert retried.status_code == 200
        assert retried.json()["id"] == published.json()["id"]
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3, 4]
        another = next((item for item in experiment["candidates"] if item["id"] != candidate["id"] and item["publishable"]), None)
        if another:
            rejected_second_choice = client.post(f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish", json={"candidate_id": another["id"], "expected_revision": 2})
            assert rejected_second_choice.status_code == 409


def test_custom_profile_crud_and_stale_experiment_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "profiles.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)
    payload = {
        "name": "城郊节油", "description": "长距离区域减少往返",
        "weights": {"travel_weight": 20, "sla_late_weight": 8, "overtime_weight": 6, "imbalance_weight": 2, "replan_change_weight": 80, "unassigned_penalty_scale": .8},
        "time_limit_seconds": 1,
    }
    with TestClient(main_module.app) as client:
        created = client.post("/api/strategy-profiles", json=payload)
        assert created.status_code == 201
        profile_id = created.json()["id"]
        payload["name"] = "城郊低行程"
        assert client.put(f"/api/strategy-profiles/{profile_id}", json=payload).json()["name"] == "城郊低行程"
        assert client.delete("/api/strategy-profiles/balanced").status_code == 409

        blocker = client.post("/api/scenarios/main/strategy-experiments", json={"profile_ids": ["fair_workload"], "time_limit_seconds": 1})
        response = client.post("/api/scenarios/main/strategy-experiments", json={"profile_ids": [profile_id], "time_limit_seconds": 1})
        assert response.json()["scenario_id"] == "main"
        assert response.json()["status"] in {"QUEUED", "RUNNING"}
        payload["weights"]["travel_weight"] = 21
        assert client.put(f"/api/strategy-profiles/{profile_id}", json=payload).status_code == 200
        changed_profile_experiment = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"profile_ids": [profile_id], "time_limit_seconds": 1},
        )
        assert changed_profile_experiment.status_code == 202
        assert changed_profile_experiment.json()["id"] != response.json()["id"]
        assert changed_profile_experiment.json()["fingerprint"] != response.json()["fingerprint"]
        assert client.delete(f"/api/strategy-profiles/{profile_id}").status_code == 204
        _wait_for_experiment(client, "main", blocker.json()["id"])
        experiment = _wait_for_experiment(client, "main", response.json()["id"])
        _wait_for_experiment(client, "main", changed_profile_experiment.json()["id"])
        assert experiment["status"] == "COMPLETED"
        edited = client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "实验后更新"})
        assert edited.status_code == 200
        rejected = client.post(f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish", json={"candidate_id": experiment["candidates"][0]["id"], "expected_revision": 1})
        assert rejected.status_code == 409


def test_atomic_public_version_allocation(tmp_path):
    store = Store(tmp_path / "atomic.db")
    scenario = get_fixture("main")
    results = [baseline_schedule(scenario, 0) for _ in range(4)]
    candidate_ids = [_verified_candidate(store, scenario, result) for result in results]
    with ThreadPoolExecutor(max_workers=4) as executor:
        plans = list(executor.map(lambda pair: store.publish_plan(scenario, pair[0], "baseline", candidate_id=pair[1]), zip(results, candidate_ids, strict=False)))
    assert sorted(plan.number for plan in plans) == [1, 2, 3, 4]
    assert [plan.number for plan in store.list_plan_versions("main")] == [1, 2, 3, 4]


def test_stale_solver_result_is_rejected_without_consuming_a_version(tmp_path):
    store = Store(tmp_path / "stale.db")
    stale = store.get_scenario("main")
    assert stale is not None
    result = baseline_schedule(stale, 0)
    candidate_id = _verified_candidate(store, stale, result)
    current = stale.model_copy(deep=True)
    current.revision += 1
    current.work_orders[0].note = "并发更新"
    store.save_scenario(current, "并发测试", expected_revision=stale.revision)
    with pytest.raises(ScenarioRevisionConflict):
        store.publish_plan(stale, result, "baseline", candidate_id=candidate_id)
    assert store.list_plan_versions("main") == []


def test_concurrent_business_edits_are_cas_protected_and_expire_current_plan(tmp_path):
    store = Store(tmp_path / "revision-race.db")
    original = store.get_scenario("main")
    assert original is not None
    initial = baseline_schedule(original, 0)
    store.publish_plan(original, initial, "baseline", candidate_id=_verified_candidate(store, original, initial))

    variants = []
    for note in ("调度员甲", "调度员乙"):
        variant = original.model_copy(deep=True)
        variant.revision += 1
        variant.work_orders[0].note = note
        variants.append(variant)

    outcomes: list[str] = []
    def save(variant):
        try:
            store.save_scenario(variant, "并发编辑", expected_revision=original.revision)
            return "saved"
        except ScenarioRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, variants))

    assert sorted(outcomes) == ["conflict", "saved"]
    assert store.get_scenario("main").revision == 1
    assert [revision.number for revision in store.list_revisions("main")] == [0, 1]
    assert store.active_plan_version("main") is None
    assert store.latest_plan_version("main").number == 1


def test_legacy_history_is_backed_up_before_one_time_rebuild(tmp_path):
    database = tmp_path / "fieldflow.db"
    legacy_scenario = get_fixture("main")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE scenarios (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE schedules (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            PRAGMA user_version = 1;
            """
        )
        connection.execute("INSERT INTO scenarios(id, payload) VALUES (?, ?)", (legacy_scenario.id, legacy_scenario.model_dump_json()))
        connection.execute("INSERT INTO schedules(id, scenario_id, kind, version, payload) VALUES ('OLD-1', 'main', 'optimized', 17, '{}')")

    store = Store(database)
    backups = list(tmp_path.glob("fieldflow.legacy-*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup, backup:
        assert backup.execute("SELECT id, version FROM schedules").fetchall() == [("OLD-1", 17)]
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    with closing(sqlite3.connect(database)) as migrated, migrated:
        assert migrated.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
        assert migrated.execute("SELECT active_plan_version_id FROM scenarios WHERE id='main'").fetchone()[0] is None
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migrated.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.list_plan_versions("main") == []


def test_relational_schema_enforces_foreign_keys_and_artifact_parent(tmp_path):
    database = tmp_path / "integrity.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_list(plan_versions)").fetchall()
        assert connection.execute("PRAGMA foreign_key_list(schedule_artifacts)").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO publication_keys VALUES ('orphan', 'x', 'missing-plan', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO schedule_artifacts VALUES ('bad-parent', NULL, NULL, 'candidate', '{}', 'now')"
            )


def test_v3_to_v4_migration_preserves_plan_history(tmp_path):
    database = tmp_path / "preserve-v3.db"
    original_store = Store(database)
    scenario = original_store.get_scenario("main")
    assert scenario is not None
    result = baseline_schedule(scenario, 0)
    published = original_store.publish_plan(
        scenario,
        result,
        "baseline",
        candidate_id=_verified_candidate(original_store, scenario, result),
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO schedule_artifacts VALUES ('orphan-artifact', 'missing-plan', NULL, 'candidate', '{}', 'now')"
        )
        connection.execute("PRAGMA user_version=3")

    migrated_store = Store(database)
    plans = migrated_store.list_plan_versions("main")
    assert [(item.id, item.number) for item in plans] == [(published.id, 1)]
    assert migrated_store.active_plan_version("main").id == published.id
    assert list(tmp_path.glob("preserve-v3.legacy-*.db"))
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT source_id FROM migration_orphans WHERE source_table='schedule_artifacts'"
        ).fetchall() == [("orphan-artifact",)]


def test_plan_publication_rolls_back_as_one_transaction(tmp_path):
    database = tmp_path / "publication-rollback.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    first = baseline_schedule(scenario, 0)
    first_plan = store.publish_plan(
        scenario,
        first,
        "baseline",
        candidate_id=_verified_candidate(store, scenario, first),
    )
    second = baseline_schedule(scenario, 0)
    second_candidate = _verified_candidate(store, scenario, second)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_second_plan BEFORE INSERT ON plan_versions "
            "WHEN NEW.number=2 BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced rollback"):
        store.publish_plan(scenario, second, "baseline", candidate_id=second_candidate)
    assert [item.number for item in store.list_plan_versions("main")] == [1]
    assert [item.id for item in store.list_schedules("main")] == [first_plan.selected.id]
    assert store.active_plan_version("main").id == first_plan.id


def test_storage_rechecks_candidate_instead_of_trusting_saved_report(tmp_path):
    store = Store(tmp_path / "forged-report.db")
    scenario = store.get_scenario("main")
    assert scenario is not None
    original = baseline_schedule(scenario, 0)
    candidate_id = _verified_candidate(store, scenario, original)
    candidate = store.get_schedule_candidate(candidate_id)
    assert candidate is not None and candidate.publishable
    forged = original.model_copy(deep=True)
    forged.assignments.pop()
    candidate.schedule = forged
    store.save_schedule_candidate(candidate)
    with pytest.raises(PublicationConflict, match="发布事务复核失败"):
        store.publish_plan(scenario, forged, "baseline", candidate_id=candidate.id)
    assert store.list_plan_versions("main") == []
