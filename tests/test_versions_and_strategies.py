import importlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from backend.fixtures import get_fixture
from backend.scheduler import baseline_schedule
from backend.storage import ScenarioRevisionConflict, Store


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
        optimized = client.post("/api/scenarios/main/optimize", json={"strategy": "low_travel", "time_limit_seconds": .05}).json()
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
            "dataset": "current", "profile_ids": ["balanced", "low_travel", "low_overtime"], "time_limit_seconds": .05,
        })
        assert experiment_response.status_code == 202
        duplicate_response = client.post("/api/scenarios/main/strategy-experiments", json={
            "dataset": "current", "profile_ids": ["balanced", "low_travel", "low_overtime"], "time_limit_seconds": .05,
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
        "time_limit_seconds": .1,
    }
    with TestClient(main_module.app) as client:
        created = client.post("/api/strategy-profiles", json=payload)
        assert created.status_code == 201
        profile_id = created.json()["id"]
        payload["name"] = "城郊低行程"
        assert client.put(f"/api/strategy-profiles/{profile_id}", json=payload).json()["name"] == "城郊低行程"
        assert client.delete("/api/strategy-profiles/balanced").status_code == 409

        blocker = client.post("/api/scenarios/main/strategy-experiments", json={"profile_ids": ["fair_workload"], "time_limit_seconds": .05})
        response = client.post("/api/scenarios/main/strategy-experiments", json={"profile_ids": [profile_id], "time_limit_seconds": .05})
        assert response.json()["scenario_id"] == "main"
        assert response.json()["status"] in {"QUEUED", "RUNNING"}
        assert client.delete(f"/api/strategy-profiles/{profile_id}").status_code == 204
        _wait_for_experiment(client, "main", blocker.json()["id"])
        experiment = _wait_for_experiment(client, "main", response.json()["id"])
        assert experiment["status"] == "COMPLETED"
        edited = client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "实验后更新"})
        assert edited.status_code == 200
        rejected = client.post(f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish", json={"candidate_id": experiment["candidates"][0]["id"], "expected_revision": 1})
        assert rejected.status_code == 409


def test_atomic_public_version_allocation(tmp_path):
    store = Store(tmp_path / "atomic.db")
    scenario = get_fixture("main")
    results = [baseline_schedule(scenario, 0) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        plans = list(executor.map(lambda result: store.publish_plan(scenario, result, "baseline"), results))
    assert sorted(plan.number for plan in plans) == [1, 2, 3, 4]
    assert [plan.number for plan in store.list_plan_versions("main")] == [1, 2, 3, 4]


def test_stale_solver_result_is_rejected_without_consuming_a_version(tmp_path):
    store = Store(tmp_path / "stale.db")
    stale = store.get_scenario("main")
    assert stale is not None
    result = baseline_schedule(stale, 0)
    current = stale.model_copy(deep=True)
    current.revision += 1
    current.work_orders[0].note = "并发更新"
    store.save_scenario(current, "并发测试", expected_revision=stale.revision)
    with pytest.raises(ScenarioRevisionConflict):
        store.publish_plan(stale, result, "baseline")
    assert store.list_plan_versions("main") == []


def test_concurrent_business_edits_are_cas_protected_and_expire_current_plan(tmp_path):
    store = Store(tmp_path / "revision-race.db")
    original = store.get_scenario("main")
    assert original is not None
    store.publish_plan(original, baseline_schedule(original, 0), "baseline")

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
    with sqlite3.connect(database) as connection:
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
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT id, version FROM schedules").fetchall() == [("OLD-1", 17)]
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    with sqlite3.connect(database) as migrated:
        assert migrated.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
        assert migrated.execute("SELECT active_plan_version_id FROM scenarios WHERE id='main'").fetchone()[0] is None
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.list_plan_versions("main") == []
