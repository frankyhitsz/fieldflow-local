from __future__ import annotations

from fastapi.testclient import TestClient

from backend.fixtures import emergency_order
from backend.main import app


def compact(result: dict) -> str:
    kpi = result["kpis"]
    return (
        f"目标={result['objective']:.0f} | SLA超时={kpi['sla_late_count']} | "
        f"行程={kpi['total_travel_minutes']}分 | 加班={kpi['total_overtime_minutes']}分 | "
        f"未分配={kpi['unassigned_count']}"
    )


with TestClient(app) as client:
    assert client.get("/api/health").status_code == 200
    scenario = client.post("/api/scenarios", json={"fixture_id": "main", "name": "自动演示检查"}).json()
    scenario_id = scenario["id"]
    baseline = client.post(f"/api/scenarios/{scenario_id}/baseline").json()
    optimized = client.post(f"/api/scenarios/{scenario_id}/optimize", json={"time_limit_seconds": 1}).json()
    assert optimized["solver_status"] == "FEASIBLE"
    assert optimized["objective"] < baseline["objective"]

    vip_ids = {"WO-1024", "WO-1032", "WO-1040"}
    locked = next(item for item in optimized["assignments"] if item["work_order_id"] in vip_ids)
    response = client.post(
        f"/api/scenarios/{scenario_id}/lock",
        json={"work_order_id": locked["work_order_id"], "technician_id": locked["technician_id"], "locked": True},
    )
    assert response.status_code == 200

    emergency = emergency_order().model_dump(mode="json")
    created = client.post(f"/api/scenarios/{scenario_id}/work-orders", json=emergency)
    assert created.status_code == 200
    replanned = client.post(
        f"/api/scenarios/{scenario_id}/replan",
        json={"current_time": 600, "time_limit_seconds": 1, "strategy": "stable"},
    ).json()
    preserved = next(item for item in replanned["assignments"] if item["work_order_id"] == locked["work_order_id"])
    assert preserved["technician_id"] == locked["technician_id"]
    assert replanned["kpis"]["stability_rate"] is not None
    assert client.get(f"/api/scenarios/{scenario_id}/comparison").status_code == 200
    assert client.get(f"/api/scenarios/{scenario_id}/report").status_code == 200

    print("✓ 人工基线       ", compact(baseline))
    print("✓ OR-Tools 优化  ", compact(optimized))
    print("✓ 显式突发单 + 锁定 + 局部重排", compact(replanned), f"| 稳定率={replanned['kpis']['stability_rate']:.0%}")
    print("✓ 对比与静态 HTML 报告已生成")
