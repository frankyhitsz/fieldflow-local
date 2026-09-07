import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.job_authority import JobAuthority, job_authority
from backend.models import RuntimeJobStatus
from backend.storage import PublicationConflict, Store
from backend.travel import (
    EuclideanTravelTimeProvider,
    MatrixTravelTimeProvider,
    freeze_travel_provider,
    restore_travel_provider,
)


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "live.db"))
    import backend.main as module

    module = importlib.reload(module)
    with TestClient(module.app) as client:
        yield module, client


def wait_job(client, identifier, statuses=frozenset({"COMPLETED", "FAILED", "CANCELLED"})):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(f"/api/v2/scenarios/main/jobs/{identifier}").json()
        if result["status"] in statuses:
            return result
        time.sleep(0.05)
    pytest.fail(f"job did not reach {statuses}: {result}")


def emergency_request(key):
    return {
        "planning_time": 600,
        "time_limit_seconds": 1,
        "idempotency_key": key,
        "emergency_order": {
            "id": "WO-AUDIT-EMG",
            "customer_name": "应急测试",
            "title": "配电柜告警",
            "required_skills": ["electrical"],
            "location": {"x": 54, "y": 56},
            "service_duration": 25,
            "window_start": 600,
            "window_end": 780,
            "sla_deadline": 700,
            "priority": "urgent",
            "drop_penalty": 12000,
            "is_emergency": True,
            "reported_at": 600,
        },
    }


def test_other_plan_published_before_ack_cannot_change_command_result(live, monkeypatch):
    module, client = live
    committed, resume = Event(), Event()
    original = module.require_store().publish_plan

    def publish(*args, **kwargs):
        plan = original(*args, **kwargs)
        if not committed.is_set():
            committed.set()
            assert resume.wait(10)
        return plan

    monkeypatch.setattr(module.require_store(), "publish_plan", publish)
    with ThreadPoolExecutor() as executor:
        first = executor.submit(
            client.post, "/api/scenarios/main/baseline", headers={"Idempotency-Key": "audit-before-ack"}
        )
        try:
            assert committed.wait(10)
            second = client.post("/api/scenarios/main/baseline")
            assert second.status_code == 200
        finally:
            resume.set()
        response = first.result(10)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1 and second.json()["version"] == 2
    replay = client.post("/api/scenarios/main/baseline", headers={"Idempotency-Key": "audit-before-ack"})
    assert replay.json()["id"] == response.json()["id"]
    assert len(client.get("/api/scenarios/main/plan-versions").json()) == 2


@pytest.mark.parametrize("cancel_before_commit", [True, False])
def test_cancel_and_publish_have_one_transaction_winner(live, monkeypatch, cancel_before_commit):
    module, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    reached, resume = Event(), Event()
    original = module.require_store().publish_plan

    def publish(*args, **kwargs):
        if not cancel_before_commit:
            result = original(*args, **kwargs)
        reached.set()
        assert resume.wait(15)
        return original(*args, **kwargs) if cancel_before_commit else result

    monkeypatch.setattr(module.require_store(), "publish_plan", publish)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            client.post, "/api/scenarios/main/replan", json=emergency_request("audit-cancel-winner")
        )
        try:
            assert reached.wait(15)
            job = client.get("/api/v2/scenarios/main/jobs").json()[0]
            cancelled = client.post(f"/api/v2/scenarios/main/jobs/{job['id']}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == ("CANCEL_REQUESTED" if cancel_before_commit else "COMPLETED")
        finally:
            resume.set()
        response = request.result(15)
    terminal = wait_job(client, job["id"])
    plans = client.get("/api/scenarios/main/plan-versions").json()
    if cancel_before_commit:
        assert response.status_code == 409, response.text
        assert terminal["status"] == "CANCELLED" and len(plans) == 1
    else:
        assert response.status_code == 200, response.text
        assert terminal["status"] == "COMPLETED" and len(plans) == 2
        assert terminal["result_resource_id"] == plans[-1]["id"]


def test_failed_emergency_replan_can_use_new_command_and_replay_after_note(live, monkeypatch):
    module, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    original = module.replan_schedule
    monkeypatch.setattr(
        module, "replan_schedule", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("injected failure"))
    )
    first = client.post("/api/scenarios/main/replan", json=emergency_request("audit-emergency-failed"))
    assert first.status_code == 500
    monkeypatch.setattr(module, "replan_schedule", original)
    payload = emergency_request("audit-emergency-new-attempt")
    second = client.post("/api/scenarios/main/replan", json=payload)
    assert second.status_code == 200, second.text
    assert (
        client.put("/api/scenarios/main/work-orders/WO-AUDIT-EMG", json={"note": "客户补充进场信息"}).status_code == 200
    )
    replay = client.post("/api/scenarios/main/replan", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == second.json()["id"]
    assert len([o for o in client.get("/api/scenarios/main").json()["work_orders"] if o["id"] == "WO-AUDIT-EMG"]) == 1
    assert len(client.get("/api/scenarios/main/plan-versions").json()) == 2


def test_identical_experiment_can_run_twice_and_cancelled_can_rerun(live):
    _, client = live
    body = {"profile_ids": ["balanced"], "time_limit_seconds": 1}
    endpoint = "/api/scenarios/main/strategy-experiments"
    first = client.post(endpoint, json=body, headers={"Idempotency-Key": "audit-experiment-one"})
    assert first.status_code == 202, first.text
    assert wait_job(client, f"JOB-EXP-{first.json()['id']}")["status"] == "COMPLETED"
    replay = client.post(endpoint, json=body, headers={"Idempotency-Key": "audit-experiment-one"})
    assert replay.json()["id"] == first.json()["id"]
    second = client.post(endpoint, json=body)
    assert second.status_code == 202 and second.json()["id"] != first.json()["id"]
    assert wait_job(client, f"JOB-EXP-{second.json()['id']}")["status"] == "COMPLETED"
    assert client.get("/api/scenarios/main/plan-versions").json() == []


def test_same_owner_old_generation_cannot_heartbeat_finish_or_commit(tmp_path, monkeypatch):
    import backend.storage as storage

    store = Store(tmp_path / "fence.db")
    job, _ = store.enqueue_runtime_job(job_type="COST_ANALYSIS", scenario_id="main", input_payload={})
    first = store.claim_runtime_job("same-owner", job_id=job.id)
    original_now = storage._now
    monkeypatch.setattr(storage, "_now", lambda: (datetime.now(UTC) + timedelta(minutes=2)).isoformat())
    store.reap_expired_jobs()
    monkeypatch.setattr(storage, "_now", original_now)
    second = store.claim_runtime_job("same-owner", job_id=job.id)
    assert second.attempt_number == first.attempt_number + 1
    with job_authority(JobAuthority(job.id, "same-owner", first.attempt_number)):
        with pytest.raises(PublicationConflict, match="租约"):
            store.heartbeat_runtime_job(job.id, "same-owner", 99)
        with pytest.raises(PublicationConflict):
            store.finish_runtime_job(job.id, "same-owner", status=RuntimeJobStatus.completed)
        with store._connect() as con, pytest.raises(PublicationConflict):
            con.execute("BEGIN IMMEDIATE")
            store._assert_job_authority(con)


@pytest.mark.parametrize(
    "provider",
    [
        EuclideanTravelTimeProvider(1, 7, "CUSTOM"),
        MatrixTravelTimeProvider({("A", "B"): 37}, {(1, 2): "A", (3.5, 4): "B"}),
    ],
)
def test_frozen_provider_roundtrip_preserves_configuration_and_fingerprint(provider):
    clone = restore_travel_provider(freeze_travel_provider(provider))
    assert clone == provider
    assert clone.fingerprint == provider.fingerprint


def test_emergency_queue_freezes_provider_and_rejects_changes_before_execution(live, monkeypatch):
    module, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    slots = module.experiment_slots
    for _ in range(3):
        assert slots.acquire(timeout=10)
    persisted, resume = Event(), Event()
    original = module.require_store().intake_emergency_work_order

    def intake(*args, **kwargs):
        result = original(*args, **kwargs)
        persisted.set()
        assert resume.wait(15)
        return result

    monkeypatch.setattr(module.require_store(), "intake_emergency_work_order", intake)
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(
            client.post, "/api/scenarios/main/replan", json=emergency_request("audit-frozen-emergency")
        )
        try:
            assert persisted.wait(10)
            job = client.get("/api/v2/scenarios/main/jobs").json()[0]
            assert job["input_payload"]["travel_provider"] == freeze_travel_provider(
                module.require_store().travel_provider
            )
            module.require_store().travel_provider = EuclideanTravelTimeProvider(3, 9, "CHANGED")
        finally:
            resume.set()
            for _ in range(3):
                slots.release()
        response = pending.result(15)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "TRAVEL_PROVIDER_CHANGED"
    assert wait_job(client, job["id"])["status"] == "FAILED"
    assert len(client.get("/api/scenarios/main/plan-versions").json()) == 1
    assert any(order["id"] == "WO-AUDIT-EMG" for order in client.get("/api/scenarios/main").json()["work_orders"])


def test_unsupported_provider_fails_before_plan_job_is_enqueued(live):
    module, client = live
    module.require_store().travel_provider = object()
    response = client.post("/api/scenarios/main/baseline")
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "UNSUPPORTED_TRAVEL_PROVIDER"
    assert client.get("/api/v2/scenarios/main/jobs").json() == []


def test_second_app_rejected_before_touching_first_jobs(live, tmp_path):
    module, client = live
    before = client.get("/api/scenarios/main").json()
    with pytest.raises(RuntimeError, match="ONE_APPLICATION_PER_PROCESS"):
        with TestClient(module.create_app(db_path=tmp_path / "second.db")):
            pass
    assert client.get("/api/scenarios/main").json() == before


def test_waiting_long_job_is_promoted_without_losing_short_job_priority(live):
    module, _ = live
    now = datetime.now(UTC)
    job, _ = module.require_store().enqueue_runtime_job(job_type="COST_ANALYSIS", scenario_id="main", input_payload={})
    short = job.model_copy(update={"created_at": now.isoformat()})
    fresh_long = short.model_copy(update={"job_type": "RISK_ANALYSIS"})
    old_long = fresh_long.model_copy(update={"created_at": (now - timedelta(seconds=31)).isoformat()})
    ordered = sorted([fresh_long, short, old_long], key=lambda item: module._job_dispatch_priority(item, now))
    assert ordered == [old_long, short, fresh_long]


def test_analysis_binding_survives_crash_before_ipc_and_cancel_interrupts_owner(live):
    module, client = live
    from fastapi import Response
    from pydantic import TypeAdapter

    from backend.models import DecisionAnalysisRunRequest, PlanUseCase

    class BeforeIPC(BaseException):
        pass

    assert client.post("/api/scenarios/main/baseline").status_code == 200
    store = module.require_store()
    plan = store.active_plan_version("main")
    with store._lock:
        job, _ = store.enqueue_runtime_job(job_type="COST_ANALYSIS", scenario_id="main", input_payload={})
        claim = store.claim_runtime_job("reservation-crash", job_id=job.id)

    def crash(_reserved):
        raise BeforeIPC()

    with job_authority(JobAuthority(job.id, "reservation-crash", claim.attempt_number)), pytest.raises(BeforeIPC):
        module.execute_decision_analysis_run(
            "main",
            store.require_plan_for_use("main", plan.id, PlanUseCase.analyze),
            TypeAdapter(DecisionAnalysisRunRequest).validate_python({"analysis_type": "COST"}),
            Response(),
            on_reserved=crash,
        )
    with store._connect() as con:
        binding = con.execute("SELECT analysis_id FROM job_analysis_bindings WHERE job_id=?", (job.id,)).fetchone()
    assert binding is not None
    cancelled = client.post(f"/api/v2/scenarios/main/jobs/{job.id}/cancel")
    assert cancelled.status_code == 200
    analysis = store.get_decision_analysis_run("main", binding["analysis_id"])
    assert analysis.status == "INTERRUPTED" and analysis.effective_integrity.value == "VERIFIED"


@pytest.mark.parametrize("cancel_follower", [False, True])
def test_follower_job_waits_for_running_analysis_without_interrupting_owner(live, cancel_follower):
    module, client = live
    from fastapi import Response
    from pydantic import TypeAdapter

    from backend.models import DecisionAnalysisRunRequest

    assert client.post("/api/scenarios/main/baseline").status_code == 200
    store = module.require_store()
    plan = store.active_plan_version("main")
    reached, resume = Event(), Event()
    with store._lock:
        owner, _ = store.enqueue_runtime_job(job_type="COST_ANALYSIS", scenario_id="main", input_payload={})
        claimed = store.claim_runtime_job("controlled-owner", job_id=owner.id, lease_seconds=120)

    def compute():
        with job_authority(JobAuthority(owner.id, "controlled-owner", claimed.attempt_number)):
            return module.execute_decision_analysis_run(
                "main",
                plan,
                TypeAdapter(DecisionAnalysisRunRequest).validate_python({"analysis_type": "COST"}),
                Response(),
                on_reserved=lambda _run: (reached.set(), resume.wait(20)),
            )

    with ThreadPoolExecutor() as executor:
        result = executor.submit(compute)
        try:
            assert reached.wait(10)
            follower = client.post(
                f"/api/v2/scenarios/main/plan-versions/{plan.id}/analysis-jobs",
                headers={"Idempotency-Key": "follower-analysis-job"},
                json={"analysis_type": "COST"},
            )
            assert follower.status_code == 202
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with store._connect() as con:
                    bound = con.execute(
                        "SELECT * FROM job_analysis_bindings WHERE job_id=?", (follower.json()["id"],)
                    ).fetchone()
                if bound:
                    break
                time.sleep(0.05)
            assert bound is not None and not bound["owns_computation"]
            assert store.get_runtime_job(follower.json()["id"]).status is RuntimeJobStatus.running
            if cancel_follower:
                assert client.post(f"/api/v2/scenarios/main/jobs/{follower.json()['id']}/cancel").status_code == 200
                assert store.get_decision_analysis_run("main", bound["analysis_id"]).status == "RUNNING"
        finally:
            resume.set()
        analysis = result.result(15)
    terminal = wait_job(client, follower.json()["id"])
    assert terminal["status"] == ("CANCELLED" if cancel_follower else "COMPLETED"), terminal
    if not cancel_follower:
        assert terminal["result_resource_id"] == analysis.id


def test_spawn_start_failure_finishes_job_and_next_job_runs(live, monkeypatch):
    module, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    plan = client.get("/api/scenarios/main/plan-versions").json()[-1]
    real_context = module.multiprocessing.get_context("spawn")

    class FailingProcessContext:
        Pipe = staticmethod(real_context.Pipe)

        @staticmethod
        def Process(*args, **kwargs):
            process = real_context.Process(*args, **kwargs)
            process.start = lambda: (_ for _ in ()).throw(OSError("injected spawn failure"))
            return process

    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _name: FailingProcessContext())
    endpoint = f"/api/v2/scenarios/main/plan-versions/{plan['id']}/analysis-jobs"
    failed = client.post(endpoint, headers={"Idempotency-Key": "spawn-fail-analysis"}, json={"analysis_type": "COST"})
    assert wait_job(client, failed.json()["id"])["status"] == "FAILED"
    monkeypatch.setattr(module.multiprocessing, "get_context", lambda _name: real_context)
    next_job = client.post(endpoint, headers={"Idempotency-Key": "spawn-next-analysis"}, json={"analysis_type": "COST"})
    assert wait_job(client, next_job.json()["id"])["status"] == "COMPLETED"


def test_cancelled_experiment_can_be_explicitly_rerun(live):
    module, client = live
    slots = module.experiment_slots
    assert all(slots.acquire(blocking=False) for _ in range(3))
    endpoint = "/api/scenarios/main/strategy-experiments"
    body = {"profile_ids": ["balanced"], "time_limit_seconds": 1}
    try:
        first = client.post(endpoint, json=body).json()
        cancellation = client.post(f"{endpoint}/{first['id']}/cancel")
        assert cancellation.json()["status"] == "CANCELLED"
        assert client.post(f"/api/v2/scenarios/main/jobs/JOB-EXP-{first['id']}/cancel").json()["status"] == "CANCELLED"
        second = client.post(endpoint, json=body)
        assert second.status_code == 202 and second.json()["id"] != first["id"]
    finally:
        for _ in range(3):
            slots.release()
    assert wait_job(client, f"JOB-EXP-{second.json()['id']}")["status"] == "COMPLETED"


def test_submit_success_ack_failure_releases_slot_exactly_once(live, monkeypatch):
    module, client = live
    slot, released = module.experiment_slots, Event()
    original_release = slot.release

    def release():
        original_release()
        released.set()

    monkeypatch.setattr(slot, "release", release)

    def fail_ack(_job_id):
        raise RuntimeError("injected outbox acknowledgement failure")

    monkeypatch.setattr(module.require_store(), "mark_outbox_dispatched_for_aggregate", fail_ack)
    response = client.post(
        "/api/scenarios/main/strategy-experiments", json={"profile_ids": ["balanced"], "time_limit_seconds": 1}
    )
    assert response.status_code == 202
    assert wait_job(client, f"JOB-EXP-{response.json()['id']}")["status"] == "COMPLETED"
    assert released.wait(10)
    held = [slot.acquire(blocking=False) for _ in range(3)]
    try:
        assert all(held)
        assert not slot.acquire(blocking=False)
    finally:
        for acquired in held:
            if acquired:
                original_release()


def test_other_process_coordinator_cannot_interrupt_live_owner(live):
    import subprocess
    import sys

    module, client = live
    code = "from fastapi.testclient import TestClient\nfrom backend.main import create_app\nimport sys\nwith TestClient(create_app(db_path=sys.argv[1])):\n    print('unexpected admission')\n"
    response = subprocess.run(
        [sys.executable, "-c", code, module.require_store().path], capture_output=True, text=True, timeout=10
    )
    assert response.returncode != 0 and "DATABASE_IN_USE" in response.stderr
    assert client.get("/api/scenarios/main").status_code == 200


def test_emergency_command_replays_after_emergency_start(live):
    _, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    payload = emergency_request("audit-replay-after-start")
    response = client.post("/api/scenarios/main/replan", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    emergency = next(item for item in result["assignments"] if item["work_order_id"] == "WO-AUDIT-EMG")
    predecessors = sorted(
        [
            a
            for a in result["assignments"]
            if a["technician_id"] == emergency["technician_id"] and a["sequence"] <= emergency["sequence"]
        ],
        key=lambda a: a["sequence"],
    )
    for assignment in predecessors:
        current = client.get("/api/scenarios/main").json()
        started = client.post(
            f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/start",
            json={
                "technician_id": assignment["technician_id"],
                "occurred_at": assignment["start_time"],
                "expected_revision": current["revision"],
                "idempotency_key": f"audit-start-{assignment['work_order_id']}",
            },
        )
        assert started.status_code == 200, started.text
        if assignment["work_order_id"] != "WO-AUDIT-EMG":
            completed = client.post(
                f"/api/scenarios/main/work-orders/{assignment['work_order_id']}/complete",
                json={
                    "technician_id": assignment["technician_id"],
                    "occurred_at": assignment["finish_time"],
                    "expected_revision": started.json()["scenario"]["revision"],
                    "idempotency_key": f"audit-complete-{assignment['work_order_id']}",
                },
            )
            assert completed.status_code == 200, completed.text
    replay = client.post("/api/scenarios/main/replan", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == result["id"]
    assert len(client.get("/api/scenarios/main/plan-versions").json()) == 2


def test_public_history_lists_are_paginated(live):
    _, client = live
    for _ in range(3):
        assert client.post("/api/scenarios/main/baseline").status_code == 200
    page = client.get("/api/scenarios/main/plan-versions?limit=1&offset=1")
    assert [p["number"] for p in page.json()] == [2]
    assert len(client.get("/api/v2/scenarios/main/jobs?limit=1&offset=1").json()) == 1
    assert client.get("/api/v2/scenarios/main/jobs?limit=101").status_code == 422
    plan = client.get("/api/scenarios/main/plan-versions").json()[-1]
    endpoint = f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs"
    for days in (1, 2):
        assert (
            client.post(
                endpoint, json={"analysis_type": "COST", "request": {"analysis_horizon": {"days": days}}}
            ).status_code
            == 201
        )
    assert [a["number"] for a in client.get(endpoint + "?limit=1&offset=1").json()] == [1]
    assert client.get(endpoint + "?limit=0").status_code == 422


def test_default_comparison_keeps_baseline_snapshot_and_scope(live):
    _, client = live
    assert client.post("/api/scenarios/main/baseline").status_code == 200
    assert client.put("/api/scenarios/main/work-orders/WO-1021", json={"note": "比较前的业务变化"}).status_code == 200
    assert (
        client.post("/api/scenarios/main/replan", json={"planning_time": 600, "time_limit_seconds": 1}).status_code
        == 200
    )
    comparison = client.get("/api/scenarios/main/comparison").json()
    assert not comparison["same_scenario_snapshot"] and not comparison["comparable"]
    assert "WO-1021" in comparison["modified_work_orders"]
    assert all(value is None for value in comparison["delta"].values())
    assert (
        client.post("/api/scenarios/main/replan", json={"planning_time": 660, "time_limit_seconds": 1}).status_code
        == 200
    )
    plans = client.get("/api/scenarios/main/plan-versions").json()
    comparison = client.get(f"/api/scenarios/main/comparison?before={plans[1]['id']}&after={plans[2]['id']}").json()
    assert comparison["same_scenario_snapshot"] and not comparison["comparable"]
    assert any("截点" in message for message in comparison["incomparable_reasons"])
