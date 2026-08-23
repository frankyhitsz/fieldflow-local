from __future__ import annotations

import html
from collections import defaultdict

from .models import ScheduleResult, ScheduleScenario
from .timeutils import hhmm


def build_report(scenario: ScheduleScenario, result: ScheduleResult) -> str:
    kind_label = {"baseline": "人工基线", "optimized": "优化方案", "replan": "局部重排"}.get(result.kind, result.kind)
    orders = {o.id: o for o in scenario.work_orders}
    techs = {t.id: t for t in scenario.technicians}
    grouped = defaultdict(list)
    for assignment in result.assignments:
        if assignment.work_order_id in orders and assignment.technician_id in techs:
            grouped[assignment.technician_id].append(assignment)

    route_sections = []
    for tech_id, assignments in grouped.items():
        rows = []
        for item in sorted(assignments, key=lambda a: a.sequence):
            order = orders[item.work_order_id]
            rows.append(
                f"<tr><td>{item.sequence}</td><td><b>{html.escape(order.id)}</b><br><small>{html.escape(order.customer_name)}</small></td>"
                f"<td>{html.escape(order.title)}</td><td>{hhmm(item.start_time)}–{hhmm(item.finish_time)}</td>"
                f"<td>{item.travel_minutes} 分钟</td><td>{'是' if item.locked else '—'}</td></tr>"
            )
        route_sections.append(
            f"<section><h2>{html.escape(techs[tech_id].name)} <small>{html.escape(tech_id)}</small></h2>"
            f"<table><thead><tr><th>顺序</th><th>工单</th><th>任务</th><th>执行时间</th><th>行程</th><th>锁定</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    unassigned = "".join(
        f"<li><b>{html.escape(item.work_order_id)}</b> · {html.escape(item.reason.value)}<br><span>{html.escape(item.detail)}</span></li>"
        for item in result.unassigned
    ) or "<li>无未分配工单</li>"
    k = result.kpis
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(scenario.name)} · FieldFlow 调度报告</title>
<style>
body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Noto Sans SC",sans-serif;color:#202722;background:#f4f5f0;margin:0}}
main{{max-width:1080px;margin:auto;padding:48px 28px}}h1{{font-family:"Songti SC",serif;font-size:38px;font-weight:500;margin:.1em 0}}
.eyebrow{{color:#577365;letter-spacing:.18em;text-transform:uppercase}}.meta{{color:#68706a}}.status{{display:inline-block;border:1px solid #6c8075;padding:3px 9px;border-radius:99px}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:#ccd2cd;border:1px solid #ccd2cd;margin:32px 0}}.kpi{{background:#fff;padding:18px}}.kpi b{{display:block;font:26px "Songti SC",serif}}section{{margin:34px 0}}.provenance{{padding:12px 14px;background:#e9eee9;border-left:3px solid #577365}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px 12px;border-bottom:1px solid #dde1dd;text-align:left}}th{{color:#5e6b63;font-size:12px;letter-spacing:.08em}}small,.muted{{color:#6a746e}}li{{margin:10px 0}}@media print{{body{{background:white}}main{{padding:0}}}}
</style></head><body><main>
<div class="eyebrow">FIELDFLOW / 调度报告</div><h1>{html.escape(scenario.name)}</h1>
<p class="meta">V{result.version:03d} · {html.escape(kind_label)} · <span class="status">{result.solver_status.value}</span> · 计算 {result.runtime_ms} ms</p>
<div class="kpis"><div class="kpi"><span>完成率</span><b>{k.completion_rate:.0%}</b></div><div class="kpi"><span>SLA 履约率</span><b>{k.committed_on_time_rate:.0%}</b></div><div class="kpi"><span>总行程</span><b>{k.total_travel_minutes}<small> 分钟</small></b></div><div class="kpi"><span>加班</span><b>{k.total_overtime_minutes}<small> 分钟</small></b></div><div class="kpi"><span>未分配</span><b>{k.unassigned_count}<small> 单</small></b></div></div>
<p>{html.escape(result.solver_note)}</p><p class="provenance">业务评分 {result.business_score if result.business_score is not None else result.objective:g} · {html.escape(result.business_score_policy_version)}<br>
求解器原始目标 {result.solver_objective_value if result.solver_objective_value is not None else '—'} · {html.escape(result.solver_name)} {html.escape(result.solver_version)}<br>
数据 D{result.scenario_revision:03d} · 行程模型 {html.escape(result.travel_model_version)} · 指标口径 {html.escape(result.metric_policy_version)}</p>{''.join(route_sections)}
<section><h2>未分配诊断</h2><ul>{unassigned}</ul></section>
<footer class="muted">FieldFlow 调度台 · 生成于 {html.escape(result.created_at)}</footer>
</main></body></html>"""
