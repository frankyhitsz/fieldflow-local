import importlib

from fastapi.testclient import TestClient


def test_full_demo_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "test.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        scenarios = client.get("/api/scenarios").json()
        assert len(scenarios) >= 4
        main_scenario = next(item for item in scenarios if item["id"] == "main")

        baseline = client.post("/api/scenarios/main/baseline").json()
        optimized_response = client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1})
        assert optimized_response.status_code == 200
        optimized = optimized_response.json()
        assert optimized["objective"] < baseline["objective"]
        assert baseline["version"] == 1
        assert optimized["version"] == 2
        assert [item["number"] for item in client.get("/api/scenarios/main/plan-versions").json()] == [1, 2]

        vip_ids = {order["id"] for order in main_scenario["work_orders"] if order["vip"]}
        vip = next(a for a in optimized["assignments"] if a["work_order_id"] in vip_ids)
        lock = client.post("/api/scenarios/main/lock", json={
            "work_order_id": vip["work_order_id"], "technician_id": vip["technician_id"], "locked": True,
        })
        assert lock.status_code == 200

        replan = client.post("/api/scenarios/main/replan", json={"current_time": 600, "time_limit_seconds": 1})
        assert replan.status_code == 200
        assert replan.json()["kind"] == "replan"
        assert replan.json()["version"] == 3
        assert replan.json()["kpis"]["stability_rate"] is not None

        comparison = client.get("/api/scenarios/main/comparison")
        assert comparison.status_code == 200
        assert "changed_orders" in comparison.json()

        report = client.get("/api/scenarios/main/report")
        assert report.status_code == 200
        assert "FieldFlow 调度台" in report.text
        assert "数据与地图完全离线" not in report.text

        before_replan_count = len(client.get("/api/scenarios/main").json()["work_orders"])
        client.post("/api/scenarios/main/replan", json={"current_time": 600, "time_limit_seconds": 1})
        assert len(client.get("/api/scenarios/main").json()["work_orders"]) == before_replan_count

        edited = client.put("/api/scenarios/main/work-orders/WO-1021", json={
            "title": "人工修改后的线路检修", "is_emergency": True, "reported_at": 615,
        })
        assert edited.status_code == 200
        edited_scenario = edited.json()
        assert edited_scenario["revision"] == 2
        edited_order = next(item for item in edited_scenario["work_orders"] if item["id"] == "WO-1021")
        assert edited_order["is_emergency"] is True
        assert edited_order["drop_penalty"] >= 8000

        revised_plan = client.post("/api/scenarios/main/optimize", json={"strategy": "punctuality", "time_limit_seconds": 1})
        assert revised_plan.status_code == 200
        assert revised_plan.json()["scenario_revision"] == 2
        assert revised_plan.json()["strategy"] == "punctuality"
        revised_version = client.get("/api/scenarios/main/plan-versions").json()[-1]
        assert revised_version["relation"] == "fresh_after_data_change"
        assert set(revised_plan.json()["objective_breakdown"]) == {
            "travel", "sla_late", "overtime", "unassigned", "imbalance", "replan_changes"
        }
        latest_comparison = client.get("/api/scenarios/main/comparison")
        assert latest_comparison.status_code == 200
        assert latest_comparison.json()["after"]["id"] == revised_plan.json()["id"]

        updated_tech = client.put("/api/scenarios/main/technicians/TECH-01", json={"name": "林乔（早班）", "overtime_limit": 75})
        assert updated_tech.status_code == 200
        assert updated_tech.json()["revision"] == 3

        new_emergency = {
            "id": "WO-EMG-TEST", "customer_name": "应急客户", "title": "突发供电中断",
            "required_skills": ["electrical"], "location": {"x": 55, "y": 45},
            "service_duration": 30, "window_start": 630, "window_end": 750,
            "sla_deadline": 690, "priority": "urgent", "drop_penalty": 10000,
            "status": "pending", "vip": True, "is_emergency": True, "reported_at": 620, "note": "",
        }
        created = client.post("/api/scenarios/main/work-orders", json=new_emergency)
        assert created.status_code == 200
        assert created.json()["revision"] == 4
        assert any(item["id"] == "WO-EMG-TEST" and item["is_emergency"] for item in created.json()["work_orders"])

        incompatible_lock = client.post("/api/scenarios/main/lock", json={
            "work_order_id": "WO-1021", "technician_id": "TECH-03", "locked": True,
        })
        assert incompatible_lock.status_code == 422

        reset = client.post("/api/scenarios/main/reset")
        assert reset.status_code == 200
        assert reset.json()["revision"] == 5
        assert len(reset.json()["work_orders"]) == 24
        assert not any(item["id"] == "WO-EMG-TEST" for item in reset.json()["work_orders"])


def test_started_work_requires_local_replan_and_keeps_committed_assignment(monkeypatch, tmp_path):
    monkeypatch.setenv("FIELDFLOW_DB", str(tmp_path / "started.db"))
    import backend.main as main_module
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app) as client:
        original = client.post("/api/scenarios/main/optimize", json={"time_limit_seconds": 1}).json()
        started = min(original["assignments"], key=lambda item: item["start_time"])
        scenario = client.get("/api/scenarios/main").json()
        started_response = client.post(
            f"/api/scenarios/main/work-orders/{started['work_order_id']}/start",
            json={"technician_id": started["technician_id"], "occurred_at": started["start_time"], "expected_revision": scenario["revision"], "idempotency_key": "start-work-order-001"},
        )
        assert started_response.status_code == 200
        assert started_response.json()["event"]["action"] == "start"
        assert client.post("/api/scenarios/main/baseline").status_code == 409
        assert client.post("/api/scenarios/main/optimize").status_code == 409
        replanned = client.post("/api/scenarios/main/replan", json={"current_time": started["start_time"] + 1, "time_limit_seconds": 1})
        assert replanned.status_code == 200
        preserved = next(item for item in replanned.json()["assignments"] if item["work_order_id"] == started["work_order_id"])
        assert (preserved["technician_id"], preserved["start_time"], preserved["finish_time"]) == (
            started["technician_id"], started["start_time"], started["finish_time"],
        )
