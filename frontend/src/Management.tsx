import { Component, type ErrorInfo, type ReactNode, useMemo, useState } from 'react'
import {
  AlertTriangle, ArrowLeft, BarChart3, Check, Clock3, Edit3, Plus, Save,
  ShieldCheck, Trash2, UserRound, X, Zap,
} from 'lucide-react'
import type { PlanVersion, Scenario, Schedule, Technician, WorkOrder } from './types'

const skillLabel: Record<string, string> = { electrical: '电气', hvac: '暖通', network: '网络' }
const kindLabel = { baseline: '人工基线', optimized: '优化方案', replan: '局部重排' }
const strategyLabel: Record<string, string> = { baseline: '基线', balanced: '均衡', completion: '完成率优先', punctuality: '准时优先', low_travel: '低行程', low_overtime: '低加班', fair_workload: '工作量公平', stable: '稳定优先', custom: '自定义' }
const hhmm = (minutes: number) => `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
const fromTime = (value: string) => { const [h, m] = value.split(':').map(Number); return h * 60 + m }
const pct = (value: number) => `${Math.round(value * 100)}%`

export class AppErrorBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {}
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('FieldFlow render error', error, info) }
  render() {
    if (!this.state.error) return this.props.children
    return <main className="fatal-state"><AlertTriangle size={28} /><h1>这页的数据不完整</h1><p>请重新读取场景。如果问题仍在，请展开错误详情。</p><button onClick={() => window.location.reload()}>重新读取</button><details><summary>错误详情</summary>{this.state.error.message}</details></main>
  }
}

export function VersionsView({ scenario, plans, onOpen, onRestore, onCompare, onRename, onReset }: { scenario: Scenario; plans: PlanVersion[]; onOpen: (plan: PlanVersion) => void; onRestore: (plan: PlanVersion) => void; onCompare: (before: PlanVersion, after: PlanVersion) => void; onRename: (plan: PlanVersion, label: string) => void; onReset: () => void }) {
  const ordered = [...plans].reverse()
  const [filter, setFilter] = useState('all')
  const [beforeId, setBeforeId] = useState('')
  const [afterId, setAfterId] = useState('')
  const visible = filter === 'all' ? ordered : ordered.filter(item => item.action === filter)
  const sourceById = new Map(plans.map(item => [item.id, item]))
  return <section className="page-view">
    <div className="page-title"><div><span className="eyebrow">PLAN HISTORY</span><h1>方案版本</h1><p>V 是方案版本，D 是该方案使用的数据修订号。</p></div><div className="page-title-actions"><span className="revision-badge">数据 D{String(scenario.revision).padStart(3, '0')}</span><button onClick={onReset}>恢复初始数据</button></div></div>
    <div className="version-tools">
      <label>筛选<select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部动作</option><option value="baseline">人工基线</option><option value="optimize">优化</option><option value="replan">局部重排</option><option value="restore">历史恢复</option><option value="experiment_publish">实验发布</option></select></label>
      <div className="version-compare"><span>跨版本比较</span><select aria-label="比较起点" value={beforeId} onChange={event => setBeforeId(event.target.value)}><option value="">起点版本</option>{plans.map(item => <option key={item.id} value={item.id}>V{String(item.number).padStart(3, '0')} · {item.label}</option>)}</select><span>→</span><select aria-label="比较终点" value={afterId} onChange={event => setAfterId(event.target.value)}><option value="">终点版本</option>{plans.map(item => <option key={item.id} value={item.id}>V{String(item.number).padStart(3, '0')} · {item.label}</option>)}</select><button disabled={!beforeId || !afterId || beforeId === afterId} onClick={() => { const before = plans.find(item => item.id === beforeId); const after = plans.find(item => item.id === afterId); if (before && after) onCompare(before, after) }}><BarChart3 size={15} />比较</button></div>
    </div>
    <div className="version-list">
      {visible.map(item => {
        const schedule = item.selected
        const source = item.source_version_id ? sourceById.get(item.source_version_id) : undefined
        return <article key={item.id} className={`version-row ${item.active ? 'current' : ''}`}>
          <button className="version-open" onClick={() => onOpen(item)} aria-label={`打开 V${item.number}`}><div className="version-mark"><b>V{String(item.number).padStart(3, '0')}</b><span>{item.active ? '当前执行方案' : kindLabel[schedule.kind]}</span></div></button>
          <div className="version-name"><strong>{item.label}</strong><small>{strategyLabel[schedule.strategy] || schedule.strategy} · D{String(item.data_revision).padStart(3, '0')} · {new Date(item.created_at).toLocaleString('zh-CN')}</small>{source && <small>来源 V{String(source.number).padStart(3, '0')} · {source.label}</small>}</div>
          <div className="version-kpi"><span>完成率 <b>{pct(schedule.kpis.completion_rate)}</b></span><span>SLA <b>{pct(schedule.kpis.sla_on_time_rate)}</b></span><span>行程 <b>{schedule.kpis.total_travel_minutes}′</b></span><span>未分配 <b>{schedule.kpis.unassigned_count}</b></span></div>
          <div className="version-state"><span className={`solver-dot ${schedule.solver_status.toLowerCase()}`} />{schedule.solver_status}<small>计算 {schedule.runtime_ms} ms</small>{item.data_revision !== scenario.revision && <em>历史数据 D{String(item.data_revision).padStart(3, '0')}</em>}</div>
          <div className="version-actions"><button onClick={() => { const label = window.prompt('方案名称', item.label); if (label?.trim() && label.trim() !== item.label) onRename(item, label.trim()) }}>重命名</button><a href={`/api/scenarios/${scenario.id}/plan-versions/${item.id}/report`} target="_blank" rel="noreferrer">报告</a><button className="restore" disabled={item.active && item.data_revision === scenario.revision} onClick={() => onRestore(item)}>恢复为新版本</button></div>
        </article>
      })}
      {!visible.length && <div className="empty-view">当前筛选下没有方案。生成基线、优化或发布实验候选后会从 V001 开始记录。</div>}
    </div>
  </section>
}

export function TechniciansView({ scenario, schedule, onEdit, onAdd }: { scenario: Scenario; schedule?: Schedule; onEdit: (tech: Technician) => void; onAdd: () => void }) {
  const coverage = Object.keys(skillLabel).map(skill => ({ skill, count: scenario.technicians.filter(t => t.skills.includes(skill)).length }))
  return <section className="page-view">
    <div className="page-title"><div><span className="eyebrow">TEAM CAPACITY</span><h1>技师与技能</h1><p>维护技能、班次、加班上限和出发点。保存后需重新生成方案。</p></div><button className="page-primary" onClick={onAdd}><Plus size={15} />新增技师</button></div>
    <div className="coverage-strip">{coverage.map(item => <div key={item.skill}><span>{skillLabel[item.skill]}覆盖</span><b>{item.count}</b><small>{item.count < 2 ? '只有一人可接' : '至少两人可接'}</small></div>)}</div>
    <div className="tech-card-grid">{scenario.technicians.map(tech => {
      const kpi = schedule?.kpis.technician.find(item => item.technician_id === tech.id)
      return <article className="tech-card" key={tech.id}>
        <div className="tech-card-head"><div className="tech-avatar large" style={{ '--tech': tech.color } as React.CSSProperties}>{tech.name.slice(-1)}</div><div><h2>{tech.name}</h2><span>{tech.id}</span></div><button className="icon-btn" onClick={() => onEdit(tech)} aria-label={`编辑${tech.name}`}><Edit3 size={15} /></button></div>
        <div className="skill-row">{tech.skills.map(skill => <span key={skill}>{skillLabel[skill]}</span>)}</div>
        <dl><div><dt>班次</dt><dd>{hhmm(tech.shift_start)}–{hhmm(tech.shift_end)}</dd></div><div><dt>加班上限</dt><dd>{tech.overtime_limit} 分钟</dd></div><div><dt>今日任务</dt><dd>{kpi?.assignment_count ?? '—'} 单</dd></div><div><dt>服务利用率</dt><dd>{kpi ? pct(kpi.utilization) : '—'}</dd></div></dl>
      </article>
    })}</div>
  </section>
}

export function ReviewView({ schedule, baseline }: { schedule?: Schedule; baseline?: Schedule }) {
  if (!schedule) return <section className="page-view"><div className="empty-view">请先生成一个方案，再查看运营复盘。</div></section>
  const breakdownLabels: Record<string, string> = { travel: '行程代价', sla_late: 'SLA 延迟代价', overtime: '加班代价', unassigned: '未分配代价', imbalance: '负载不均代价', replan_changes: '方案变更代价' }
  const maxCost = Math.max(1, ...Object.values(schedule.objective_breakdown))
  const delta = baseline && baseline.id !== schedule.id ? {
    travel: schedule.kpis.total_travel_minutes - baseline.kpis.total_travel_minutes,
    late: schedule.kpis.sla_late_count - baseline.kpis.sla_late_count,
    overtime: schedule.kpis.total_overtime_minutes - baseline.kpis.total_overtime_minutes,
    dropped: schedule.kpis.unassigned_count - baseline.kpis.unassigned_count,
  } : undefined
  const tradeoffs = delta ? [
    delta.dropped < 0 ? `多完成 ${-delta.dropped} 单` : delta.dropped > 0 ? `少完成 ${delta.dropped} 单` : '完成数量不变',
    delta.late > 0 ? `SLA 超时增加 ${delta.late} 单` : delta.late < 0 ? `SLA 超时减少 ${-delta.late} 单` : 'SLA 超时不变',
    delta.overtime > 0 ? `加班增加 ${delta.overtime} 分钟` : delta.overtime < 0 ? `加班减少 ${-delta.overtime} 分钟` : '加班不变',
    delta.travel > 0 ? `行程增加 ${delta.travel} 分钟` : delta.travel < 0 ? `行程减少 ${-delta.travel} 分钟` : '行程不变',
  ] : []
  return <section className="page-view">
    <div className="page-title"><div><span className="eyebrow">OPERATIONS REVIEW</span><h1>运营复盘</h1><p>查看计算耗时、现场耗时，以及各项指标相对基线的变化。</p></div><span className="revision-badge">{strategyLabel[schedule.strategy]}策略</span></div>
    <div className="review-summary"><div><small>方案计算用时</small><b>{schedule.runtime_ms}<em> ms</em></b><p>生成本方案所用的时间。</p></div><div><small>现场行程时间</small><b>{schedule.kpis.total_travel_minutes}<em> 分钟</em></b><p>各段路程和返程的预计总时长。</p></div><div><small>现场服务时间</small><b>{schedule.kpis.total_service_minutes}<em> 分钟</em></b><p>已排工单的服务时长合计。</p></div></div>
    {tradeoffs.length > 0 && <div className="tradeoff-card"><ShieldCheck size={20} /><div><h2>与基线相比</h2><p>{tradeoffs.join('；')}。方案排序依据为当前策略权重。</p></div></div>}
    <div className="review-grid"><article><h2>目标值构成</h2><div className="cost-bars">{Object.entries(schedule.objective_breakdown).map(([key, value]) => <div key={key}><span>{breakdownLabels[key] || key}</span><i><b style={{ width: `${value / maxCost * 100}%` }} /></i><strong>{Math.round(value)}</strong></div>)}</div></article><article><h2>技师工作量</h2>{schedule.kpis.technician.map(item => <div className="util-row" key={item.technician_id}><span>{item.technician_id}</span><i><b style={{ width: `${Math.min(100, item.utilization * 100)}%` }} /></i><strong>{pct(item.utilization)}</strong><small>{item.assignment_count} 单</small></div>)}</article></div>
  </section>
}

const blankOrder = (emergency = false): WorkOrder => ({
  id: `WO-${emergency ? 'EMG' : 'NEW'}-${String(Date.now()).slice(-6)}`,
  customer_name: '', title: '', required_skills: ['electrical'], location: { x: 50, y: 50 },
  service_duration: 45, window_start: emergency ? 600 : 540, window_end: emergency ? 720 : 660,
  sla_deadline: emergency ? 660 : 630, priority: emergency ? 'urgent' : 'normal',
  drop_penalty: emergency ? 10000 : 2500, status: 'pending', vip: emergency,
  is_emergency: emergency, reported_at: emergency ? 600 : null, note: '',
})

export function WorkOrderEditor({ initial, emergencyPreset, onClose, onSave, onDelete }: { initial?: WorkOrder; emergencyPreset?: boolean; onClose: () => void; onSave: (order: WorkOrder, replan: boolean) => Promise<void>; onDelete?: (order: WorkOrder) => Promise<void> }) {
  const [order, setOrder] = useState<WorkOrder>(() => initial ? structuredClone(initial) : blankOrder(emergencyPreset))
  const [saving, setSaving] = useState(false)
  const valid = order.customer_name.trim() && order.title.trim() && order.required_skills.length && order.window_end >= order.window_start && order.sla_deadline >= order.window_start
  const patch = <K extends keyof WorkOrder>(key: K, value: WorkOrder[K]) => setOrder(current => ({ ...current, [key]: value }))
  const submit = async (replan: boolean) => { if (!valid) return; setSaving(true); try { await onSave(order, replan) } finally { setSaving(false) } }
  return <div className="modal-backdrop"><section className="editor-modal">
    <div className="editor-head"><div><span className="eyebrow">WORK ORDER</span><h2>{initial ? `编辑 ${initial.id}` : order.is_emergency ? '登记突发工单' : '新增工单'}</h2></div><button className="icon-btn" onClick={onClose}><X /></button></div>
    <div className="form-grid"><label className="span-2">客户名称<input value={order.customer_name} onChange={e => patch('customer_name', e.target.value)} placeholder="例如：衡安数据中心" /></label><label className="span-2">任务内容<input value={order.title} onChange={e => patch('title', e.target.value)} placeholder="例如：核心机房断电告警" /></label>
      <fieldset className="span-2"><legend>所需技能</legend>{Object.entries(skillLabel).map(([key, label]) => <label className="check" key={key}><input type="checkbox" checked={order.required_skills.includes(key)} onChange={e => patch('required_skills', e.target.checked ? [...order.required_skills, key] : order.required_skills.filter(item => item !== key))} />{label}</label>)}</fieldset>
      <label>时间窗开始<input type="time" value={hhmm(order.window_start)} onChange={e => patch('window_start', fromTime(e.target.value))} /></label><label>时间窗结束<input type="time" value={hhmm(order.window_end)} onChange={e => patch('window_end', fromTime(e.target.value))} /></label><label>SLA 截止<input type="time" value={hhmm(order.sla_deadline)} onChange={e => patch('sla_deadline', fromTime(e.target.value))} /></label><label>服务时长（分钟）<input type="number" min="5" max="480" value={order.service_duration} onChange={e => patch('service_duration', Number(e.target.value))} /></label>
      <label>横坐标<input type="number" min="0" max="100" value={order.location.x} onChange={e => patch('location', { ...order.location, x: Number(e.target.value) })} /></label><label>纵坐标<input type="number" min="0" max="100" value={order.location.y} onChange={e => patch('location', { ...order.location, y: Number(e.target.value) })} /></label>
      <label>优先级<select value={order.priority} onChange={e => patch('priority', e.target.value as WorkOrder['priority'])}><option value="urgent">紧急</option><option value="high">高</option><option value="normal">普通</option><option value="low">低</option></select></label><label>状态<select value={order.status} onChange={e => patch('status', e.target.value as WorkOrder['status'])}><option value="pending">待处理</option><option value="started">已开始</option><option value="completed">已完成</option></select></label>
      <label className="switch-line span-2"><input type="checkbox" checked={order.is_emergency} onChange={e => { if (e.target.checked) { setOrder(current => ({ ...current, is_emergency: true, priority: 'urgent', drop_penalty: Math.max(8000, current.drop_penalty), reported_at: current.reported_at ?? 600 })) } else { setOrder(current => ({ ...current, is_emergency: false, drop_penalty: current.priority === 'high' ? 4800 : current.priority === 'low' ? 1400 : 2500, reported_at: null })) } }} /><Zap size={15} />标记为突发工单<span>突发单会在局部重排中获得更高的保留优先级</span></label>
      {order.is_emergency && <label>接报时间<input type="time" value={hhmm(order.reported_at ?? 600)} onChange={e => patch('reported_at', fromTime(e.target.value))} /></label>}
      <label className="span-2">现场备注<textarea value={order.note} onChange={e => patch('note', e.target.value)} rows={3} /></label>
    </div>
    {!valid && <p className="form-error">请填写客户、任务和至少一项技能，并检查时间设置。</p>}
    <div className="editor-actions">{initial && onDelete && initial.status === 'pending' && <button className="delete-btn" disabled={saving} onClick={() => onDelete(order)}><Trash2 size={15} />删除工单</button>}<button onClick={onClose}>取消</button><button className="page-primary" disabled={!valid || saving} onClick={() => submit(false)}><Save size={15} />保存</button>{order.is_emergency && <button className="emergency-save" disabled={!valid || saving} onClick={() => submit(true)}><Zap size={15} />保存并局部重排</button>}</div>
  </section></div>
}

const blankTechnician = (): Technician => ({ id: `TECH-${String(Date.now()).slice(-3)}`, name: '', skills: ['electrical'], shift_start: 480, shift_end: 1020, start_location: { x: 48, y: 52 }, overtime_limit: 60, cost_per_minute: 1, color: '#315c4b' })

export function TechnicianEditor({ initial, onClose, onSave }: { initial?: Technician; onClose: () => void; onSave: (tech: Technician) => Promise<void> }) {
  const [tech, setTech] = useState<Technician>(() => initial ? structuredClone(initial) : blankTechnician())
  const [saving, setSaving] = useState(false)
  const patch = <K extends keyof Technician>(key: K, value: Technician[K]) => setTech(current => ({ ...current, [key]: value }))
  const valid = tech.name.trim() && tech.skills.length && tech.shift_end > tech.shift_start
  return <div className="modal-backdrop"><section className="editor-modal compact"><div className="editor-head"><div><span className="eyebrow">TECHNICIAN</span><h2>{initial ? `编辑 ${initial.name}` : '新增技师'}</h2></div><button className="icon-btn" onClick={onClose}><X /></button></div>
    <div className="form-grid"><label>技师编号<input value={tech.id} disabled={!!initial} onChange={e => patch('id', e.target.value)} /></label><label>姓名<input value={tech.name} onChange={e => patch('name', e.target.value)} /></label><fieldset className="span-2"><legend>技能</legend>{Object.entries(skillLabel).map(([key, label]) => <label className="check" key={key}><input type="checkbox" checked={tech.skills.includes(key)} onChange={e => patch('skills', e.target.checked ? [...tech.skills, key] : tech.skills.filter(item => item !== key))} />{label}</label>)}</fieldset><label>班次开始<input type="time" value={hhmm(tech.shift_start)} onChange={e => patch('shift_start', fromTime(e.target.value))} /></label><label>班次结束<input type="time" value={hhmm(tech.shift_end)} onChange={e => patch('shift_end', fromTime(e.target.value))} /></label><label>加班上限（分钟）<input type="number" min="0" max="240" value={tech.overtime_limit} onChange={e => patch('overtime_limit', Number(e.target.value))} /></label><label>每分钟成本<input type="number" min="0.1" step="0.1" value={tech.cost_per_minute} onChange={e => patch('cost_per_minute', Number(e.target.value))} /></label></div>
    <div className="editor-actions"><button onClick={onClose}>取消</button><button className="page-primary" disabled={!valid || saving} onClick={async () => { setSaving(true); try { await onSave(tech) } finally { setSaving(false) } }}><Save size={15} />保存技师</button></div>
  </section></div>
}
