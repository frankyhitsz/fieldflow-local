from __future__ import annotations

import html
from collections import defaultdict

from .models import (
    AnalysisIntegrityStatus,
    CurrentWorkOrderDisposition,
    PlanVersion,
    ReportMode,
    ScenarioOperationalView,
    ScheduleAssignment,
    ScheduleResult,
    ScheduleScenario,
)
from .timeutils import hhmm


def build_report(
    scenario: ScheduleScenario,
    result: ScheduleResult,
    *,
    mode: ReportMode = ReportMode.frozen_plan,
    plan: PlanVersion | None = None,
    operational_view: ScenarioOperationalView | None = None,
    execution_watermark: int = 0,
    proof_status: AnalysisIntegrityStatus = AnalysisIntegrityStatus.verified,
) -> str:
    if mode is ReportMode.current_operational and operational_view is None:
        raise ValueError("current operational report requires an operational view")
    kind_label = {"baseline": "人工基线", "optimized": "优化方案", "replan": "局部重排"}.get(result.kind, result.kind)
    orders = {o.id: o for o in scenario.work_orders}
    techs = {t.id: t for t in scenario.technicians}
    grouped: defaultdict[str, list[ScheduleAssignment]] = defaultdict(list)
    for assignment in result.assignments:
        if assignment.work_order_id in orders and assignment.technician_id in techs:
            grouped[assignment.technician_id].append(assignment)

    route_sections: list[str] = []
    for tech_id, assignments in grouped.items():
        rows: list[str] = []
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

    unassigned = (
        "".join(
            f"<li><b>{html.escape(item.work_order_id)}</b> · {html.escape(item.reason.value)}<br><span>{html.escape(item.detail)}</span></li>"
            for item in result.unassigned
        )
        or "<li>无未分配工单</li>"
    )
    k = result.kpis
    if mode is ReportMode.current_operational:
        assert operational_view is not None
        metrics = operational_view.current_metrics
        in_service = sum(
            item.disposition is CurrentWorkOrderDisposition.started for item in operational_view.work_orders
        )
        completed = sum(
            item.disposition is CurrentWorkOrderDisposition.completed for item in operational_view.work_orders
        )
        kpi_markup = (
            f'<div class="kpi"><span>当前可执行覆盖率</span><b>{metrics.current_actionable_coverage_rate:.0%}</b></div>'
            f'<div class="kpi"><span>新增未覆盖</span><b>{metrics.new_uncovered_count}<small> 单</small></b></div>'
            f'<div class="kpi"><span>失效分配</span><b>{metrics.invalid_assignment_count}<small> 单</small></b></div>'
            f'<div class="kpi"><span>服务中</span><b>{in_service}<small> 单</small></b></div>'
            f'<div class="kpi"><span>已完成</span><b>{completed}<small> 单</small></b></div>'
        )
        disclaimer = (
            '<p class="notice current"><b>当前运营报告</b>：覆盖率和执行状态按当前业务数据与执行水位计算；'
            "行程、SLA 与路线表仍引用当前活动方案。</p>"
        )
        metric_freshness = "CURRENT_OPERATIONAL"
        current_revision = operational_view.scenario_revision
        applicability = operational_view.plan_applicability
        applicability_text = (
            " · ".join(
                [
                    f"路线可执行 {'是' if applicability.route_executable else '否'}",
                    f"覆盖完整 {'是' if applicability.coverage_complete else '否'}",
                    f"规划最新 {'是' if applicability.planning_current else '否'}",
                    f"指标最新 {'是' if applicability.metrics_current else '否'}",
                ]
            )
            if applicability
            else "无活动方案适用性记录"
        )
    else:
        kpi_markup = (
            f'<div class="kpi"><span>发布时计划覆盖率</span><b>{k.completion_rate:.0%}</b></div>'
            f'<div class="kpi"><span>发布时 SLA 达成率</span><b>{k.committed_on_time_rate:.0%}</b></div>'
            f'<div class="kpi"><span>发布时总行程</span><b>{k.total_travel_minutes}<small> 分钟</small></b></div>'
            f'<div class="kpi"><span>发布时计划加班</span><b>{k.total_overtime_minutes}<small> 分钟</small></b></div>'
            f'<div class="kpi"><span>发布时未分配</span><b>{k.unassigned_count}<small> 单</small></b></div>'
        )
        disclaimer = (
            '<p class="notice frozen"><b>冻结方案报告</b>：本报告只反映该版本发布时的状态，不反映发布后的'
            "新增需求、数据变更或现场执行。</p>"
        )
        metric_freshness = "FROZEN_AT_PUBLICATION"
        current_revision = result.scenario_revision
        applicability_text = "不适用；冻结报告不推断当前可执行性"
    plan_number = plan.number if plan else result.version
    proof_label = proof_status.value
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(scenario.name)} · FieldFlow 调度报告</title>
<style>
body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Noto Sans SC",sans-serif;color:#202722;background:#f4f5f0;margin:0}}
main{{max-width:1080px;margin:auto;padding:48px 28px}}h1{{font-family:"Songti SC",serif;font-size:38px;font-weight:500;margin:.1em 0}}
.eyebrow{{color:#577365;letter-spacing:.18em;text-transform:uppercase}}.meta{{color:#68706a}}.status{{display:inline-block;border:1px solid #6c8075;padding:3px 9px;border-radius:99px}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:#ccd2cd;border:1px solid #ccd2cd;margin:32px 0}}.kpi{{background:#fff;padding:18px}}.kpi b{{display:block;font:26px "Songti SC",serif}}section{{margin:34px 0}}.provenance,.notice{{padding:12px 14px;background:#e9eee9;border-left:3px solid #577365}}.notice.frozen{{background:#fff7df;border-color:#9c6b16}}.notice.current{{background:#e8f1eb}}
table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px 12px;border-bottom:1px solid #dde1dd;text-align:left}}th{{color:#5e6b63;font-size:12px;letter-spacing:.08em}}small,.muted{{color:#6a746e}}li{{margin:10px 0}}@media print{{body{{background:white}}main{{padding:0}}}}
</style></head><body><main>
<div class="eyebrow">FIELDFLOW / {mode.value}</div><h1>{html.escape(scenario.name)}</h1>
<p class="meta">D{current_revision:03d} · V{plan_number:03d} · E{execution_watermark:03d} · {html.escape(kind_label)} · <span class="status">{result.solver_status.value}</span> · 计算 {result.runtime_ms} ms</p>
{disclaimer}<div class="kpis">{kpi_markup}</div>
<p>{html.escape(result.solver_note)}</p><p class="provenance">业务评分 {result.business_score if result.business_score is not None else result.objective:g} · {html.escape(result.business_score_policy_version)}<br>
求解器原始目标 {result.solver_objective_value if result.solver_objective_value is not None else "—"} · {html.escape(result.solver_name)} {html.escape(result.solver_version)}<br>
报告模式 {mode.value} · 指标新鲜度 {metric_freshness} · 证明状态 {html.escape(proof_label)}<br>
方案适用性：{html.escape(applicability_text)}<br>数据 D{current_revision:03d} · 执行水位 E{execution_watermark:03d} · 行程模型 {html.escape(result.travel_model_version)} · 指标口径 {html.escape(result.metric_policy_version)}</p>{"".join(route_sections)}
<section><h2>未分配诊断</h2><ul>{unassigned}</ul></section>
<footer class="muted">FieldFlow 调度台 · 生成于 {html.escape(result.created_at)}</footer>
</main></body></html>"""
