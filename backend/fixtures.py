from __future__ import annotations

import copy
import random

from .models import (
    Point,
    Priority,
    ScheduleScenario,
    Skill,
    SolverConfig,
    Technician,
    WorkOrder,
)


SEED = 20260823


def _technicians() -> list[Technician]:
    return [
        Technician(id="TECH-01", name="林乔", skills=[Skill.electrical, Skill.network], shift_start=480, shift_end=927, start_location=Point(x=48, y=52), overtime_limit=60, color="#315c4b"),
        Technician(id="TECH-02", name="周岚", skills=[Skill.hvac, Skill.electrical], shift_start=510, shift_end=850, start_location=Point(x=48, y=52), overtime_limit=45, color="#a65b48"),
        Technician(id="TECH-03", name="沈川", skills=[Skill.network, Skill.hvac], shift_start=480, shift_end=930, start_location=Point(x=48, y=52), overtime_limit=60, color="#4b6481"),
        Technician(id="TECH-04", name="陈砚", skills=[Skill.electrical, Skill.hvac, Skill.network], shift_start=540, shift_end=1020, start_location=Point(x=48, y=52), overtime_limit=30, color="#8a7147"),
    ]


CUSTOMERS = [
    "云岫公寓", "青禾书店", "澄江酒店", "松隐园区", "拾光咖啡", "远帆物流",
    "东篱诊所", "镜湖小学", "新桥工坊", "竹里社区", "开元商厦", "栖云酒店",
    "长风仓储", "南枝画廊", "望岳科技", "汀兰餐厅", "九章事务所", "白鹭体育馆",
    "临溪超市", "春山养老院", "知行培训", "归谷民宿", "北辰制造", "月涌剧场",
]


TITLES = {
    Skill.electrical: ["配电箱巡检", "线路异常检修", "设备供电恢复"],
    Skill.hvac: ["空调制冷检修", "新风系统保养", "机组异响排查"],
    Skill.network: ["网络中断排障", "弱电机柜巡检", "终端接入安装"],
}


def _main_orders(seed: int = SEED) -> list[WorkOrder]:
    rng = random.Random(seed)
    skills = [Skill.electrical, Skill.hvac, Skill.network]
    anchors = [(18, 18), (78, 22), (20, 76), (78, 75), (52, 45), (64, 55)]
    orders: list[WorkOrder] = []
    for i in range(24):
        skill = skills[i % 3]
        ax, ay = anchors[i % len(anchors)]
        start = 510 + (i % 6) * 75 + rng.choice([-15, 0, 15])
        width = rng.choice([90, 120, 150])
        duration = rng.choice([35, 45, 50, 60, 75])
        priority = [Priority.high, Priority.normal, Priority.normal, Priority.low][i % 4]
        vip = i in {3, 11, 19}
        if vip:
            priority = Priority.urgent
        orders.append(
            WorkOrder(
                id=f"WO-{1021 + i}",
                customer_name=CUSTOMERS[i],
                title=rng.choice(TITLES[skill]),
                required_skills=[skill],
                location=Point(x=max(5, min(95, ax + rng.randint(-10, 10))), y=max(5, min(95, ay + rng.randint(-10, 10)))),
                service_duration=duration,
                window_start=start,
                window_end=start + width,
                sla_deadline=start + min(width, rng.choice([70, 90, 120])),
                priority=priority,
                drop_penalty={Priority.urgent: 8500, Priority.high: 4800, Priority.normal: 2500, Priority.low: 1400}[priority],
                vip=vip,
                note="已承诺到场，请优先保持安排稳定" if vip else "",
            )
        )
    by_id = {order.id: order for order in orders}
    # Five early electrical calls intentionally expose the greedy baseline's
    # limited look-ahead. Mutating after generation preserves the seed stream.
    for i, order in enumerate(orders[:5]):
        order.required_skills = [Skill.electrical]
        order.title = TITLES[Skill.electrical][i % len(TITLES[Skill.electrical])]
        order.window_start = 540
        order.window_end = 620
        order.sla_deadline = 660
        order.service_duration = 60
    # SLA calibration keeps the built-in baseline aligned with the demo brief:
    # 4 late, 310 travel minutes, 70 overtime minutes and 2 unassigned.
    by_id["WO-1025"].sla_deadline = 690
    by_id["WO-1026"].sla_deadline = 960
    by_id["WO-1029"].sla_deadline = 810
    return orders


def _scenario(scenario_id: str, name: str, description: str, orders: list[WorkOrder]) -> ScheduleScenario:
    return ScheduleScenario(
        id=scenario_id,
        name=name,
        description=description,
        technicians=_technicians(),
        work_orders=orders,
        solver_config=SolverConfig(),
        seed=SEED,
    )


def _strategy_fixture(scenario_id: str, technician_count: int, order_count: int, seed: int) -> ScheduleScenario:
    """Deterministic capacity-pressure fixture with real trade-offs between profiles."""
    rng = random.Random(seed)
    skills = [Skill.electrical, Skill.hvac, Skill.network]
    colors = ["#315c4b", "#a65b48", "#4b6481", "#8a7147", "#536f62", "#875f56", "#65748a", "#917b57", "#3f6856", "#b06a52", "#516a7c", "#756448"]
    technicians: list[Technician] = []
    for index in range(technician_count):
        primary = skills[index % len(skills)]
        secondary = skills[(index + 1) % len(skills)]
        tech_skills = [primary, secondary] if index % 4 else [primary]
        technicians.append(Technician(
            id=f"TECH-LAB-{index + 1:02d}", name=f"实验技师{index + 1:02d}", skills=tech_skills,
            shift_start=480 + (index % 3) * 20, shift_end=960 - (index % 4) * 15,
            start_location=Point(x=48 + (index % 2) * 4, y=48 + ((index // 2) % 2) * 4),
            overtime_limit=[20, 40, 60][index % 3], color=colors[index],
        ))
    anchors = [(14, 18), (84, 18), (18, 82), (82, 80), (50, 42), (52, 70)]
    orders: list[WorkOrder] = []
    for index in range(order_count):
        skill = skills[(index * 5 + index // 7) % len(skills)]
        ax, ay = anchors[index % len(anchors)]
        wave = index % 10
        start = 500 + wave * 43 + (index // 10 % 3) * 10 + rng.choice([-10, 0, 10])
        width = [70, 90, 120, 150][index % 4]
        duration = [30, 40, 50, 60, 75][(index * 3) % 5]
        priority = [Priority.urgent, Priority.high, Priority.normal, Priority.normal, Priority.low][index % 5]
        vip = index % 17 == 0
        required = [skill]
        if index % 19 == 0:
            required = [skill, skills[(skills.index(skill) + 1) % len(skills)]]
        orders.append(WorkOrder(
            id=f"WO-LAB-{index + 1:03d}", customer_name=f"{CUSTOMERS[index % len(CUSTOMERS)]}·{index + 1:02d}",
            title=rng.choice(TITLES[skill]), required_skills=required,
            location=Point(x=max(3, min(97, ax + rng.randint(-9, 9))), y=max(3, min(97, ay + rng.randint(-9, 9)))),
            service_duration=duration, window_start=start, window_end=start + width,
            sla_deadline=start + min(width, [55, 75, 100][index % 3]), priority=Priority.urgent if vip else priority,
            drop_penalty=10000 if vip else {Priority.urgent: 8500, Priority.high: 4800, Priority.normal: 2500, Priority.low: 1400}[priority],
            vip=vip, note="已向客户承诺到场" if vip else "",
        ))
    return ScheduleScenario(
        id=scenario_id,
        name="策略实验 · 中型" if order_count == 60 else "策略实验 · 压力型",
        description=f"{technician_count} 名技师、{order_count} 个工单，可重复比较不同策略",
        technicians=technicians,
        work_orders=orders,
        solver_config=SolverConfig(time_limit_seconds=2 if order_count == 60 else 4),
        seed=seed,
    )


def all_fixtures() -> dict[str, ScheduleScenario]:
    main = _scenario("main", "今日调度 · 城西片区", "24 个工单、4 名技师", _main_orders())

    skill_orders = copy.deepcopy(_main_orders()[:10])
    skill_orders.append(
        WorkOrder(
            id="WO-SKILL-01", customer_name="精密实验室", title="冷媒高压系统检修",
            required_skills=[Skill.hvac, Skill.network], location=Point(x=88, y=12), service_duration=90,
            window_start=600, window_end=720, sla_deadline=690, priority=Priority.urgent,
            drop_penalty=9000, note="需要同时具备暖通与网络技能",
        )
    )
    skill_scenario = _scenario("skill-shortage", "技能不足", "含一张需要两项技能的工单", skill_orders)
    # Remove the only technician with the exact composite skill combination.
    skill_scenario.technicians = [t for t in skill_scenario.technicians if t.id != "TECH-03"]
    for t in skill_scenario.technicians:
        if Skill.hvac in t.skills and Skill.network in t.skills:
            t.skills.remove(Skill.network)

    window_orders = copy.deepcopy(_main_orders()[:8])
    for i, order in enumerate(window_orders[:5]):
        order.window_start = 540
        order.window_end = 570
        order.sla_deadline = 570
        order.service_duration = 60
    window_scenario = _scenario("window-conflict", "时间窗冲突", "多个同技能工单争用同一窄时间窗", window_orders)

    emergency_orders = copy.deepcopy(_main_orders()[:16])
    emergency_scenario = _scenario("emergency", "突发工单重排", "已有安排中加入一张紧急工单", emergency_orders)

    strategy_medium = _strategy_fixture("strategy-medium", 8, 60, SEED + 60)
    strategy_stress = _strategy_fixture("strategy-stress", 12, 100, SEED + 100)

    return {s.id: s for s in [main, skill_scenario, window_scenario, emergency_scenario, strategy_medium, strategy_stress]}


def get_fixture(fixture_id: str) -> ScheduleScenario:
    fixtures = all_fixtures()
    if fixture_id not in fixtures:
        raise KeyError(f"Unknown fixture: {fixture_id}")
    return copy.deepcopy(fixtures[fixture_id])


def emergency_order() -> WorkOrder:
    return WorkOrder(
        id="WO-EMG-01",
        customer_name="衡安数据中心",
        title="核心机房断电告警",
        required_skills=[Skill.electrical],
        location=Point(x=67, y=42),
        service_duration=55,
        window_start=705,
        window_end=825,
        sla_deadline=765,
        priority=Priority.urgent,
        drop_penalty=12000,
        vip=True,
        is_emergency=True,
        reported_at=600,
        note="10:00 接报，业务核心系统受影响",
    )
