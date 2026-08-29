import importlib
import sqlite3
import time
from contextlib import closing

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from backend.hashing import content_hash
from backend.models import ReplanRequest, RuntimeJobStatus, WorkOrder
from backend.solver_worker import process_resident_memory_bytes
from backend.storage import PublicationConflict, Store


def wait_for_job_status(
    client: TestClient,
    job_id: str,
    statuses: set[str],
    *,
    timeout_seconds: float = 30,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    current: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/scenarios/main/jobs/{job_id}")
        assert response.status_code == 200, response.text
        current = response.json()
        if current["status"] in statuses:
            return current
        time.sleep(0.05)
    return current


def test_linux_resident_memory_reader_uses_rss_not_virtual_size(tmp_path):
    process = tmp_path / "321"
    process.mkdir()
    (process / "status").write_text(
        "Name:\tfieldflow\nVmSize:\t8388608 kB\nVmRSS:\t2048 kB\n",
        encoding="utf-8",
    )
    assert process_resident_memory_bytes(321, proc_root=tmp_path) == 2 * 1024 * 1024
    assert process_resident_memory_bytes(999, proc_root=tmp_path) is None


def test_child_store_does_not_run_application_restart_recovery(tmp_path):
    database = tmp_path / "child-store.db"
    store = Store(database)
    job, _ = store.enqueue_runtime_job(
        job_type="RISK_ANALYSIS",
        scenario_id="main",
        input_payload={"plan_version_id": "PV-1"},
        dedupe_key="child-store-recovery-proof-001",
    )
    claimed = store.claim_runtime_job("parent-worker", job_id=job.id, lease_seconds=120)
    assert claimed is not None and claimed.status is RuntimeJobStatus.running

    Store(database, allow_migration=False, recover_runtime=False)
    still_running = store.get_runtime_job(job.id)
    assert still_running is not None
    assert still_running.status is RuntimeJobStatus.running
    assert still_running.lease_owner == "parent-worker"

    Store(database, allow_migration=False)
    interrupted = store.get_runtime_job(job.id)
    assert interrupted is not None and interrupted.status is RuntimeJobStatus.interrupted


def test_runtime_job_lease_outbox_and_terminal_invariants(tmp_path):
    database = tmp_path / "jobs.db"
    store = Store(database)
    job, created = store.enqueue_runtime_job(
        job_type="RISK_ANALYSIS",
        scenario_id="main",
        input_payload={"plan_version_id": "PV-1", "trials": 5000},
        dedupe_key="risk-queue-proof-001",
    )
    assert created is True
    repeated, repeated_created = store.enqueue_runtime_job(
        job_type="RISK_ANALYSIS",
        scenario_id="main",
        input_payload={"plan_version_id": "PV-1", "trials": 5000},
        dedupe_key="risk-queue-proof-001",
    )
    assert repeated_created is False and repeated.id == job.id
    pending = store.list_pending_outbox()
    assert len(pending) == 1 and pending[0].aggregate_id == job.id

    claimed = store.claim_runtime_job("worker-a", job_id=job.id, lease_seconds=60)
    assert claimed is not None
    assert claimed.status is RuntimeJobStatus.running
    assert claimed.attempt_number == 1
    assert store.claim_runtime_job("worker-b", job_id=job.id) is None
    heartbeat = store.heartbeat_runtime_job(job.id, "worker-a", 45)
    assert heartbeat.progress == 45
    with pytest.raises(PublicationConflict, match="租约"):
        store.heartbeat_runtime_job(job.id, "worker-b", 50)

    completed = store.finish_runtime_job(
        job.id,
        "worker-a",
        status=RuntimeJobStatus.completed,
        result_resource_type="decision_analysis",
        result_resource_id="A001",
    )
    assert completed.status is RuntimeJobStatus.completed
    assert completed.progress == 100
    assert store.finish_runtime_job(job.id, None, status=RuntimeJobStatus.completed).id == job.id

    assert store.mark_outbox_dispatched_for_aggregate(job.id) == 1
    assert store.list_pending_outbox() == []
    with closing(sqlite3.connect(database)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="input identity is immutable"):
            connection.execute(
                "UPDATE runtime_jobs SET input_manifest_hash='forged' WHERE id=?",
                (job.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="terminal runtime job is immutable"):
            connection.execute(
                "UPDATE runtime_jobs SET payload=json_set(payload, '$.status', 'RUNNING') WHERE id=?",
                (job.id,),
            )


def test_decision_analysis_job_is_non_blocking_durable_and_idempotent(monkeypatch, tmp_path):
    database = tmp_path / "analysis-jobs.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        endpoint = f"/api/v2/scenarios/main/plan-versions/{plan['id']}/analysis-jobs"
        payload = {"analysis_type": "RISK", "request": {"seed": 71, "trials": 50}}
        created = client.post(endpoint, headers={"Idempotency-Key": "analysis-job-proof-001"}, json=payload)
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] in {"QUEUED", "RUNNING"}
        current = wait_for_job_status(client, job["id"], {"COMPLETED", "FAILED", "CANCELLED"})
        assert current["status"] == "COMPLETED", current
        run = client.get(f"/api/scenarios/main/analysis-runs/{current['result_resource_id']}")
        assert run.status_code == 200 and run.json()["status"] == "COMPLETED"

        replay = client.post(endpoint, headers={"Idempotency-Key": "analysis-job-proof-001"}, json=payload)
        assert replay.status_code == 200 and replay.json()["id"] == job["id"]
        conflict = client.post(
            endpoint,
            headers={"Idempotency-Key": "analysis-job-proof-001"},
            json={"analysis_type": "RISK", "request": {"seed": 72, "trials": 50}},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_running_risk_job_hard_cancel_interrupts_analysis_subprocess(monkeypatch, tmp_path):
    database = tmp_path / "analysis-cancel.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = client.get("/api/scenarios/main/plan-versions").json()[0]
        created = client.post(
            f"/api/v2/scenarios/main/plan-versions/{plan['id']}/analysis-jobs",
            headers={"Idempotency-Key": "analysis-hard-cancel-001"},
            json={"analysis_type": "RISK", "request": {"seed": 73, "trials": 5000}},
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        current = wait_for_job_status(client, job_id, {"RUNNING"}, timeout_seconds=10)
        assert current["status"] == "RUNNING"
        cancelled = client.post(f"/api/v2/scenarios/main/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        current = wait_for_job_status(client, job_id, {"CANCELLED"}, timeout_seconds=10)
        assert current["status"] == "CANCELLED", current
        with closing(sqlite3.connect(database)) as connection:
            running = connection.execute(
                "SELECT COUNT(*) FROM decision_analysis_runs WHERE status='RUNNING'"
            ).fetchone()[0]
        assert running == 0


def test_emergency_intake_outbox_recovers_replan_after_restart(monkeypatch, tmp_path):
    database = tmp_path / "emergency-outbox.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    intake_namespace = "main:emergency-intake"
    intake_key = "emergency-intake:WO-EMG-OUTBOX"
    order = WorkOrder.model_validate(
        {
            "id": "WO-EMG-OUTBOX",
            "customer_name": "应急客户",
            "title": "配电柜告警",
            "required_skills": ["electrical"],
            "location": {"x": 54, "y": 56},
            "service_duration": 25,
            "window_start": 600,
            "window_end": 720,
            "sla_deadline": 660,
            "priority": "urgent",
            "drop_penalty": 12000,
            "status": "pending",
            "vip": True,
            "is_emergency": True,
            "reported_at": 600,
            "note": "",
        }
    )
    request = ReplanRequest(
        planning_time=600,
        current_time=600,
        time_limit_seconds=1,
        emergency_order=order.model_dump(exclude={"status"}, mode="json"),
        idempotency_key="emergency-outbox-replan-001",
        intake_idempotency_key=intake_key,
    )
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        store = main_module.require_store()
        store.intake_emergency_work_order(
            "main",
            order,
            namespace=intake_namespace,
            idempotency_key=intake_key,
            request_fingerprint=content_hash({"scenario_id": "main", "work_order": order}),
            replan_job_payload={"request": request.model_dump(mode="json", exclude_unset=True)},
        )
        job_id = f"JOB-EMG-{content_hash({'namespace': intake_namespace, 'key': intake_key})[:16]}"
        assert store.get_runtime_job(job_id).status is RuntimeJobStatus.queued

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        current = wait_for_job_status(client, job_id, {"COMPLETED", "FAILED"})
        assert current["status"] == "COMPLETED", current
        plans = client.get("/api/scenarios/main/plan-versions").json()
        assert len(plans) == 2 and plans[-1]["active"] is True
        scenario = client.get("/api/scenarios/main").json()
        assert any(item["id"] == order.id for item in scenario["work_orders"])


def test_interrupted_analysis_job_restarts_as_next_attempt(monkeypatch, tmp_path):
    class SimulatedProcessExit(BaseException):
        pass

    database = tmp_path / "analysis-restart.db"
    monkeypatch.setenv("FIELDFLOW_DB", str(database))
    import backend.main as main_module

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
        plan = main_module.require_store().active_plan_version("main")
        assert plan is not None
        request = TypeAdapter(main_module.DecisionAnalysisRunRequest).validate_python(
            {"analysis_type": "RISK", "request": {"seed": 89, "trials": 50}}
        )
        job, created = main_module.require_store().enqueue_runtime_job(
            job_type="RISK_ANALYSIS",
            scenario_id="main",
            input_payload={"plan_version_id": plan.id, "analysis_request": request.model_dump(mode="json")},
            dedupe_key="analysis-restart-proof-001",
        )
        assert created is True

        def crash_after_reservation(_run):
            raise SimulatedProcessExit("after A reservation")

        with pytest.raises(SimulatedProcessExit):
            main_module.execute_decision_analysis_run(
                "main",
                plan,
                request,
                Response(),
                on_reserved=crash_after_reservation,
            )

    main_module = importlib.reload(main_module)
    with TestClient(main_module.app) as client:
        current = wait_for_job_status(client, job.id, {"COMPLETED", "FAILED"})
        assert current["status"] == "COMPLETED", current
        runs = client.get(f"/api/scenarios/main/plan-versions/{plan.id}/analysis-runs").json()
        assert [item["status"] for item in runs] == ["INTERRUPTED", "COMPLETED"]
        assert [item["attempt_number"] for item in runs] == [1, 2]
        assert runs[0]["logical_analysis_id"] == runs[1]["logical_analysis_id"]
