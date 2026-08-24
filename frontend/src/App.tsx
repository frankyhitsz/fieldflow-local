import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ArrowDownRight, ArrowUpRight, BarChart3, CalendarDays, Check,
  Beaker, ChevronDown, CircleHelp, Clock3, Edit3, FileText, Gauge, GripVertical, Lock, Map as MapIcon,
  Plus, RefreshCw, Route, Sparkles, TimerReset, Unlock, Users,
  WandSparkles, X, Zap,
} from 'lucide-react'
import { api } from './api'
import { ReviewView, TechnicianEditor, TechniciansView, VersionsView, WorkOrderEditor } from './Management'
import { StrategyLab } from './StrategyLab'
import type { Assignment, Comparison, PlanVersion, Scenario, Schedule, Strategy, StrategyProfile, Technician, Unassigned, WorkOrder } from './types'

const hhmm = (minutes: number) => {
  const day = Math.floor(minutes / 1440)
  const clock = ((minutes % 1440) + 1440) % 1440
  const value = `${String(Math.floor(clock / 60)).padStart(2, '0')}:${String(clock % 60).padStart(2, '0')}`
  return day > 0 ? `次日 ${value}` : value
}
const pct = (value: number) => `${Math.round(value * 100)}%`
const priorityLabel = { urgent: '紧急', high: '高', normal: '普通', low: '低' }
const skillLabel: Record<string, string> = { electrical: '电气', hvac: '暖通', network: '网络' }
const kindLabel = { baseline: '人工基线', optimized: '优化方案', replan: '局部重排' }
const commandKey = (action: string) => `${action}:${crypto.randomUUID()}`
const executionDefaultTime = (scenario: Scenario, assignment: Assignment, action: 'start' | 'complete') => {
  const now = new Date()
  const localDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  if (scenario.planning_date === localDate) return now.getHours() * 60 + now.getMinutes()
  return action === 'start' ? assignment.start_time : Math.max(assignment.finish_time, assignment.start_time + 1)
}
const solverStatusLabel: Record<Schedule['solver_status'], string> = {
  OPTIMAL: '已证明最优', FEASIBLE: '已找到可行解', TIME_LIMIT_FEASIBLE: '限时内可行',
  TIME_LIMIT_NO_SOLUTION: '限时内无解', INFEASIBLE: '已证明无解', NO_SOLUTION: '未找到方案',
  INVALID_MODEL: '模型无效', FAILED: '求解失败', CANCELLED: '已取消', TIME_LIMIT: '已到时限',
}
type View = 'dispatch' | 'versions' | 'technicians' | 'lab' | 'review'

function InkMark() {
  return <div className="ink-mark" aria-hidden="true"><span>流</span></div>
}

function Sidebar({ scenarios, current, active, busy, onSelect, onNavigate }: { scenarios: Scenario[]; current?: Scenario; active: View; busy: boolean; onSelect: (id: string) => void; onNavigate: (view: View) => void }) {
  return <aside className="sidebar">
    <div className="brand"><InkMark /><div><strong>FieldFlow</strong><small>服务调度台</small></div></div>
    <nav aria-label="主导航">
      <button className={`nav-item ${active === 'dispatch' ? 'active' : ''}`} onClick={() => onNavigate('dispatch')}><MapIcon size={17} />今日调度{active === 'dispatch' && <span className="nav-dot" />}</button>
      <button className={`nav-item ${active === 'versions' ? 'active' : ''}`} onClick={() => onNavigate('versions')}><CalendarDays size={17} />方案版本</button>
      <button className={`nav-item ${active === 'technicians' ? 'active' : ''}`} onClick={() => onNavigate('technicians')}><Users size={17} />技师与技能</button>
      <button className={`nav-item ${active === 'lab' ? 'active' : ''}`} onClick={() => onNavigate('lab')}><Beaker size={17} />策略实验室</button>
      <button className={`nav-item ${active === 'review' ? 'active' : ''}`} onClick={() => onNavigate('review')}><BarChart3 size={17} />运营复盘</button>
    </nav>
    <div className="scenario-switch">
      <label htmlFor="scenario">业务场景</label>
      <div className="select-wrap"><select id="scenario" disabled={busy} value={current?.id || ''} onChange={e => onSelect(e.target.value)}>
        {scenarios.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
      </select><ChevronDown size={14} /></div>
      <p>{current?.description}</p>
    </div>
    <div className="day-note"><span className="pulse" /><div><strong>{current?.work_orders.filter(item => item.status === 'pending').length ?? 0} 单待安排</strong><small>{current?.technicians.length ?? 0} 名技师参与今日服务</small></div></div>
  </aside>
}

function SolverBadge({ schedule }: { schedule?: Schedule }) {
  if (!schedule) return <span className="solver-badge quiet">等待计划</span>
  const statusClass = schedule.solver_status.toLowerCase().replaceAll('_', '-')
  return <span className={`solver-badge ${statusClass}`} title={schedule.solver_note}><span />{solverStatusLabel[schedule.solver_status]} · {schedule.runtime_ms} ms</span>
}

function KpiStrip({ schedule, baseline }: { schedule?: Schedule; baseline?: Schedule }) {
  const k = schedule?.kpis
  const metrics = [
    { label: '计划覆盖率', value: k ? pct(k.completion_rate) : '—', sub: k ? `${schedule!.assignments.length} / ${schedule!.assignments.length + k.unassigned_count} 单已排入计划` : '尚未排程', icon: Check },
    { label: '计划 SLA 达成率', value: k ? pct(k.committed_on_time_rate) : '—', sub: k ? `已排工单计划按时率 ${pct(k.assigned_on_time_rate)}` : '尚未计算', icon: Gauge, warn: !!k?.sla_late_count || !!k?.unassigned_count },
    { label: '总行程', value: k ? `${k.total_travel_minutes}` : '—', unit: '分钟', delta: baseline && schedule && schedule.id !== baseline.id ? k!.total_travel_minutes - baseline.kpis.total_travel_minutes : null, icon: Route },
    { label: '总加班', value: k ? `${k.total_overtime_minutes}` : '—', unit: '分钟', delta: baseline && schedule && schedule.id !== baseline.id ? k!.total_overtime_minutes - baseline.kpis.total_overtime_minutes : null, icon: Clock3 },
    { label: '未分配', value: k ? `${k.unassigned_count}` : '—', unit: '工单', sub: k ? `${k.high_priority_missed} 单高优先级` : '尚未计算', icon: AlertTriangle, warn: !!k?.unassigned_count },
    { label: schedule?.kind === 'replan' ? '重排稳定率' : '占用利用率', value: k ? (schedule?.kind === 'replan' && k.stability_rate != null ? pct(k.stability_rate) : pct(k.average_occupied_utilization)) : '—', sub: schedule?.kind === 'replan' ? `原技师 ${k?.same_technician_rate == null ? '—' : pct(k.same_technician_rate)}` : '行程 + 等待 + 服务 ÷ 可用时间', icon: TimerReset },
  ]
  return <section className="kpi-strip" aria-label="关键业务指标">
    {metrics.map(({ label, value, unit, sub, delta, icon: Icon, warn }) => <div className={`kpi ${warn ? 'warn' : ''}`} key={label}>
      <div className="kpi-label"><Icon size={14} />{label}</div>
      <div className="kpi-value">{value}{unit && <small>{unit}</small>}</div>
      {delta != null ? <div className={`delta ${delta <= 0 ? 'good' : 'bad'}`}>{delta <= 0 ? <ArrowDownRight size={13} /> : <ArrowUpRight size={13} />}{Math.abs(delta)} 较基线</div> : <div className="kpi-sub">{sub}</div>}
    </div>)}
  </section>
}

function OrderQueue({ scenario, schedule, selectedId, onSelect, onAdd }: { scenario: Scenario; schedule?: Schedule; selectedId?: string; onSelect: (id: string) => void; onAdd: () => void }) {
  const [tab, setTab] = useState<'risk' | 'all'>('risk')
  const assignmentMap = new Map(schedule?.assignments.map(a => [a.work_order_id, a]))
  const unassignedMap = new Map(schedule?.unassigned.map(u => [u.work_order_id, u]))
  const ranked = [...scenario.work_orders].sort((a, b) => {
    const au = unassignedMap.has(a.id) ? 0 : assignmentMap.get(a.id)?.sla_late_minutes ? 1 : 2
    const bu = unassignedMap.has(b.id) ? 0 : assignmentMap.get(b.id)?.sla_late_minutes ? 1 : 2
    return au - bu || a.sla_deadline - b.sla_deadline
  })
  const visible = tab === 'all' || !schedule ? ranked : ranked.filter(order => unassignedMap.has(order.id) || !!assignmentMap.get(order.id)?.sla_late_minutes)
  return <section className="queue panel">
    <div className="panel-heading"><div><span className="eyebrow">WORK ORDERS</span><h2>工单队列</h2></div><div className="heading-actions"><span className="count">{scenario.work_orders.length}</span><button className="mini-add" onClick={onAdd} aria-label="新增工单"><Plus size={13} /></button></div></div>
    <div className="queue-tabs"><button className={tab === 'risk' ? 'active' : ''} onClick={() => setTab('risk')}>风险优先</button><button className={tab === 'all' ? 'active' : ''} onClick={() => setTab('all')}>全部</button></div>
    <div className="queue-list">
      {visible.map(order => {
        const assignment = assignmentMap.get(order.id)
        const unassigned = unassignedMap.get(order.id)
        return <button className={`order-card ${selectedId === order.id ? 'selected' : ''} ${unassigned ? 'unassigned' : ''}`} key={order.id} onClick={() => onSelect(order.id)}>
          <span className={`priority ${order.priority}`}>{priorityLabel[order.priority]}</span>
          <div className="order-main"><div className="order-id">{order.id}{order.is_emergency && <span className="emergency-tag">突发</span>}{order.vip && <span className="vip">VIP</span>}</div><strong>{order.customer_name}</strong><small>{order.title}</small></div>
          <div className="order-meta"><span><Clock3 size={12} />{hhmm(order.window_start)}–{hhmm(order.window_end)}</span><span className={unassigned ? 'danger' : assignment?.sla_late_minutes ? 'danger' : ''}>{unassigned ? '待处理' : assignment ? `${hhmm(assignment.start_time)} 开始` : '未排程'}</span></div>
        </button>
      })}
      {!visible.length && <div className="queue-empty"><Check size={18} /><strong>当前没有风险工单</strong><span>所有已分配工单均满足 SLA。</span></div>}
    </div>
  </section>
}

function RouteMap({ scenario, schedule, selectedId, onSelect }: { scenario: Scenario; schedule?: Schedule; selectedId?: string; onSelect: (id: string) => void }) {
  const orderMap = new Map(scenario.work_orders.map(o => [o.id, o]))
  const assignedMap = new Map(schedule?.assignments.map(a => [a.work_order_id, a]))
  const unassigned = new Set(schedule?.unassigned.map(u => u.work_order_id))
  const routes = scenario.technicians.map(tech => ({
    tech,
    assignments: (schedule?.assignments.filter(a => a.technician_id === tech.id && orderMap.has(a.work_order_id)) || []).sort((a, b) => a.sequence - b.sequence),
  }))
  const depots = Array.from(new Map(scenario.technicians.map(tech => [`${tech.start_location.x}:${tech.start_location.y}`, tech.start_location])).values())
  return <section className="map-panel panel">
    <div className="panel-heading map-heading"><div><span className="eyebrow">服务区域 / 位置与行程估算</span><h2>服务位置图</h2></div><div className="map-legend"><span><i className="legend depot" />技师出发点</span><span><i className="legend job" />已排</span><span><i className="legend risk" />待排</span></div></div>
    <div className="map-canvas">
      <svg viewBox="0 0 100 100" role="img" aria-label="工单位置与技师路线图">
        <defs>
          <pattern id="grid" width="10" height="10" patternUnits="userSpaceOnUse"><path d="M 10 0 L 0 0 0 10" fill="none" stroke="#8e9a92" strokeWidth=".18" opacity=".38" /></pattern>
          <filter id="ink-edge" x="-10%" y="-10%" width="120%" height="120%"><feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="2" seed="17" result="noise"/><feDisplacementMap in="SourceGraphic" in2="noise" scale=".45" /></filter>
        </defs>
        <rect width="100" height="100" fill="url(#grid)" />
        <path className="terrain" d="M0,20 C12,15 16,25 27,19 S42,8 51,16 S69,25 79,14 S91,9 100,14 M0,75 C14,64 27,81 42,73 S67,63 80,72 S92,80 100,71" />
        {routes.map(({ tech, assignments }) => {
          if (!assignments.length) return null
          const routeOrders = assignments.map(a => orderMap.get(a.work_order_id)).filter((item): item is WorkOrder => !!item)
          const points = [tech.start_location, ...routeOrders.map(order => order.location)]
          const d = points.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' ')
          return <g key={tech.id} filter="url(#ink-edge)"><path d={d} className="route-shadow" /><path d={d} className="route-line" style={{ stroke: tech.color }} /></g>
        })}
        {depots.map((point, index) => <g className="depot-pin" key={`${point.x}:${point.y}`}><title>{`出发点 ${index + 1}`}</title><circle cx={point.x} cy={point.y} r="3.6" /><circle cx={point.x} cy={point.y} r="7" className="depot-ring" /><path d={`M${point.x - 2} ${point.y + 2}v-4l2-1.6 2 1.6v4z`} /></g>)}
        {scenario.work_orders.map(order => {
          const a = assignedMap.get(order.id)
          const tech = a ? scenario.technicians.find(t => t.id === a.technician_id) : undefined
          const isSelected = selectedId === order.id
          return <g key={order.id} className={`job-pin ${unassigned.has(order.id) ? 'risk' : ''} ${isSelected ? 'selected' : ''}`} onClick={() => onSelect(order.id)} role="button" tabIndex={0} aria-label={`查看工单 ${order.id}`} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(order.id) } }}>
            {isSelected && <circle cx={order.location.x} cy={order.location.y} r="4.7" className="selection-ring" />}
            <circle cx={order.location.x} cy={order.location.y} r={isSelected ? 2.5 : 1.8} style={{ fill: unassigned.has(order.id) ? '#b6533f' : tech?.color || '#707a73' }} />
            {order.vip && <circle cx={order.location.x} cy={order.location.y} r="3.1" className="vip-ring" />}
            <circle cx={order.location.x} cy={order.location.y} r="4" className="hit-area" />
          </g>
        })}
      </svg>
      <div className="map-stamp"><b>{schedule?.assignments.length || 0}</b><span>已落点</span></div>
    </div>
  </section>
}

function Timeline({ scenario, schedule, onSelect, currentTime }: { scenario: Scenario; schedule?: Schedule; onSelect: (id: string) => void; currentTime: number }) {
  const orders = new Map(scenario.work_orders.map(o => [o.id, o]))
  const assignments = schedule?.assignments || []
  const start = Math.floor(Math.min(currentTime, ...scenario.technicians.map(item => item.shift_start), ...assignments.map(item => item.arrival_time)) / 60) * 60
  const end = Math.ceil(Math.max(currentTime + 60, ...scenario.technicians.map(item => item.shift_end + item.overtime_limit), ...assignments.map(item => item.finish_time)) / 60) * 60
  const tickCount = 6
  const ticks = Array.from({ length: tickCount }, (_, index) => Math.round((start + (end - start) * index / (tickCount - 1)) / 30) * 30)
  const pos = (m: number) => Math.max(0, Math.min(100, ((m - start) / (end - start)) * 100))
  return <section className="timeline panel">
    <div className="panel-heading"><div><span className="eyebrow">TECHNICIAN RUNS</span><h2>技师时间轴</h2></div><CircleHelp size={16} className="muted-icon" /></div>
    <div className="time-head">{ticks.map((tick, index) => <span key={`${tick}-${index}`}>{hhmm(tick)}</span>)}</div>
    <div className="tech-runs">
      {scenario.technicians.map(tech => {
        const items = (schedule?.assignments.filter(a => a.technician_id === tech.id && orders.has(a.work_order_id)) || []).sort((a, b) => a.start_time - b.start_time)
        const kpi = schedule?.kpis.technician.find(k => k.technician_id === tech.id)
        return <div className="tech-run" key={tech.id}>
          <div className="tech-info"><div className="tech-avatar" style={{ '--tech': tech.color } as React.CSSProperties}>{tech.name.slice(-1)}</div><div><strong>{tech.name}</strong><small>{tech.skills.map(s => skillLabel[s]).join(' · ')}</small></div><span className="util">{kpi ? pct(kpi.utilization) : '—'}</span></div>
          <div className="run-track" style={{ '--tick-pct': `${100 / (tickCount - 1)}%` } as React.CSSProperties}>
            <div className="shift-range" style={{ left: `${pos(tech.shift_start)}%`, width: `${pos(tech.shift_end) - pos(tech.shift_start)}%` }} />
            <div className="replan-line" style={{ left: `${pos(currentTime)}%` }} />
            {items.map(item => {
              const order = orders.get(item.work_order_id)
              if (!order) return null
              return <button key={item.work_order_id} className={`run-block ${item.sla_late_minutes ? 'late' : ''} ${item.locked ? 'locked' : ''}`} style={{ left: `${pos(item.start_time)}%`, width: `${Math.max(5, pos(item.finish_time) - pos(item.start_time))}%`, '--tech': tech.color } as React.CSSProperties} onClick={() => onSelect(item.work_order_id)} title={`${item.work_order_id} · ${order.customer_name} · ${hhmm(item.start_time)}–${hhmm(item.finish_time)}`}><span>{item.sequence}</span>{item.locked && <Lock size={9} />}</button>
            })}
          </div>
        </div>
      })}
    </div>
    <div className="timeline-note"><span className="now-line" />虚线为 {hhmm(currentTime)} 重排时点；浅色范围为正常班次</div>
  </section>
}

function ExplanationPanel({ scenario, schedule, orderId, readOnly = false, onClose, onLock, onEdit, onAssign, onExecute }: { scenario: Scenario; schedule?: Schedule; orderId: string; readOnly?: boolean; onClose: () => void; onLock: (assignment: Assignment, locked: boolean) => void; onEdit: (order: WorkOrder) => void; onAssign: (orderId: string, technicianId: string) => void; onExecute: (assignment: Assignment, action: 'start' | 'complete') => void }) {
  const order = scenario.work_orders.find(o => o.id === orderId)
  const assignment = schedule?.assignments.find(a => a.work_order_id === orderId)
  const unassigned = schedule?.unassigned.find(u => u.work_order_id === orderId)
  const tech = assignment ? scenario.technicians.find(t => t.id === assignment.technician_id) : undefined
  const candidates = order ? scenario.technicians.filter(t => order.required_skills.every(s => t.skills.includes(s))) : []
  const [manualTech, setManualTech] = useState('')
  useEffect(() => setManualTech(assignment?.technician_id || candidates[0]?.id || ''), [orderId, assignment?.technician_id, candidates[0]?.id])
  if (!order) return null
  return <aside className="detail-drawer" aria-label="工单详情">
    <div className="drawer-top"><div><span className={`priority ${order.priority}`}>{priorityLabel[order.priority]}</span>{order.is_emergency && <span className="emergency-tag">突发</span>}<span className="mono">{order.id}</span></div><div>{!readOnly && <button className="icon-btn" onClick={() => onEdit(order)} aria-label="编辑工单"><Edit3 size={16} /></button>}<button className="icon-btn" onClick={onClose} aria-label="关闭工单详情"><X size={18} /></button></div></div>
    {readOnly && <div className="readonly-note">这是只读历史。如需使用，请回到版本页恢复。</div>}
    <h2>{order.customer_name}</h2><p className="detail-title">{order.title}</p><span className={`execution-status ${order.status}`}>{{ pending: '待处理', started: '服务中', completed: '已完成' }[order.status]}</span>
    <div className="detail-grid"><div><small>允许开始窗口</small><strong>{hhmm(order.window_start)}–{hhmm(order.window_end)}</strong></div><div><small>SLA 截止</small><strong className={assignment?.sla_late_minutes ? 'danger' : ''}>{hhmm(order.sla_deadline)}</strong></div><div><small>服务时长</small><strong>{order.service_duration} 分钟</strong></div><div><small>所需技能</small><strong>{order.required_skills.map(s => skillLabel[s]).join('、')}</strong></div></div>
    {assignment ? <>
      <div className="assignment-summary"><div className="tech-avatar" style={{ '--tech': tech?.color } as React.CSSProperties}>{tech?.name.slice(-1)}</div><div><small>当前分配</small><strong>{tech?.name} · {hhmm(assignment.start_time)} 到场</strong></div><span>{assignment.travel_minutes}′ 行程</span></div>
      <div className="explain"><h3><Sparkles size={15} />安排原因</h3><ol>{assignment.explanation.map((line, i) => <li key={i}><span>{i + 1}</span>{line}</li>)}</ol></div>
      {!readOnly && order.status === 'pending' && <div className="execution-actions"><button className={`lock-action ${assignment.locked ? 'locked' : ''}`} onClick={() => onLock(assignment, !assignment.locked)}>{assignment.locked ? <Unlock size={16} /> : <Lock size={16} />}{assignment.locked ? '解除人工锁定' : '锁定此工单与技师'}</button><button className="start-action" onClick={() => onExecute(assignment, 'start')}><Clock3 size={16} />开始服务</button></div>}
      {!readOnly && order.status === 'started' && <button className="complete-action" onClick={() => onExecute(assignment, 'complete')}><Check size={16} />完成服务</button>}
    </> : <UnassignedDetail item={unassigned} candidates={candidates} />}
    {!readOnly && order.status === 'pending' && candidates.length > 0 && <div className="manual-assign"><label>手工改派</label><div><select value={manualTech} onChange={e => setManualTech(e.target.value)}>{candidates.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.name} · {candidate.skills.map(skill => skillLabel[skill]).join('、')}</option>)}</select><button disabled={!manualTech || manualTech === assignment?.technician_id} onClick={() => onAssign(order.id, manualTech)}>改派并锁定</button></div><small>保存后会局部重排未开始工单，已执行安排不变。</small></div>}
    {order.note && <div className="order-note"><b>现场备注</b>{order.note}</div>}
  </aside>
}

function UnassignedDetail({ item, candidates }: { item?: Unassigned; candidates: Technician[] }) {
  return <div className="unassigned-detail"><AlertTriangle size={18} /><div><strong>{item?.reason || '尚未进入计划'}</strong><p>{item?.detail || '请先生成一个排程方案。'}</p>{item?.suggestions.map(s => <span key={s}>· {s}</span>)}<small>{candidates.length ? `${candidates.length} 名技师具备所需技能` : '没有完整技能匹配'}</small></div></div>
}

function CompareDrawer({ data, onClose }: { data: Comparison; onClose: () => void }) {
  const rows = [
    ['加权目标', data.before.objective, data.after.objective, data.delta.objective],
    ['SLA 超时', data.before.kpis.sla_late_count, data.after.kpis.sla_late_count, data.delta.sla_late_count],
    ['行程分钟', data.before.kpis.total_travel_minutes, data.after.kpis.total_travel_minutes, data.delta.travel_minutes],
    ['加班分钟', data.before.kpis.total_overtime_minutes, data.after.kpis.total_overtime_minutes, data.delta.overtime_minutes],
    ['未分配', data.before.kpis.unassigned_count, data.after.kpis.unassigned_count, data.delta.unassigned_count],
  ]
  const tradeoffs = data.comparable ? [
    Number(data.delta.unassigned_count) < 0 ? `多完成 ${-Number(data.delta.unassigned_count)} 单` : Number(data.delta.unassigned_count) > 0 ? `少完成 ${data.delta.unassigned_count} 单` : '',
    Number(data.delta.sla_late_count) > 0 ? `SLA 超时增加 ${data.delta.sla_late_count} 单` : Number(data.delta.sla_late_count) < 0 ? `SLA 超时减少 ${-Number(data.delta.sla_late_count)} 单` : '',
    Number(data.delta.overtime_minutes) > 0 ? `加班增加 ${data.delta.overtime_minutes} 分钟` : Number(data.delta.overtime_minutes) < 0 ? `加班减少 ${-Number(data.delta.overtime_minutes)} 分钟` : '',
  ].filter(Boolean) : []
  const objectiveReduction = data.comparable && data.delta.objective != null && data.before.objective !== 0
    ? Math.round((1 - data.after.objective / data.before.objective) * 100)
    : undefined
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="compare-modal" role="dialog" aria-modal="true" aria-labelledby="compare-title" onMouseDown={e => e.stopPropagation()}>
    <div className="compare-head"><div><span className="eyebrow">BEFORE / AFTER</span><h2 id="compare-title">方案对比</h2><p>V{String(data.before.version).padStart(3, '0')} 与 V{String(data.after.version).padStart(3, '0')} · {kindLabel[data.after.kind]}</p></div><button className="icon-btn" onClick={onClose} aria-label="关闭方案对比"><X /></button></div>
    <div className="compare-status"><SolverBadge schedule={data.after} /><span>{data.after.solver_note}</span></div>
    {!data.comparable && <div className="compare-warning"><AlertTriangle size={17} /><div><b>两版使用的业务数据不同，不能直接判断方案优劣</b><span>共同工单 {data.common_work_order_count} 个；新增 {data.added_work_orders.length} 个，移除 {data.removed_work_orders.length} 个，修改 {data.modified_work_orders.length} 个。下表只展示原始数值变化。</span></div></div>}
    <table className="compare-table"><thead><tr><th>指标</th><th>方案 A</th><th>方案 B</th><th>变化</th></tr></thead><tbody>{rows.map(([label, before, after, delta]) => <tr key={String(label)}><td>{label}</td><td>{before}</td><td><b>{after}</b></td><td className={delta == null || !data.comparable ? '' : Number(delta) <= 0 ? 'good' : 'bad'}>{delta == null ? '—' : `${Number(delta) > 0 ? '+' : ''}${delta}`}</td></tr>)}</tbody></table>
    {tradeoffs.length > 0 && <p className="compare-explain"><b>变化：</b>{tradeoffs.join('；')}。“计算用时”只指生成方案所花的时间。</p>}
    <div className="compare-footer"><div><b>{data.changed_orders.length}</b><span>个工单安排发生变化</span></div><div><b>{data.after.kpis.stability_rate == null ? '—' : pct(data.after.kpis.stability_rate)}</b><span>重排稳定率</span></div><div><b>{objectiveReduction == null ? '—' : `${objectiveReduction > 0 ? '+' : ''}${objectiveReduction}%`}</b><span>{!data.comparable ? '需求快照不同，不比较目标值' : data.delta.objective == null ? '不同策略不比较目标值' : data.before.objective === 0 ? '基准值为 0，不计算百分比' : '目标值降幅（正数为改善）'}</span></div></div>
  </section></div>
}

function ExecutionDialog({ scenario, assignment, action, onClose, onSubmit }: { scenario: Scenario; assignment: Assignment; action: 'start' | 'complete'; onClose: () => void; onSubmit: (occurredAt: number, earlyStartOverrideReason: string, estimatedRemainingMinutes: number | undefined, note: string) => void }) {
  const order = scenario.work_orders.find(item => item.id === assignment.work_order_id)!
  const technician = scenario.technicians.find(item => item.id === assignment.technician_id)
  const [occurredAt, setOccurredAt] = useState(() => executionDefaultTime(scenario, assignment, action))
  const [overrideReason, setOverrideReason] = useState('')
  const [estimatedRemainingMinutes, setEstimatedRemainingMinutes] = useState(order.service_duration)
  const [note, setNote] = useState('')
  const customerReadyAt = Math.max(order.window_start, order.reported_at ?? 0)
  const early = action === 'start' && occurredAt < customerReadyAt
  return <div className="modal-backdrop" onMouseDown={onClose}><form className="editor-modal compact execution-modal" role="dialog" aria-modal="true" aria-labelledby="execution-title" onMouseDown={event => event.stopPropagation()} onSubmit={event => { event.preventDefault(); onSubmit(occurredAt, overrideReason.trim(), action === 'start' ? estimatedRemainingMinutes : undefined, note.trim()) }}>
    <div className="editor-head"><div><span className="eyebrow">ACTUAL EXECUTION</span><h2 id="execution-title">{action === 'start' ? '登记开始服务' : '登记完成服务'}</h2></div><button type="button" className="icon-btn" onClick={onClose} aria-label="关闭执行登记"><X size={18} /></button></div>
    <div className="execution-summary"><b>{assignment.work_order_id}</b><span>{technician?.name} · 计划 {hhmm(assignment.start_time)}–{hhmm(assignment.finish_time)}</span></div>
    <div className="form-grid">
      <label>实际发生时间（当日起分钟）<input aria-label="实际发生时间" type="number" min="0" max="2280" step="1" value={occurredAt} onChange={event => setOccurredAt(Number(event.target.value))} /><small>{hhmm(occurredAt)}，与重排时点相互独立</small></label>
      <label>执行技师<input aria-label="执行技师" value={technician?.name || assignment.technician_id} disabled /></label>
      {action === 'start' && <label>预计剩余服务（分钟）<input aria-label="预计剩余服务" type="number" min="1" max="480" step="1" value={estimatedRemainingMinutes} onChange={event => setEstimatedRemainingMinutes(Number(event.target.value))} /><small>局部重排会据此冻结技师容量；超出估计后使用场景保守默认值</small></label>}
      {early && <label className="span-2">提前开始原因<textarea aria-label="提前开始原因" required value={overrideReason} onChange={event => setOverrideReason(event.target.value)} placeholder={`客户允许时间为 ${hhmm(customerReadyAt)}，请记录授权原因`} /></label>}
      <label className="span-2">执行备注<textarea aria-label="执行备注" value={note} onChange={event => setNote(event.target.value)} placeholder="可记录客户确认、现场情况或偏差原因" /></label>
    </div>
    <div className="editor-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" type="submit" disabled={early && !overrideReason.trim()}><Check size={15} />确认登记</button></div>
  </form></div>
}

export default function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [scenario, setScenario] = useState<Scenario>()
  const [schedule, setSchedule] = useState<Schedule>()
  const [baseline, setBaseline] = useState<Schedule>()
  const [plans, setPlans] = useState<PlanVersion[]>([])
  const [profiles, setProfiles] = useState<StrategyProfile[]>([])
  const [historicalScenario, setHistoricalScenario] = useState<Scenario>()
  const [view, setView] = useState<View>('dispatch')
  const [strategy, setStrategy] = useState<Strategy>('balanced')
  const [replanTime, setReplanTime] = useState(600)
  const [selectedId, setSelectedId] = useState<string>()
  const [working, setWorking] = useState<string>()
  const [loadingScenarioId, setLoadingScenarioId] = useState<string>()
  const [comparison, setComparison] = useState<Comparison>()
  const [toast, setToast] = useState<string>()
  const [loadError, setLoadError] = useState<string>()
  const [workEditor, setWorkEditor] = useState<{ initial?: WorkOrder; emergencyPreset?: boolean }>()
  const [techEditor, setTechEditor] = useState<{ initial?: Technician }>()
  const [executionEditor, setExecutionEditor] = useState<{ assignment: Assignment; action: 'start' | 'complete' }>()
  const booted = useRef(false)
  const loadSequence = useRef(0)
  const activeScenarioId = useRef<string | undefined>(undefined)

  const loadScenario = async (id: string) => {
    const previousId = activeScenarioId.current
    activeScenarioId.current = id
    const sequence = ++loadSequence.current
    setLoadingScenarioId(id)
    setWorking('正在读取业务场景')
    setLoadError(undefined)
    try {
      const [next, planItems] = await Promise.all([api.scenario(id), api.planVersions(id)])
      if (sequence !== loadSequence.current) return
      setScenario(next)
      setPlans(planItems)
      setHistoricalScenario(undefined)
      setWorkEditor(undefined); setTechEditor(undefined); setExecutionEditor(undefined)
      const active = planItems.find(item => item.active)
      setSchedule(active?.selected)
      if (active) {
        const detail = await api.planVersion(id, active.id)
        if (sequence !== loadSequence.current) return
        setBaseline(detail.artifacts.find(item => item.role === 'baseline')?.schedule || [...planItems].reverse().find(item => item.action === 'baseline' && item.data_revision === next.revision)?.selected)
      } else setBaseline(undefined)
      setSelectedId(undefined)
    } catch (error) {
      if (sequence === loadSequence.current) activeScenarioId.current = previousId
      const message = error instanceof Error ? error.message : '场景加载失败'
      setLoadError(message); setToast(message)
    } finally { if (sequence === loadSequence.current) { setWorking(undefined); setLoadingScenarioId(undefined) } }
  }

  useEffect(() => {
    if (booted.current) return
    booted.current = true
    Promise.all([api.scenarios(), api.strategyProfiles()]).then(([items, strategyProfiles]) => {
      if (!items.length) throw new Error('没有可用业务场景')
      setScenarios(items)
      setProfiles(strategyProfiles)
      return loadScenario(items.find(s => s.id === 'main')?.id || items[0].id)
    }).catch(error => { setLoadError(error.message); setToast(error.message) })
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(undefined), 4200)
    return () => window.clearTimeout(timer)
  }, [toast])

  const act = async (label: string, call: () => Promise<Schedule>, success: string) => {
    setWorking(label)
    try {
      const result = await call()
      if (activeScenarioId.current !== result.scenario_id) return
      const planItems = await api.planVersions(result.scenario_id)
      if (activeScenarioId.current !== result.scenario_id) return
      setSchedule(planItems.find(item => item.active)?.selected || result)
      setPlans(planItems); setHistoricalScenario(undefined)
      if (result.kind === 'baseline') setBaseline(result)
      setToast(success)
    } catch (error) { setToast(error instanceof Error ? error.message : '操作失败') }
    finally { setWorking(undefined) }
  }

  const displayScenario = historicalScenario || scenario
  const selected = useMemo(() => displayScenario?.work_orders.find(o => o.id === selectedId), [displayScenario, selectedId])
  const dateText = scenario ? new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date(`${scenario.planning_date}T00:00:00`)) : ''

  if (!scenario) return <main className="boot-screen"><InkMark /><h1>FieldFlow</h1><p>{loadError || working || '正在打开调度台…'}</p>{loadError && <button onClick={() => window.location.reload()}>重新连接</button>}</main>
  const shownScenario = historicalScenario || scenario
  const serviceWindow = shownScenario.technicians.length
    ? `${hhmm(Math.min(...shownScenario.technicians.map(item => item.shift_start)))}–${hhmm(Math.max(...shownScenario.technicians.map(item => item.shift_end)))}`
    : '未配置班次'
  const skillCount = new Set([
    ...shownScenario.technicians.flatMap(item => item.skills),
    ...shownScenario.work_orders.flatMap(item => item.required_skills),
  ]).size
  const activePlan = plans.find(item => item.active)
  const partialCoverage = !historicalScenario && activePlan?.coverage_status === 'PARTIAL_NEW_DEMAND'
  const executionProgress = !historicalScenario && !!schedule && activePlan?.selected.id === schedule.id
    && schedule.scenario_revision !== scenario.revision
    && scenario.work_orders.some(order => order.status !== 'pending')

  const invalidatePlan = (next: Scenario, message: string) => {
    setScenario(next); setSchedule(undefined); setBaseline(undefined); setHistoricalScenario(undefined); setSelectedId(undefined)
    setScenarios(current => current.map(item => item.id === next.id ? next : item))
    setToast(`${message}，请重新生成方案`)
  }

  const applyScenarioEdit = async (next: Scenario, message: string) => {
    if (!next.work_orders.some(order => order.status !== 'pending')) {
      invalidatePlan(next, message)
      return
    }
    const planItems = await api.planVersions(next.id)
    if (activeScenarioId.current !== next.id) return
    const active = planItems.find(item => item.active)
    setScenario(next); setPlans(planItems); setSchedule(active?.selected); setHistoricalScenario(undefined)
    setScenarios(current => current.map(item => item.id === next.id ? next : item))
    setToast(`${message}；当前执行方案已保留，请完成服务或局部重排`)
  }

  const saveWorkOrder = async (order: WorkOrder, replan: boolean) => {
    setWorking(order.is_emergency ? '正在保存突发工单' : '正在保存工单')
    try {
      if (replan && order.is_emergency && !workEditor?.initial) {
        const planningTime = order.reported_at ?? replanTime
        const result = await api.replan(
          scenario.id,
          planningTime,
          'stable',
          order,
          commandKey('emergency-replan'),
        )
        if (activeScenarioId.current !== scenario.id) return
        const [fresh, planItems] = await Promise.all([api.scenario(scenario.id), api.planVersions(scenario.id)])
        if (activeScenarioId.current !== scenario.id) return
        setWorkEditor(undefined); setScenario(fresh); setSchedule(result); setPlans(planItems)
        setHistoricalScenario(undefined); setView('dispatch')
        setScenarios(current => current.map(item => item.id === fresh.id ? fresh : item))
        setToast('突发工单已接入，局部重排已发布')
        return
      }
      const next = workEditor?.initial
        ? await api.updateWorkOrder(scenario.id, order.id, order)
        : await api.createWorkOrder(scenario.id, order)
      if (activeScenarioId.current !== next.id) return
      setWorkEditor(undefined)
      if (replan) {
        const result = await api.replan(next.id, order.reported_at ?? 600, 'stable', undefined, commandKey('emergency-replan'))
        if (activeScenarioId.current !== next.id) return
        const [fresh, planItems] = await Promise.all([api.scenario(next.id), api.planVersions(next.id)])
        if (activeScenarioId.current !== next.id) return
        setScenario(fresh); setSchedule(result); setPlans(planItems); setHistoricalScenario(undefined); setView('dispatch')
        setScenarios(current => current.map(item => item.id === fresh.id ? fresh : item))
        setToast('突发工单已保存，局部重排完成')
      } else await applyScenarioEdit(next, '工单已保存')
    } catch (error) {
      if (replan && order.is_emergency && !workEditor?.initial) {
        try {
          const [fresh, planItems] = await Promise.all([api.scenario(scenario.id), api.planVersions(scenario.id)])
          if (activeScenarioId.current === scenario.id) {
            const active = planItems.find(item => item.active)
            setScenario(fresh); setPlans(planItems); setSchedule(active?.selected); setHistoricalScenario(undefined)
            setScenarios(current => current.map(item => item.id === fresh.id ? fresh : item))
          }
        } catch { /* keep the original replanning error */ }
      }
      setToast(error instanceof Error ? error.message : '工单保存失败')
    }
    finally { setWorking(undefined) }
  }

  const deleteWorkOrder = async (order: WorkOrder) => {
    if (!window.confirm(`确认删除待处理工单 ${order.id}？`)) return
    setWorking('正在删除工单')
    try { const next = await api.deleteWorkOrder(scenario.id, order.id); if (activeScenarioId.current !== next.id) return; setWorkEditor(undefined); await applyScenarioEdit(next, '工单已删除') }
    catch (error) { setToast(error instanceof Error ? error.message : '工单删除失败') }
    finally { setWorking(undefined) }
  }

  const saveTechnician = async (tech: Technician) => {
    setWorking('正在保存技师资料')
    try {
      const next = techEditor?.initial ? await api.updateTechnician(scenario.id, tech.id, tech) : await api.createTechnician(scenario.id, tech)
      if (activeScenarioId.current !== next.id) return
      setTechEditor(undefined); await applyScenarioEdit(next, '技师资料已保存')
    } catch (error) { setToast(error instanceof Error ? error.message : '技师保存失败') }
    finally { setWorking(undefined) }
  }

  const runOptimize = async () => {
    setWorking('正在生成推荐方案')
    try {
      const result = await api.optimize(scenario.id, strategy, undefined, commandKey('optimize'))
      if (activeScenarioId.current !== scenario.id) return
      const planItems = await api.planVersions(scenario.id)
      if (activeScenarioId.current !== scenario.id) return
      const active = planItems.find(item => item.active)
      const detail = active ? await api.planVersion(scenario.id, active.id) : undefined
      if (activeScenarioId.current !== scenario.id) return
      setPlans(planItems); setSchedule(active?.selected || result); setHistoricalScenario(undefined)
      setBaseline(detail?.artifacts.find(item => item.role === 'baseline')?.schedule)
      setToast('推荐方案已生成')
    } catch (error) { setToast(error instanceof Error ? error.message : '优化失败') }
    finally { setWorking(undefined) }
  }

  const runReplan = async () => {
    setWorking('正在保持已执行安排并局部重排')
    try {
      const result = await api.replan(scenario.id, replanTime, 'stable', undefined, commandKey('replan'))
      if (activeScenarioId.current !== scenario.id) return
      const [fresh, planItems] = await Promise.all([api.scenario(scenario.id), api.planVersions(scenario.id)])
      if (activeScenarioId.current !== scenario.id) return
      setScenario(fresh); setPlans(planItems); setSchedule(planItems.find(item => item.active)?.selected || result); setHistoricalScenario(undefined); setToast('局部重排完成')
    } catch (error) { setToast(error instanceof Error ? error.message : '局部重排失败') }
    finally { setWorking(undefined) }
  }

  const dispatch = <>
    <section className="command-bar">
      <div className="command-context"><GripVertical size={17} /><div><small>{partialCoverage ? '最后发布方案' : '当前方案'}</small><strong>{schedule ? `${kindLabel[schedule.kind]} · V${String(schedule.version).padStart(3, '0')}${partialCoverage ? ' · 新需求未覆盖' : ''}` : `业务数据已更新 · D${String(scenario.revision).padStart(3, '0')}`}</strong></div></div>
      <div className="command-actions">
        <button onClick={() => act('正在生成基线', () => api.baseline(scenario.id, commandKey('baseline')), '人工基线已生成')} disabled={!!working}><RefreshCw size={15} />生成基线</button>
        <div className="strategy-select"><select aria-label="优化策略" value={strategy} onChange={e => setStrategy(e.target.value as Strategy)}><option value="balanced">均衡策略</option><option value="completion">覆盖率优先</option><option value="punctuality">准时优先</option><option value="low_travel">低行程</option><option value="low_overtime">低加班</option><option value="fair_workload">工作量公平</option></select><ChevronDown size={13} /></div>
        <button className="primary" onClick={runOptimize} disabled={!!working}><WandSparkles size={16} />生成推荐方案</button>
        <button className="emergency" onClick={() => setWorkEditor({ emergencyPreset: true })} disabled={!!working}><Zap size={15} />新增突发单</button>
        <label className="replan-time">重排时点<span>{hhmm(replanTime)}</span><input aria-label="重排时点（当日起分钟数）" type="number" min="0" max="1800" step="5" value={replanTime} onChange={event => setReplanTime(Number(event.target.value))} /></label>
        <button onClick={runReplan} disabled={!!working || !schedule}><TimerReset size={15} />局部重排</button>
        <span className="divider" />
        <button onClick={async () => { const shownPlan = plans.find(item => item.selected.id === schedule?.id); setWorking('正在对比方案'); try { setComparison(await api.comparison(scenario.id, undefined, shownPlan?.id)) } catch (e) { setToast(e instanceof Error ? e.message : '无法比较') } finally { setWorking(undefined) } }} disabled={!!working || !schedule || !baseline || schedule.id === baseline.id}><BarChart3 size={15} />方案比较</button>
      </div>
      {working && <div className="working"><RefreshCw size={14} />{working}</div>}
    </section>
    {!schedule && <div className="stale-banner"><AlertTriangle size={16} /><div><strong>业务数据已修改，现有方案不再适用</strong><span>生成基线或推荐方案后再开始派单。</span></div></div>}
    {partialCoverage && schedule && <div className="stale-banner partial"><AlertTriangle size={16} /><div><strong>突发工单已保存，最后发布方案尚未覆盖全部需求</strong><span>V{String(schedule.version).padStart(3, '0')} 仍保留原承诺；请处理未计划工单后重新重排。</span></div></div>}
    {executionProgress && <div className="stale-banner partial"><Clock3 size={16} /><div><strong>现场执行状态已更新</strong><span>V{String(schedule.version).padStart(3, '0')} 仍是当前执行依据；局部重排会保留服务中的安排，并排除已完成工单。</span></div></div>}
    {!partialCoverage && !executionProgress && schedule && schedule.scenario_revision !== scenario.revision && <div className="stale-banner historical"><CalendarDays size={16} /><div><strong>正在查看历史方案 V{String(schedule.version).padStart(3, '0')}</strong><span>该方案使用数据 D{String(schedule.scenario_revision).padStart(3, '0')}，当前数据为 D{String(scenario.revision).padStart(3, '0')}，仅供回看。</span></div></div>}
    <KpiStrip schedule={schedule} baseline={baseline} />
    <div className="workspace"><OrderQueue scenario={shownScenario} schedule={schedule} selectedId={selectedId} onSelect={setSelectedId} onAdd={() => setWorkEditor({})} /><RouteMap scenario={shownScenario} schedule={schedule} selectedId={selectedId} onSelect={setSelectedId} /><Timeline scenario={shownScenario} schedule={schedule} onSelect={setSelectedId} currentTime={replanTime} /></div>
    <footer className="statusbar"><span><span className="pulse" />行程估算已更新</span><span>业务数据 D{String(shownScenario.revision).padStart(3, '0')}</span><span>{shownScenario.work_orders.length} 工单 / {shownScenario.technicians.length} 技师 / {skillCount} 类技能</span><span className="objective">业务评分 <b>{typeof schedule?.business_score === 'number' ? schedule.business_score.toLocaleString() : '—'}</b></span></footer>
  </>

  return <div className="app-shell">
    <Sidebar scenarios={scenarios} current={scenario} active={view} busy={!!working || !!loadingScenarioId} onSelect={loadScenario} onNavigate={nextView => { setView(nextView); if (nextView === 'lab' && !scenario.id.startsWith('strategy-')) void loadScenario('strategy-medium') }} />
    <main className="main-content">
      <header className="topbar"><div><div className="date-line"><span>{dateText}</span><i />服务时段 {serviceWindow}</div><h1>{shownScenario.name}</h1></div><div className="top-actions"><SolverBadge schedule={schedule} /><button className="ghost-btn" disabled={!schedule} onClick={() => schedule && window.open(`/api/scenarios/${scenario.id}/report?schedule_id=${schedule.id}`, '_blank')}><FileText size={16} />导出报告</button></div></header>
      {view === 'dispatch' && dispatch}
      {view === 'versions' && <VersionsView scenario={scenario} plans={plans} onOpen={async item => { setWorking('正在读取版本快照'); try { const detail = await api.planVersion(scenario.id, item.id); setSchedule(detail.selected); setHistoricalScenario(detail.active && detail.data_revision === scenario.revision ? undefined : detail.scenario_snapshot || undefined); setBaseline(detail.artifacts.find(artifactItem => artifactItem.role === 'baseline')?.schedule); setView('dispatch'); setToast(`已打开历史方案 V${String(item.number).padStart(3, '0')}`) } catch (error) { setToast(error instanceof Error ? error.message : '版本读取失败') } finally { setWorking(undefined) } }} onActivate={async item => {
        setWorking('正在核对并激活历史计划')
        try {
          const activated = await api.activatePlanVersion(scenario.id, item.id, scenario.revision, commandKey('activate'))
          if (activeScenarioId.current !== scenario.id) return
          const planItems = await api.planVersions(scenario.id)
          if (activeScenarioId.current !== scenario.id) return
          setPlans(planItems); setSchedule(activated.selected); setHistoricalScenario(undefined); setView('dispatch'); setToast(`已激活为 V${String(activated.number).padStart(3, '0')}`)
        } catch (error) { setToast(error instanceof Error ? error.message : '历史计划无法激活') }
        finally { setWorking(undefined) }
      }} onClone={async item => {
        const name = window.prompt('新场景名称', `${scenario.name} · V${String(item.number).padStart(3, '0')} 副本`)?.trim()
        if (!name) return
        setWorking('正在从历史快照克隆场景')
        try {
          const cloned = await api.clonePlanScenario(scenario.id, item.id, name, commandKey('clone'))
          setScenarios(current => current.some(existing => existing.id === cloned.id) ? current : [...current, cloned])
          await loadScenario(cloned.id); setView('dispatch'); setToast('历史快照已克隆为独立场景')
        } catch (error) { setToast(error instanceof Error ? error.message : '场景克隆失败') }
        finally { setWorking(undefined) }
      }} onRestore={async item => {
        setWorking('正在核对业务回滚差异')
        try {
          const preview = await api.rollbackPreview(scenario.id, item.id)
          if (preview.completed_work_orders_reopened.length) throw new Error(`默认禁止重新打开已完成工单：${preview.completed_work_orders_reopened.join('、')}`)
          if (preview.started_work_orders_reopened.length) throw new Error(`禁止重新打开服务中工单：${preview.started_work_orders_reopened.join('、')}`)
          if (preview.executed_work_orders_deleted.length) throw new Error(`禁止删除已有执行记录的工单：${preview.executed_work_orders_deleted.join('、')}`)
          if (preview.removed_work_orders.length) throw new Error(`默认禁止删除历史版本之后新增的工单：${preview.removed_work_orders.join('、')}`)
          const reason = window.prompt('请填写业务回滚原因')?.trim()
          if (!reason) return
          const currentPlan = preview.current_plan_number == null ? '当前没有可执行方案' : `当前 V${String(preview.current_plan_number).padStart(3, '0')}`
          const message = `将业务数据回滚到 V${String(item.number).padStart(3, '0')} 的快照。\n\n工单：新增 ${preview.added_work_orders.length} 个、删除 ${preview.removed_work_orders.length} 个、修改 ${preview.modified_work_orders.length} 个；技师变化 ${preview.technician_changes.length} 项；锁定变化 ${preview.lock_changes.length} 项；受影响执行事件 ${preview.affected_execution_event_ids.length} 条。\n${currentPlan} 切换后将有 ${preview.changed_plan_work_orders.length} 个工单的安排发生变化。\n\n这不是普通的计划切换。操作会创建新的 D 和 V，现有历史不会删除。`
          if (!window.confirm(message)) return
          const restored = await api.rollbackPlanVersion(scenario.id, item.id, scenario.revision, preview.confirmation_token, reason, commandKey('rollback'))
          if (activeScenarioId.current !== scenario.id) return
          const [fresh, planItems] = await Promise.all([api.scenario(scenario.id), api.planVersions(scenario.id)])
          if (activeScenarioId.current !== scenario.id) return
          setScenario(fresh); setPlans(planItems); setSchedule(restored.selected); setHistoricalScenario(undefined); setBaseline(undefined); setView('dispatch'); setToast(`业务数据已回滚，生成 V${String(restored.number).padStart(3, '0')}`)
        } catch (error) { setToast(error instanceof Error ? error.message : '业务回滚失败') }
        finally { setWorking(undefined) }
      }} onCompare={async (before, after) => { setWorking('正在比较指定版本'); try { setComparison(await api.comparison(scenario.id, before.id, after.id)) } catch (error) { setToast(error instanceof Error ? error.message : '版本比较失败') } finally { setWorking(undefined) } }} onRename={async (item, label) => { try { const updated = await api.renamePlanVersion(scenario.id, item.id, label); setPlans(current => current.map(plan => plan.id === updated.id ? updated : plan)); setToast('方案名称已更新') } catch (error) { setToast(error instanceof Error ? error.message : '重命名失败') } }} onReset={async () => {
        if (!window.confirm('恢复初始业务数据？已有方案历史会保留。')) return
        setWorking('正在恢复初始数据')
        try { const next = await api.resetScenario(scenario.id); invalidatePlan(next, '业务数据已恢复') }
        catch (error) { setToast(error instanceof Error ? error.message : '恢复失败') }
        finally { setWorking(undefined) }
      }} />}
      {view === 'technicians' && <TechniciansView scenario={scenario} schedule={schedule} onEdit={initial => setTechEditor({ initial })} onAdd={() => setTechEditor({})} />}
      {view === 'lab' && <StrategyLab key={scenario.id} scenario={scenario} profiles={profiles} loadingDataset={!!loadingScenarioId} onSelectDataset={id => { void loadScenario(id); setView('lab') }} onReloadProfiles={async () => setProfiles(await api.strategyProfiles())} onPublished={async plan => { if (activeScenarioId.current !== plan.scenario_id) return; const [fresh, planItems] = await Promise.all([api.scenario(plan.scenario_id), api.planVersions(plan.scenario_id)]); if (activeScenarioId.current !== plan.scenario_id) return; setScenario(fresh); setPlans(planItems); setSchedule(plan.selected); setHistoricalScenario(undefined); setBaseline(undefined); setView('dispatch') }} onToast={setToast} />}
      {view === 'review' && <ReviewView scenarioId={scenario.id} planVersionId={(plans.find(item => item.selected.id === schedule?.id) || plans.find(item => item.active))?.id} schedule={schedule} baseline={baseline} />}
    </main>
    {selected && <ExplanationPanel scenario={shownScenario} schedule={schedule} orderId={selected.id} readOnly={!!historicalScenario} onClose={() => setSelectedId(undefined)} onEdit={initial => setWorkEditor({ initial })} onExecute={(assignment, action) => setExecutionEditor({ assignment, action })} onAssign={async (orderId, technicianId) => {
      setWorking('正在改派并局部重排')
      try {
        const result = await api.manualReassignment(scenario.id, orderId, technicianId, replanTime, scenario.revision, commandKey('manual-reassignment'))
        if (activeScenarioId.current !== scenario.id) return
        const planItems = await api.planVersions(scenario.id)
        if (activeScenarioId.current !== scenario.id) return
        setScenario(result.scenario); setPlans(planItems); setSchedule(result.schedule || planItems.find(item => item.active)?.selected); setHistoricalScenario(undefined)
        setToast(result.replan_status === 'COMPLETED' ? '工单已改派并锁定' : '人工锁定已保存，但局部重排失败；最后发布方案仍保留，请检查后重试')
      } catch (error) {
        try {
          const [fresh, planItems] = await Promise.all([api.scenario(scenario.id), api.planVersions(scenario.id)])
          if (activeScenarioId.current === scenario.id) { setScenario(fresh); setPlans(planItems); setSchedule(planItems.find(item => item.active)?.selected) }
        } catch { /* Keep the original actionable error when refresh also fails. */ }
        setToast(error instanceof Error ? error.message : '改派失败')
      }
      finally { setWorking(undefined) }
    }} onLock={async (assignment, locked) => {
      setWorking(locked ? '正在锁定安排' : '正在解除锁定')
      try {
        const next = await api.lock(scenario.id, assignment.work_order_id, assignment.technician_id, locked)
        if (activeScenarioId.current !== next.id) return
        const planItems = await api.planVersions(next.id)
        if (activeScenarioId.current !== next.id) return
        if (next.work_orders.some(order => order.status === 'started')) {
          const active = planItems.find(item => item.active)
          setScenario(next); setPlans(planItems); setSchedule(active?.selected); setHistoricalScenario(undefined)
          setToast(`${locked ? '人工锁定已生效' : '已解除人工锁定'}；当前执行方案已保留，请局部重排`)
        } else {
          setScenario(next); setPlans(planItems); setSchedule(undefined); setBaseline(undefined); setHistoricalScenario(undefined)
          setToast(`${locked ? '人工锁定已生效' : '已解除人工锁定'}，请重新生成或局部重排方案`)
        }
      } catch (e) { setToast(e instanceof Error ? e.message : '锁定失败') } finally { setWorking(undefined) }
    }} />}
    {comparison && <CompareDrawer data={comparison} onClose={() => setComparison(undefined)} />}
    {executionEditor && <ExecutionDialog scenario={scenario} assignment={executionEditor.assignment} action={executionEditor.action} onClose={() => setExecutionEditor(undefined)} onSubmit={async (occurredAt, earlyStartOverrideReason, estimatedRemainingMinutes, note) => {
      const { assignment, action } = executionEditor
      setWorking(action === 'start' ? '正在登记开始服务' : '正在登记完成服务')
      try {
        const result = await api.executeWorkOrder(scenario.id, assignment.work_order_id, action, assignment.technician_id, occurredAt, scenario.revision, commandKey(`execution-${action}`), { earlyStartOverrideReason, estimatedRemainingMinutes, note })
        if (activeScenarioId.current !== scenario.id) return
        const planItems = await api.planVersions(scenario.id)
        if (activeScenarioId.current !== scenario.id) return
        setExecutionEditor(undefined); setScenario(result.scenario); setPlans(planItems); setScenarios(current => current.map(item => item.id === result.scenario.id ? result.scenario : item)); setToast(action === 'start' ? '已登记实际开始时间；后续重排会保留该安排' : `已登记完成服务${result.event.actual_duration_minutes == null ? '' : `，实际用时 ${result.event.actual_duration_minutes} 分钟`}`)
      } catch (error) { setToast(error instanceof Error ? error.message : '执行状态登记失败') }
      finally { setWorking(undefined) }
    }} />}
    {workEditor && <WorkOrderEditor initial={workEditor.initial} emergencyPreset={workEditor.emergencyPreset} onClose={() => setWorkEditor(undefined)} onSave={saveWorkOrder} onDelete={deleteWorkOrder} />}
    {techEditor && <TechnicianEditor initial={techEditor.initial} onClose={() => setTechEditor(undefined)} onSave={saveTechnician} />}
    {toast && <div className="toast" role="status"><span className="toast-mark" aria-hidden="true" />{toast}<button onClick={() => setToast(undefined)} aria-label="关闭提示"><X size={14} /></button></div>}
  </div>
}
