import importlib
import sqlite3
import threading
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
        id=f"RUN-{suffix}",
        scenario_id=scenario.id,
        action="baseline",
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        solver_name=result.solver_name,
        solver_version=result.solver_version,
        solver_config_hash=result.solver_config_hash,
        solver_policy_fingerprint=result.solver_policy.fingerprint,
        requested_time_limit_ms=0,
        effective_time_limit_ms=0,
        status=ScheduleRunStatus.running,
        solution_found=False,
        started_at=now,
    )
    store.save_schedule_run(run)
    report = verify_schedule(scenario, result)
    candidate = ScheduleCandidate(
        id=f"CAND-{suffix}",
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        solver_config_hash=result.solver_config_hash,
        solver_policy_fingerprint=result.solver_policy.fingerprint,
        schedule=result,
        verification_report=report,
        publishable=report.publishable,
        created_at=now,
    )
    run.status = ScheduleRunStatus.feasible
    run.solution_found = True
    run.finished_at = now
    run.candidate_id = candidate.id
    store.complete_schedule_run(run, candidate)
    return candidate.id


def _wait_for_experiment(client: TestClient, scenario_id: str, experiment_id: str) -> dict:
    for _ in range(120):
        payload = client.get(f"/api/scenarios/{scenario_id}/strategy-experiments/{experiment_id}").json()
        if payload["status"] not in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("strategy experiment did not finish")


def test_restore_is_non_destructive_and_experiment_candidates_do_not_consume_versions(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "versions.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        baseline = client.post("/api/scenarios/main/baseline").json()
        optimized = client.post(
            "/api/scenarios/main/optimize", json={"strategy": "low_travel", "time_limit_seconds": 1}
        ).json()
        assert (baseline["version"], optimized["version"]) == (1, 2)
        low_travel_profile = next(
            item for item in main_module.require_store().list_profiles() if item.id == "low_travel"
        )
        low_travel_profile.time_limit_seconds = 1
        expected_solver_hash = content_hash(
            main_module.scenario_for_profile(get_fixture("main"), low_travel_profile).solver_config
        )
        assert optimized["solver_config_hash"] == expected_solver_hash
        assert optimized["solver_config_hash"] != content_hash(get_fixture("main").solver_config)
        optimized_plan = client.get("/api/scenarios/main/plan-versions/V002").json()
        internal_baseline = next(item["schedule"] for item in optimized_plan["artifacts"] if item["role"] == "baseline")
        assert internal_baseline["solver_config_hash"] == expected_solver_hash
        assert internal_baseline["scenario_snapshot_hash"] == optimized["scenario_snapshot_hash"]

        edited = client.put(
            "/api/scenarios/main/work-orders/WO-1021",
            headers={"If-Match": "D0"},
            json={"title": "临时修改"},
        ).json()
        assert edited["revision"] == 1

        plans = client.get("/api/scenarios/main/plan-versions").json()
        preview = client.get(f"/api/scenarios/main/plan-versions/{plans[0]['id']}/rollback-preview").json()
        assert preview["current_plan_version_id"] == plans[1]["id"]
        assert preview["current_plan_number"] == 2
        assert isinstance(preview["changed_plan_work_orders"], list)
        restored_response = client.post(
            f"/api/scenarios/main/plan-versions/{plans[0]['id']}/restore",
            json={
                "expected_revision": 1,
                "confirmation_token": preview["confirmation_token"],
                "reason": "撤销错误录入",
                "idempotency_key": "rollback-version-001",
            },
        )
        assert restored_response.status_code == 200
        restored = restored_response.json()
        assert restored["number"] == 3
        assert restored["action"] == "restore"
        assert restored["source_version_id"] == plans[0]["id"]
        assert restored["selected"]["assignments"] == baseline["assignments"]
        assert restored["selected"]["unassigned"] == baseline["unassigned"]
        assert restored["selected"]["kpis"] == baseline["kpis"]
        scenario = client.get("/api/scenarios/main").json()
        assert scenario["revision"] == 2
        assert next(item for item in scenario["work_orders"] if item["id"] == "WO-1021")["title"] != "临时修改"
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]
        retried_restore = client.post(
            f"/api/scenarios/main/plan-versions/{plans[0]['id']}/restore",
            json={
                "expected_revision": 1,
                "confirmation_token": preview["confirmation_token"],
                "reason": "撤销错误录入",
                "idempotency_key": "rollback-version-001",
            },
        )
        assert retried_restore.status_code == 200
        assert retried_restore.json()["id"] == restored["id"]
        assert client.get("/api/scenarios/main").json()["revision"] == 2
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]

        renamed = client.patch(f"/api/scenarios/main/plan-versions/{restored['id']}", json={"label": "午前恢复方案"})
        assert renamed.status_code == 200
        assert renamed.json()["label"] == "午前恢复方案"
        assert (
            client.get(f"/api/scenarios/main/comparison?before={plans[0]['id']}&after={plans[1]['id']}").status_code
            == 200
        )
        selected_comparison = client.get(f"/api/scenarios/main/comparison?after={plans[1]['id']}")
        assert selected_comparison.status_code == 200
        assert selected_comparison.json()["after"]["id"] == optimized["id"]
        assert client.get(f"/api/scenarios/main/plan-versions/{restored['id']}/report").status_code == 200

        profiles = client.get("/api/strategy-profiles").json()
        assert {"low_overtime", "fair_workload", "stable"}.issubset({item["id"] for item in profiles})
        experiment_response = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={
                "dataset": "current",
                "profile_ids": ["balanced", "low_travel", "low_overtime"],
                "time_limit_seconds": 1,
            },
        )
        assert experiment_response.status_code == 202
        duplicate_response = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={
                "dataset": "current",
                "profile_ids": ["balanced", "low_travel", "low_overtime"],
                "time_limit_seconds": 1,
            },
        )
        assert duplicate_response.status_code == 202
        assert duplicate_response.json()["id"] == experiment_response.json()["id"]
        experiment = _wait_for_experiment(client, "main", experiment_response.json()["id"])
        assert experiment["status"] == "COMPLETED"
        assert len(experiment["candidates"]) == 3
        assert (
            len(
                {
                    tuple(
                        (item["work_order_id"], item["technician_id"], item["sequence"])
                        for item in candidate["schedule"]["assignments"]
                    )
                    for candidate in experiment["candidates"]
                }
            )
            >= 2
        )
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]

        candidate = next(item for item in experiment["candidates"] if item["publishable"])
        published = client.post(
            f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish",
            json={"candidate_id": candidate["id"], "expected_revision": 2},
        )
        assert published.status_code == 200
        assert published.json()["number"] == 4
        retried = client.post(
            f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish",
            json={"candidate_id": candidate["id"], "expected_revision": 2},
        )
        assert retried.status_code == 200
        assert retried.json()["id"] == published.json()["id"]
        published_experiment = client.get(f"/api/scenarios/main/strategy-experiments/{experiment['id']}").json()
        assert published_experiment["winner_candidate_id"] == candidate["id"]
        assert published_experiment["winner_plan_version_id"] == published.json()["id"]
        assert published_experiment["published_at"]
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3, 4]
        another = next(
            (item for item in experiment["candidates"] if item["id"] != candidate["id"] and item["publishable"]), None
        )
        if another:
            rejected_second_choice = client.post(
                f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish",
                json={"candidate_id": another["id"], "expected_revision": 2},
            )
            assert rejected_second_choice.status_code == 409


def test_experiment_can_be_cancelled_cooperatively(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "experiment-cancel.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    started = threading.Event()
    release = threading.Event()
    original = main_module.optimized_schedule

    def blocking_solver(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "optimized_schedule", blocking_solver)
    with TestClient(main_module.app) as client:
        created = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"profile_ids": ["balanced"], "time_limit_seconds": 1},
        )
        assert created.status_code == 202
        assert started.wait(2)
        cancelled = client.post(f"/api/scenarios/main/strategy-experiments/{created.json()['id']}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "CANCEL_REQUESTED"
        release.set()
        terminal = _wait_for_experiment(client, "main", created.json()["id"])
        assert terminal["status"] == "CANCELLED"
        assert terminal["finished_at"]
        assert (
            client.post(
                f"/api/scenarios/main/strategy-experiments/{created.json()['id']}/publish",
                json={"candidate_id": "none", "expected_revision": 0},
            ).status_code
            == 409
        )


def test_experiment_partial_failure_and_queue_capacity_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "experiment-governance.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    original = main_module.optimized_schedule

    def one_profile_fails(*args, **kwargs):
        if kwargs.get("strategy") == "low_travel":
            raise RuntimeError("injected candidate failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "optimized_schedule", one_profile_fails)
    with TestClient(main_module.app) as client:
        duplicate = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"profile_ids": ["balanced", "balanced"]},
        )
        assert duplicate.status_code == 422
        too_many = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"profile_ids": [f"profile-{index}" for index in range(9)]},
        )
        assert too_many.status_code == 422

        assert main_module.experiment_slots is not None
        slots = main_module.experiment_slots
        acquired = [slots.acquire(blocking=False) for _ in range(main_module.EXPERIMENT_QUEUE_CAPACITY)]
        assert all(acquired)
        try:
            full = client.post(
                "/api/scenarios/main/strategy-experiments",
                json={"profile_ids": ["balanced"]},
            )
            assert full.status_code == 429
        finally:
            for _ in acquired:
                slots.release()

        created = client.post(
            "/api/scenarios/main/strategy-experiments",
            json={"profile_ids": ["balanced", "low_travel"], "time_limit_seconds": 1},
        )
        assert created.status_code == 202
        terminal = _wait_for_experiment(client, "main", created.json()["id"])
        assert terminal["status"] == "COMPLETED_WITH_ERRORS"
        assert terminal["candidate_errors"]["low_travel"].startswith("RuntimeError")
        winner = next(item for item in terminal["candidates"] if item["publishable"])
        published = client.post(
            f"/api/scenarios/main/strategy-experiments/{terminal['id']}/publish",
            json={"candidate_id": winner["id"], "expected_revision": 0},
        )
        assert published.status_code == 200


def test_custom_profile_crud_and_stale_experiment_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "profiles.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    payload = {
        "name": "城郊节油",
        "description": "长距离区域减少往返",
        "weights": {
            "travel_weight": 20,
            "sla_late_weight": 8,
            "overtime_weight": 6,
            "imbalance_weight": 2,
            "replan_change_weight": 80,
            "unassigned_penalty_scale": 0.8,
        },
        "time_limit_seconds": 1,
    }
    with TestClient(main_module.app) as client:
        created = client.post("/api/strategy-profiles", json=payload)
        assert created.status_code == 201
        profile_id = created.json()["id"]
        payload["name"] = "城郊低行程"
        assert client.put(f"/api/strategy-profiles/{profile_id}", json=payload).json()["name"] == "城郊低行程"
        assert client.delete("/api/strategy-profiles/balanced").status_code == 409

        blocker = client.post(
            "/api/scenarios/main/strategy-experiments", json={"profile_ids": ["fair_workload"], "time_limit_seconds": 1}
        )
        response = client.post(
            "/api/scenarios/main/strategy-experiments", json={"profile_ids": [profile_id], "time_limit_seconds": 1}
        )
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
        edited = client.put(
            "/api/scenarios/main/work-orders/WO-1021",
            headers={"If-Match": "D0"},
            json={"note": "实验后更新"},
        )
        assert edited.status_code == 200
        rejected = client.post(
            f"/api/scenarios/main/strategy-experiments/{experiment['id']}/publish",
            json={"candidate_id": experiment["candidates"][0]["id"], "expected_revision": 1},
        )
        assert rejected.status_code == 409


def test_comparison_marks_different_business_snapshots_as_non_comparable(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "comparison-snapshots.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        before = client.get("/api/scenarios/main/plan-versions").json()[0]
        assert (
            client.put(
                "/api/scenarios/main/work-orders/WO-1021",
                headers={"If-Match": "D0"},
                json={"note": "客户改约后重新计算"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/scenarios/main/optimize",
                json={"strategy": "balanced", "time_limit_seconds": 1},
            ).status_code
            == 200
        )
        after = client.get("/api/scenarios/main/plan-versions").json()[-1]
        comparison = client.get(f"/api/scenarios/main/comparison?before={before['id']}&after={after['id']}").json()
        assert comparison["comparable"] is False
        assert comparison["same_scenario_snapshot"] is False
        assert comparison["modified_work_orders"] == ["WO-1021"]
        assert comparison["added_work_orders"] == []
        assert comparison["removed_work_orders"] == []
        assert comparison["delta"]["objective"] is None
        assert all(value is None for value in comparison["delta"].values())


def test_plan_commands_are_idempotent_in_scenario_and_action_namespaces(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "plan-command-idempotency.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    headers = {"Idempotency-Key": "same-visible-key-001"}
    with TestClient(main_module.app) as client:
        first = client.post("/api/scenarios/main/baseline", headers=headers)
        repeated = client.post("/api/scenarios/main/baseline", headers=headers)
        assert first.status_code == repeated.status_code == 200
        assert first.json()["id"] == repeated.json()["id"]
        assert len(client.get("/api/scenarios/main/schedule-runs").json()) == 1

        optimized = client.post(
            "/api/scenarios/main/optimize",
            headers=headers,
            json={"strategy": "balanced", "time_limit_seconds": 1},
        )
        optimized_retry = client.post(
            "/api/scenarios/main/optimize",
            headers=headers,
            json={"strategy": "balanced", "time_limit_seconds": 1},
        )
        assert optimized.status_code == optimized_retry.status_code == 200
        assert optimized.json()["id"] == optimized_retry.json()["id"]
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2]

        assert (
            client.put(
                "/api/scenarios/main/work-orders/WO-1021",
                headers={"If-Match": "D0"},
                json={"note": "新修订"},
            ).status_code
            == 200
        )
        conflict = client.post("/api/scenarios/main/baseline", headers=headers)
        assert conflict.status_code == 409


def test_historical_activation_and_clone_do_not_modify_current_business_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "activation-clone.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1})
        plans = client.get("/api/scenarios/main/plan-versions").json()
        source = plans[0]
        stale_preview = client.get(f"/api/scenarios/main/plan-versions/{source['id']}/rollback-preview").json()
        activated = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/activate",
            json={"expected_revision": 0, "idempotency_key": "activate-history-001"},
        )
        assert activated.status_code == 200
        assert activated.json()["number"] == 3
        assert activated.json()["action"] == "activate"
        assert activated.json()["relation"] == "reactivated_from"
        repeated = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/activate",
            json={"expected_revision": 0, "idempotency_key": "activate-history-001"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["id"] == activated.json()["id"]
        assert client.get("/api/scenarios/main").json()["revision"] == 0
        expired_rollback = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/restore",
            json={
                "expected_revision": 0,
                "confirmation_token": stale_preview["confirmation_token"],
                "reason": "使用已过期预览",
                "idempotency_key": "expired-preview-001",
            },
        )
        assert expired_rollback.status_code == 409
        assert "重新查看差异" in str(expired_rollback.json()["detail"])
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2, 3]

        clone_request = {"name": "V001 独立副本", "idempotency_key": "clone-history-001"}
        clone = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/clone-scenario",
            json=clone_request,
        )
        clone_retry = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/clone-scenario",
            json=clone_request,
        )
        assert clone.status_code == clone_retry.status_code == 201
        assert clone.json()["id"] == clone_retry.json()["id"]
        assert clone.json()["id"] != "main"
        assert clone.json()["revision"] == 0
        assert client.get("/api/scenarios/main").json()["revision"] == 0

        assert (
            client.put(
                "/api/scenarios/main/work-orders/WO-1021",
                headers={"If-Match": "D0"},
                json={"note": "实时变更"},
            ).status_code
            == 200
        )
        rejected = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/activate",
            json={"expected_revision": 1, "idempotency_key": "activate-history-002"},
        )
        assert rejected.status_code == 409
        assert (
            next(item for item in client.get("/api/scenarios/main").json()["work_orders"] if item["id"] == "WO-1021")[
                "note"
            ]
            == "实时变更"
        )


def test_replan_activation_preserves_lineage_and_original_stability_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "activation-stability.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        replanned = client.post(
            "/api/scenarios/main/replan",
            json={"planning_time": 600, "strategy": "stable", "time_limit_seconds": 1},
        )
        assert replanned.status_code == 200, replanned.text
        baseline_plan, replan_plan = client.get("/api/scenarios/main/plan-versions").json()
        assert replan_plan["lineage_source_version_id"] == baseline_plan["id"]
        assert replan_plan["stability_baseline_version_id"] == baseline_plan["id"]

        activated = client.post(
            f"/api/scenarios/main/plan-versions/{replan_plan['id']}/activate",
            json={"expected_revision": 0, "idempotency_key": "activate-replan-stability-001"},
        )
        assert activated.status_code == 200, activated.text
        restored = activated.json()
        assert restored["lineage_source_version_id"] == replan_plan["id"]
        assert restored["stability_baseline_version_id"] == baseline_plan["id"]
        assert restored["selected"]["kpis"]["stability_rate"] == replan_plan["selected"]["kpis"]["stability_rate"]
        assert (
            restored["selected"]["kpis"]["same_technician_rate"]
            == replan_plan["selected"]["kpis"]["same_technician_rate"]
        )
        assert (
            restored["publication_planning_context"]["route_entries"]
            == replan_plan["publication_planning_context"]["route_entries"]
        )
        risk = client.post(
            f"/api/scenarios/main/plan-versions/{restored['id']}/analysis-runs",
            json={
                "analysis_type": "RISK",
                "analysis_scope": "EX_ANTE_FROZEN_PLAN",
                "request": {"seed": 41, "trials": 50},
            },
        )
        assert risk.status_code == 201
        assert risk.json()["status"] == "COMPLETED", risk.text

        preview = client.get(f"/api/scenarios/main/plan-versions/{replan_plan['id']}/rollback-preview").json()
        rolled_back = client.post(
            f"/api/scenarios/main/plan-versions/{replan_plan['id']}/restore",
            json={
                "expected_revision": preview["expected_revision"],
                "confirmation_token": preview["confirmation_token"],
                "reason": "验证重排上下文恢复",
                "allow_delete_new_orders": False,
                "idempotency_key": "restore-replan-context-001",
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        restored_again = rolled_back.json()
        assert (
            restored_again["publication_planning_context"]["route_entries"]
            == replan_plan["publication_planning_context"]["route_entries"]
        )
        assert restored_again["publication_planning_context"]["scenario_revision"] == 1
        restored_risk = client.post(
            f"/api/scenarios/main/plan-versions/{restored_again['id']}/analysis-runs",
            json={
                "analysis_type": "RISK",
                "analysis_scope": "EX_ANTE_FROZEN_PLAN",
                "request": {"seed": 43, "trials": 50},
            },
        )
        assert restored_risk.status_code == 201
        assert restored_risk.json()["status"] == "COMPLETED", restored_risk.text


def test_cloned_scenario_resets_to_its_cloned_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "clone-reset.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        source_note = "克隆时保留的业务输入"
        assert (
            client.put(
                "/api/scenarios/main/work-orders/WO-1021",
                headers={"If-Match": "D0"},
                json={"note": source_note},
            ).status_code
            == 200
        )
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        source = client.get("/api/scenarios/main/plan-versions").json()[0]
        clone = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/clone-scenario",
            json={
                "name": "历史快照副本",
                "idempotency_key": "clone-reset-source-001",
            },
        ).json()
        clone_id = clone["id"]
        assert next(item for item in clone["work_orders"] if item["id"] == "WO-1021")["note"] == source_note

        edited = client.put(
            f"/api/scenarios/{clone_id}/work-orders/WO-1021",
            headers={"If-Match": "D0"},
            json={"note": "副本中的临时修改"},
        )
        assert edited.status_code == 200
        reset = client.post(f"/api/scenarios/{clone_id}/reset", headers={"If-Match": "D1"})
        assert reset.status_code == 200
        assert reset.json()["revision"] == 2
        assert next(item for item in reset.json()["work_orders"] if item["id"] == "WO-1021")["note"] == source_note


def test_business_rollback_blocks_reopening_completed_and_deleting_new_orders(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "rollback-guards.db"))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        client.post("/api/scenarios/main/baseline")
        source = client.get("/api/scenarios/main/plan-versions").json()[0]
        assignment = next(item for item in source["selected"]["assignments"] if item["sequence"] == 1)
        work_order_id = assignment["work_order_id"]
        revision = client.get("/api/scenarios/main").json()["revision"]
        started = client.post(
            f"/api/scenarios/main/work-orders/{work_order_id}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": revision,
                "idempotency_key": "rollback-start-001",
            },
        )
        assert started.status_code == 200
        completed = client.post(
            f"/api/scenarios/main/work-orders/{work_order_id}/complete",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["finish_time"],
                "expected_revision": started.json()["scenario"]["revision"],
                "idempotency_key": "rollback-complete-001",
            },
        )
        assert completed.status_code == 200
        preview = client.get(f"/api/scenarios/main/plan-versions/{source['id']}/rollback-preview").json()
        assert preview["completed_work_orders_reopened"] == [work_order_id]
        assert preview["executed_work_orders_deleted"] == []
        assert len(preview["affected_execution_event_ids"]) == 2
        blocked = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/restore",
            json={
                "expected_revision": completed.json()["scenario"]["revision"],
                "confirmation_token": preview["confirmation_token"],
                "reason": "测试保护",
                "idempotency_key": "rollback-guard-001",
            },
        )
        assert blocked.status_code == 409

        current = client.get("/api/scenarios/main").json()
        extra = dict(current["work_orders"][0])
        extra.update({"id": "WO-NEW-AFTER-PLAN", "is_emergency": False, "reported_at": None})
        extra.pop("status", None)
        assert (
            client.post(
                "/api/scenarios/main/work-orders",
                headers={"If-Match": "D2"},
                json=extra,
            ).status_code
            == 200
        )
        preview = client.get(f"/api/scenarios/main/plan-versions/{source['id']}/rollback-preview").json()
        assert preview["removed_work_orders"] == ["WO-NEW-AFTER-PLAN"]
        blocked = client.post(
            f"/api/scenarios/main/plan-versions/{source['id']}/restore",
            json={
                "expected_revision": 2,
                "confirmation_token": preview["confirmation_token"],
                "reason": "测试新增工单保护",
                "allow_reopen_completed": True,
                "idempotency_key": "rollback-guard-002",
            },
        )
        assert blocked.status_code == 409
        assert any(
            item["id"] == "WO-NEW-AFTER-PLAN" for item in client.get("/api/scenarios/main").json()["work_orders"]
        )


def test_atomic_public_version_allocation(tmp_path):
    store = Store(tmp_path / "atomic.db")
    scenario = get_fixture("main")
    results = [baseline_schedule(scenario, 0) for _ in range(4)]
    candidate_ids = [_verified_candidate(store, scenario, result) for result in results]
    with ThreadPoolExecutor(max_workers=4) as executor:
        plans = list(
            executor.map(
                lambda pair: store.publish_plan(scenario, pair[0], "baseline", candidate_id=pair[1]),
                zip(results, candidate_ids, strict=False),
            )
        )
    assert sorted(plan.number for plan in plans) == [1, 2, 3, 4]
    assert [plan.number for plan in store.list_plan_versions("main")] == [1, 2, 3, 4]


def test_run_and_candidate_completion_rolls_back_as_one_transaction(tmp_path):
    database = tmp_path / "run-completion.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    assert scenario is not None
    result = baseline_schedule(scenario, 0)
    now = datetime.now(UTC).isoformat()
    run = ScheduleRun(
        id="RUN-ATOMIC",
        scenario_id=scenario.id,
        action="baseline",
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        solver_name=result.solver_name,
        solver_version=result.solver_version,
        solver_config_hash=result.solver_config_hash,
        solver_policy_fingerprint=result.solver_policy.fingerprint,
        requested_time_limit_ms=0,
        effective_time_limit_ms=0,
        status=ScheduleRunStatus.running,
        started_at=now,
    )
    store.save_schedule_run(run)
    report = verify_schedule(scenario, result)
    candidate = ScheduleCandidate(
        id="CAND-ATOMIC",
        run_id=run.id,
        scenario_id=scenario.id,
        scenario_revision=scenario.revision,
        scenario_snapshot_hash=content_hash(scenario),
        solver_config_hash=result.solver_config_hash,
        solver_policy_fingerprint=result.solver_policy.fingerprint,
        schedule=result,
        verification_report=report,
        publishable=report.publishable,
        created_at=now,
    )
    run.status = ScheduleRunStatus.feasible
    run.solution_found = True
    run.finished_at = now
    run.candidate_id = candidate.id
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TRIGGER fail_run_completion
            BEFORE UPDATE ON schedule_runs
            WHEN NEW.status != 'RUNNING'
            BEGIN
                SELECT RAISE(ABORT, 'forced run completion rollback');
            END;
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="forced run completion rollback"):
        store.complete_schedule_run(run, candidate)
    assert store.get_schedule_candidate(candidate.id) is None
    stored_run = store.get_schedule_run(run.id)
    assert stored_run is not None
    assert stored_run.status is ScheduleRunStatus.running
    assert stored_run.candidate_id is None


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
        connection.execute(
            "INSERT INTO scenarios(id, payload) VALUES (?, ?)", (legacy_scenario.id, legacy_scenario.model_dump_json())
        )
        connection.execute(
            "INSERT INTO schedules(id, scenario_id, kind, version, payload) VALUES ('OLD-1', 'main', 'optimized', 17, '{}')"
        )

    store = Store(database)
    backups = list(tmp_path.glob("fieldflow.legacy-*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup, backup:
        assert backup.execute("SELECT id, version FROM schedules").fetchall() == [("OLD-1", 17)]
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    with closing(sqlite3.connect(database)) as migrated, migrated:
        assert migrated.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
        assert migrated.execute("SELECT active_plan_version_id FROM scenarios WHERE id='main'").fetchone()[0] is None
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 21
        assert migrated.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.list_plan_versions("main") == []


@pytest.mark.parametrize("legacy_version", range(1, 16))
def test_schema_versions_1_through_15_converge_to_current_schema(tmp_path, legacy_version):
    database = tmp_path / f"schema-v{legacy_version}.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={legacy_version}")

    migrated = Store(database)
    assert migrated.get_scenario("main") is not None
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "publication_key" in {row[1] for row in connection.execute("PRAGMA table_info(command_keys)").fetchall()}
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_applicability'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_analysis_runs'"
        ).fetchone()


def test_relational_schema_enforces_foreign_keys_and_artifact_parent(tmp_path):
    database = tmp_path / "integrity.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_list(plan_versions)").fetchall()
        assert connection.execute("PRAGMA foreign_key_list(plan_applicability)").fetchall()
        assert connection.execute("PRAGMA foreign_key_list(schedule_artifacts)").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO publication_keys VALUES ('orphan', 'x', 'missing-plan', 'now')")
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
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


def test_saved_candidates_are_immutable_and_publish_is_rechecked(tmp_path):
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
    with pytest.raises(PublicationConflict, match="不可修改"):
        store.save_schedule_candidate(candidate)
    stored = store.get_schedule_candidate(candidate.id)
    assert stored is not None and stored.schedule.model_dump() == original.model_dump()
    published = store.publish_plan(scenario, original, "baseline", candidate_id=candidate.id)
    assert published.number == 1
