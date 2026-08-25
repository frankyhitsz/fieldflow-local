import { Component, type ErrorInfo, type ReactNode, useEffect, useState } from 'react'
import {
  AlertTriangle, BarChart3, Edit3, Plus, Save, ShieldCheck, Trash2, X, Zap,
} from 'lucide-react'
import { api } from './api'
import type { CapacityAnalysis, CapacityCounterfactualArtifact, CostAnalysis, DecisionAnalysisRun, PlanVersion, RiskSimulation, Scenario, Schedule, SimulationScenarioSetArtifact, Technician, WorkOrder } from './types'

const skillLabel: Record<string, string> = { electrical: '电气', hvac: '暖通', network: '网络' }
const kindLabel = { baseline: '人工基线', optimized: '优化方案', replan: '局部重排' }
const strategyLabel: Record<string, string> = { baseline: '基线', balanced: '均衡', completion: '覆盖率优先', punctuality: '准时优先', low_travel: '低行程', low_overtime: '低加班', fair_workload: '工作量公平', stable: '稳定优先', custom: '自定义' }
const clockValue = (minutes: number) => {
  const clock = ((minutes % 1440) + 1440) % 1440
  return `${String(Math.floor(clock / 60)).padStart(2, '0')}:${String(clock % 60).padStart(2, '0')}`
}
const hhmm = (minutes: number) => `${minutes >= 1440 ? '次日 ' : ''}${clockValue(minutes)}`
const fromClock = (value: string, day: number) => {
  const [h, m] = value.split(':').map(Number)
  return day * 1440 + h * 60 + m
}
const pct = (value: number) => `${Math.round(value * 100)}%`
const money = (cents: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(cents / 100)

function ServiceTimeInput({ value, onChange, allowNextDay = true, disabled = false, label }: { value: number; onChange: (value: number) => void; allowNextDay?: boolean; disabled?: boolean; label: string }) {
  const day = value >= 1440 ? 1 : 0
  return <span className="service-time-input">
    {allowNextDay && <select disabled={disabled} aria-label={`${label}日期`} value={day} onChange={event => onChange(fromClock(clockValue(value), Number(event.target.value)))}><option value={0}>当日</option><option value={1}>次日</option></select>}
    <input disabled={disabled} aria-label={label} type="time" value={clockValue(value)} onChange={event => onChange(fromClock(event.target.value, day))} />
  </span>
}

export class AppErrorBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {}
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('FieldFlow render error', error, info) }
  render() {
    if (!this.state.error) return this.props.children
    return <main className="fatal-state"><AlertTriangle size={28} /><h1>这页的数据不完整</h1><p>请重新读取场景。如果问题仍在，请展开错误详情。</p><button onClick={() => window.location.reload()}>重新读取</button><details><summary>错误详情</summary>{this.state.error.message}</details></main>
  }
}

export function VersionsView({ scenario, plans, onOpen, onActivate, onReattest, onClone, onRestore, onCompare, onRename, onReset }: { scenario: Scenario; plans: PlanVersion[]; onOpen: (plan: PlanVersion) => void; onActivate: (plan: PlanVersion) => void; onReattest: (plan: PlanVersion) => void; onClone: (plan: PlanVersion) => void; onRestore: (plan: PlanVersion) => void; onCompare: (before: PlanVersion, after: PlanVersion) => void; onRename: (plan: PlanVersion, label: string) => void; onReset: () => void }) {
  const ordered = [...plans].reverse()
  const [actionFilter, setActionFilter] = useState('all')
  const [strategyFilter, setStrategyFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [beforeId, setBeforeId] = useState('')
  const [afterId, setAfterId] = useState('')
  const strategies = [...new Set(plans.map(item => item.selected.strategy))]
  const statuses = [...new Set(plans.map(item => item.selected.solver_status))]
  const visible = ordered.filter(item =>
    (actionFilter === 'all' || item.action === actionFilter)
    && (strategyFilter === 'all' || item.selected.strategy === strategyFilter)
    && (statusFilter === 'all' || item.selected.solver_status === statusFilter)
  )
  const sourceById = new Map(plans.map(item => [item.id, item]))
  const trustedPlans = plans.filter(item => item.effective_integrity === 'VERIFIED')
  return <section className="page-view">
    <div className="page-title"><div><span className="eyebrow">PLAN HISTORY</span><h1>方案版本</h1><p>V 是方案版本，D 是该方案使用的数据修订号。</p></div><div className="page-title-actions"><span className="revision-badge">数据 D{String(scenario.revision).padStart(3, '0')}</span><button onClick={onReset}>恢复初始数据</button></div></div>
    <div className="version-tools">
      <div className="version-filters"><label>动作<select aria-label="按动作筛选" value={actionFilter} onChange={event => setActionFilter(event.target.value)}><option value="all">全部动作</option><option value="baseline">人工基线</option><option value="optimize">优化</option><option value="replan">局部重排</option><option value="activate">历史激活</option><option value="restore">业务回滚</option><option value="reattest">重新验证</option><option value="experiment_publish">实验发布</option></select></label><label>策略<select aria-label="按策略筛选" value={strategyFilter} onChange={event => setStrategyFilter(event.target.value)}><option value="all">全部策略</option>{strategies.map(item => <option key={item} value={item}>{strategyLabel[item] || item}</option>)}</select></label><label>求解状态<select aria-label="按求解状态筛选" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">全部状态</option>{statuses.map(item => <option key={item} value={item}>{item}</option>)}</select></label></div>
      <div className="version-compare"><span>跨版本比较</span><select aria-label="比较起点" value={beforeId} onChange={event => setBeforeId(event.target.value)}><option value="">起点版本</option>{trustedPlans.map(item => <option key={item.id} value={item.id}>V{String(item.number).padStart(3, '0')} · {item.label}</option>)}</select><span>→</span><select aria-label="比较终点" value={afterId} onChange={event => setAfterId(event.target.value)}><option value="">终点版本</option>{trustedPlans.map(item => <option key={item.id} value={item.id}>V{String(item.number).padStart(3, '0')} · {item.label}</option>)}</select><button disabled={!beforeId || !afterId || beforeId === afterId} onClick={() => { const before = trustedPlans.find(item => item.id === beforeId); const after = trustedPlans.find(item => item.id === afterId); if (before && after) onCompare(before, after) }}><BarChart3 size={15} />比较</button></div>
    </div>
    <div className="version-list">
      {visible.map(item => {
        const schedule = item.selected
        const source = item.source_version_id ? sourceById.get(item.source_version_id) : undefined
        const trusted = item.effective_integrity === 'VERIFIED'
        return <article key={item.id} className={`version-row ${item.active ? 'current' : ''} ${trusted ? '' : 'untrusted'}`}>
          <button className="version-open" onClick={() => onOpen(item)} aria-label={`打开 V${String(item.number).padStart(3, '0')}`}><div className="version-mark"><b>V{String(item.number).padStart(3, '0')}</b><span>{item.active ? !item.applicability.route_executable ? '当前版本 · 路线受影响' : !item.applicability.coverage_complete ? '最后发布 · 部分覆盖' : item.applicability.reoptimization_opportunity ? '当前路线 · 可再优化' : '当前执行方案' : kindLabel[schedule.kind]}</span></div></button>
          <div className="version-name"><strong>{item.label}</strong><small>{strategyLabel[schedule.strategy] || schedule.strategy} · D{String(item.data_revision).padStart(3, '0')} · {new Date(item.created_at).toLocaleString('zh-CN')}</small><small>证据：{item.effective_integrity}{!trusted ? ' · 仅供审计，不可执行业务操作' : ''}</small>{source && <small>来源 V{String(source.number).padStart(3, '0')} · {source.label}</small>}</div>
          <div className="version-kpi">{trusted ? <><span>计划覆盖 <b>{pct(schedule.kpis.completion_rate)}</b></span><span>计划 SLA <b>{pct(schedule.kpis.committed_on_time_rate)}</b></span><span>行程 <b>{schedule.kpis.total_travel_minutes}′</b></span><span>未分配 <b>{schedule.kpis.unassigned_count}</b></span></> : <span><b>业务数字已隐藏</b><small>先重新验证为新 V</small></span>}</div>
          <div className="version-state"><span className={`solver-dot ${schedule.solver_status.toLowerCase()}`} />{schedule.solver_status}<small>计算 {schedule.runtime_ms} ms</small>{!item.applicability.route_executable ? <em>路线需重排</em> : !item.applicability.coverage_complete ? <em>新增需求未覆盖</em> : item.applicability.reoptimization_opportunity ? <em>新增容量可优化</em> : !item.applicability.metrics_current ? <em>指标已过期</em> : item.data_revision !== scenario.revision && <em>历史数据 D{String(item.data_revision).padStart(3, '0')}</em>}</div>
          <div className="version-actions"><button onClick={() => { const label = window.prompt('方案名称', item.label); if (label?.trim() && label.trim() !== item.label) onRename(item, label.trim()) }}>重命名</button>{trusted ? <a href={`/api/scenarios/${scenario.id}/plan-versions/${item.id}/report`} target="_blank" rel="noreferrer">报告</a> : <button onClick={() => onReattest(item)}>重新验证为新 V</button>}<button disabled={!trusted || (item.active && item.data_revision === scenario.revision)} onClick={() => onActivate(item)}>重新激活</button><button disabled={!trusted} onClick={() => onClone(item)}>克隆场景</button><button disabled={!trusted} className="restore" onClick={() => onRestore(item)}>回滚业务数据</button></div>
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

export function ReviewView({ scenarioId, planVersionId, schedule, baseline }: { scenarioId: string; planVersionId?: string; schedule?: Schedule; baseline?: Schedule }) {
  const [cost, setCost] = useState<CostAnalysis>()
  const [risk, setRisk] = useState<RiskSimulation>()
  const [capacity, setCapacity] = useState<CapacityAnalysis>()
  const [analysisNumbers, setAnalysisNumbers] = useState<{ cost?: number; risk?: number; capacity?: number }>({})
  const [capacityMode, setCapacityMode] = useState<'SELECTED_PLAN_DELTA' | 'CONTROLLED_REOPTIMIZATION'>('SELECTED_PLAN_DELTA')
  const [horizonDays, setHorizonDays] = useState(1)
  const [decisionErrors, setDecisionErrors] = useState<string[]>([])
  const [retryableRuns, setRetryableRuns] = useState<DecisionAnalysisRun[]>([])
  const [evidenceRuns, setEvidenceRuns] = useState<DecisionAnalysisRun[]>([])
  const [planEvidence, setPlanEvidence] = useState<PlanVersion>()
  const [capacityAnalysisRunId, setCapacityAnalysisRunId] = useState<string>()
  const [riskAnalysisRunId, setRiskAnalysisRunId] = useState<string>()
  const [capacityArtifact, setCapacityArtifact] = useState<CapacityCounterfactualArtifact>()
  const [riskScenarioArtifact, setRiskScenarioArtifact] = useState<SimulationScenarioSetArtifact>()
  const [loadingExisting, setLoadingExisting] = useState(() => !!planVersionId)
  const [loadingDecision, setLoadingDecision] = useState(false)
  useEffect(() => {
    let cancelled = false
    if (!planVersionId) return () => { cancelled = true }
    Promise.all([api.analysisRuns(scenarioId, planVersionId), api.planVersion(scenarioId, planVersionId)])
      .then(([runs, plan]) => {
        if (cancelled) return
        setEvidenceRuns(runs); setPlanEvidence(plan)
        const latest = (type: DecisionAnalysisRun['analysis_type']) => [...runs].reverse().find(item => item.analysis_type === type && item.status === 'COMPLETED' && item.effective_integrity === 'VERIFIED' && item.result)
        const costRun = latest('COST') as DecisionAnalysisRun<CostAnalysis> | undefined
        const riskRun = latest('RISK') as DecisionAnalysisRun<RiskSimulation> | undefined
        const capacityRun = latest('CAPACITY') as DecisionAnalysisRun<CapacityAnalysis> | undefined
        setCost(costRun?.result || undefined); setRisk(riskRun?.result || undefined); setCapacity(capacityRun?.result || undefined)
        setCapacityAnalysisRunId(capacityRun?.id)
        setRiskAnalysisRunId(riskRun?.id)
        setAnalysisNumbers({ cost: costRun?.number, risk: riskRun?.number, capacity: capacityRun?.number })
        const issues = runs.filter(item => item.status === 'FAILED' || item.status === 'INTERRUPTED' || item.integrity_status === 'FAILED')
        if (issues.length) {
          setRetryableRuns(issues.filter(item => item.status === 'FAILED' || item.status === 'INTERRUPTED').slice(-3))
          setDecisionErrors(issues.slice(-3).map(item => `A${String(item.number).padStart(3, '0')} ${item.integrity_status === 'FAILED' ? '完整性校验失败' : typeof item.error?.message === 'string' ? item.error.message : item.status === 'INTERRUPTED' ? '分析被中断' : '分析失败'}`))
        }
      })
      .catch(error => { if (!cancelled) setDecisionErrors([error instanceof Error ? error.message : '读取经营分析失败']) })
      .finally(() => { if (!cancelled) setLoadingExisting(false) })
    return () => { cancelled = true }
  }, [scenarioId, planVersionId])
  if (!schedule) return <section className="page-view"><div className="empty-view">请先生成一个方案，再查看运营复盘。</div></section>
  const publicationRemaining = schedule.kind === 'replan' && !!planEvidence?.publication_planning_context
  const effectiveHorizonDays = publicationRemaining ? 1 : horizonDays
  const planTrusted = planEvidence?.effective_integrity === 'VERIFIED'
  const completedResult = <T,>(run: DecisionAnalysisRun<T>, label: string): T => {
    if (run.effective_integrity !== 'VERIFIED') throw new Error(`A${String(run.number).padStart(3, '0')}：证据链未通过完整性校验`)
    if (run.status === 'COMPLETED' && run.result) return run.result
    const message = typeof run.error?.message === 'string' ? run.error.message : `${label}没有完成`
    throw new Error(`A${String(run.number).padStart(3, '0')}：${message}`)
  }
  const waitForRun = async <T extends CostAnalysis | CapacityAnalysis | RiskSimulation,>(initial: DecisionAnalysisRun<T>): Promise<DecisionAnalysisRun<T>> => {
    let current = initial
    for (let attempt = 0; current.status === 'RUNNING' && attempt < 40; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 500))
      current = await api.decisionAnalysisRun<T>(scenarioId, current.id)
    }
    if (current.status === 'RUNNING') throw new Error(`A${String(current.number).padStart(3, '0')} 仍在运行，请稍后回到本页查看。`)
    return current
  }
  const rememberRetryable = (run: DecisionAnalysisRun) => {
    if (run.status !== 'FAILED' && run.status !== 'INTERRUPTED') return
    setRetryableRuns(current => [...new Map([...current, run].map(item => [item.id, item])).values()])
  }
  const retryAnalysis = async (run: DecisionAnalysisRun) => {
    setLoadingDecision(true); setDecisionErrors([])
    try {
      const retried = await waitForRun(await api.retryDecisionAnalysisRun(scenarioId, run.id))
      if (retried.status !== 'COMPLETED' || !retried.result) {
        rememberRetryable(retried)
        throw new Error(`A${String(retried.number).padStart(3, '0')}：${typeof retried.error?.message === 'string' ? retried.error.message : '重试没有完成'}`)
      }
      if (retried.analysis_type === 'COST') {
        setCost(retried.result as CostAnalysis); setAnalysisNumbers(current => ({ ...current, cost: retried.number }))
      } else if (retried.analysis_type === 'RISK') {
        setRisk(retried.result as RiskSimulation); setRiskAnalysisRunId(retried.id); setRiskScenarioArtifact(undefined); setAnalysisNumbers(current => ({ ...current, risk: retried.number }))
      } else {
        setCapacity(retried.result as CapacityAnalysis); setCapacityAnalysisRunId(retried.id); setAnalysisNumbers(current => ({ ...current, capacity: retried.number }))
      }
      setRetryableRuns(current => current.filter(item => item.logical_analysis_id !== retried.logical_analysis_id))
    } catch (error) {
      setDecisionErrors([error instanceof Error ? error.message : '经营分析重试失败'])
    } finally { setLoadingDecision(false) }
  }
  const runCostAndRisk = async () => {
    if (!planVersionId) return
    setLoadingDecision(true); setDecisionErrors([])
    const [costOutcome, riskOutcome] = await Promise.allSettled([
      api.createDecisionAnalysisRun<CostAnalysis>(scenarioId, planVersionId, 'COST', { horizonDays: effectiveHorizonDays }).then(waitForRun),
      api.createDecisionAnalysisRun<RiskSimulation>(scenarioId, planVersionId, 'RISK', { horizonDays: effectiveHorizonDays }).then(waitForRun),
    ])
    const errors: string[] = []
    if (costOutcome.status === 'fulfilled') {
      try { setCost(completedResult(costOutcome.value, '成本分析')); setAnalysisNumbers(current => ({ ...current, cost: costOutcome.value.number })) } catch (error) { rememberRetryable(costOutcome.value); errors.push(error instanceof Error ? error.message : '成本分析失败') }
    } else errors.push(costOutcome.reason instanceof Error ? costOutcome.reason.message : '成本分析失败')
    if (riskOutcome.status === 'fulfilled') {
      try { setRisk(completedResult(riskOutcome.value, '风险分析')); setRiskAnalysisRunId(riskOutcome.value.id); setRiskScenarioArtifact(undefined); setAnalysisNumbers(current => ({ ...current, risk: riskOutcome.value.number })) } catch (error) { rememberRetryable(riskOutcome.value); errors.push(error instanceof Error ? error.message : '风险分析失败') }
    } else errors.push(riskOutcome.reason instanceof Error ? riskOutcome.reason.message : '风险分析失败')
    setDecisionErrors(errors); setLoadingDecision(false)
  }
  const runCapacity = async () => {
    if (!planVersionId) return
    setLoadingDecision(true); setDecisionErrors([])
    let run: DecisionAnalysisRun<CapacityAnalysis> | undefined
    try {
      const created = await waitForRun(await api.createDecisionAnalysisRun<CapacityAnalysis>(scenarioId, planVersionId, 'CAPACITY', { referenceMode: capacityMode, horizonDays: effectiveHorizonDays }))
      run = created
      setCapacity(completedResult(created, '容量分析')); setCapacityAnalysisRunId(created.id); setCapacityArtifact(undefined); setAnalysisNumbers(current => ({ ...current, capacity: created.number }))
    } catch (error) { if (run) rememberRetryable(run); setDecisionErrors([error instanceof Error ? error.message : '容量分析失败']) }
    finally { setLoadingDecision(false) }
  }
  const showCapacityArtifact = async (artifactId: string) => {
    if (!capacityAnalysisRunId) return
    try {
      const artifact = await api.decisionAnalysisArtifact(scenarioId, capacityAnalysisRunId, artifactId)
      if (artifact.artifact_type !== 'CAPACITY_COUNTERFACTUAL') throw new Error('该证据不是容量反事实结果')
      setCapacityArtifact(artifact)
    } catch (error) {
      setDecisionErrors([error instanceof Error ? error.message : '读取容量证据失败'])
    }
  }
  const showRiskScenarioArtifact = async () => {
    if (!riskAnalysisRunId || !risk?.scenario_set_artifact_id) return
    try {
      const artifact = await api.decisionAnalysisArtifact(scenarioId, riskAnalysisRunId, risk.scenario_set_artifact_id)
      if (artifact.artifact_type !== 'SIMULATION_SCENARIO_SET') throw new Error('该证据不是风险场景集')
      setRiskScenarioArtifact(artifact)
    } catch (error) {
      setDecisionErrors([error instanceof Error ? error.message : '读取风险场景证据失败'])
    }
  }
  const downloadEvidence = (name: string, payload: unknown) => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click(); URL.revokeObjectURL(url)
  }
  const breakdownLabels: Record<string, string> = { travel: '行程代价', sla_late: 'SLA 延迟代价', overtime: '加班代价', unassigned: '未分配代价', imbalance: '负载不均代价', replan_changes: '方案变更代价' }
  const cadenceLabel = { ONE_TIME: '一次性', PER_DAY: '每日', PER_SHIFT: '每班', PER_ORDER: '每单', PER_MONTH: '每月' }
  const unitLabel = { INVESTMENT: '项投入', PLAN_DAY: '个计划日', TECHNICIAN_SHIFT: '个技师班次/日', WORK_ORDER: '张工单/日', WORK_MONTH: '个工作月' }
  const pp = (value: number | null) => value == null ? '—' : `${value > 0 ? '+' : ''}${value}pp`
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
  const analysisScope = cost?.analysis_scope ?? risk?.analysis_scope ?? capacity?.analysis_scope
  const remainingScope = analysisScope === 'PUBLICATION_REMAINING_PLAN'
  return <section className="page-view">
    <div className="page-title"><div><span className="eyebrow">OPERATIONS REVIEW</span><h1>运营复盘</h1><p>查看计算耗时、现场耗时，以及各项指标相对基线的变化。</p></div><span className="revision-badge">{strategyLabel[schedule.strategy]}策略</span></div>
    <div className="review-summary"><div><small>方案计算用时</small><b>{schedule.runtime_ms}<em> ms</em></b><p>生成本方案所用的时间。</p></div><div><small>业务评分</small><b>{schedule.business_score?.toLocaleString() ?? '—'}</b><p>{schedule.business_score_policy_version} 重算结果；求解器原始目标为 {schedule.solver_objective_value?.toLocaleString() ?? '—'}。</p></div><div><small>计划占用时间</small><b>{schedule.kpis.total_travel_minutes + schedule.kpis.total_waiting_minutes + schedule.kpis.total_service_minutes}<em> 分钟</em></b><p>计划行程、等待和服务时间合计，不代表实际工时。</p></div></div>
    {tradeoffs.length > 0 && <div className="tradeoff-card"><ShieldCheck size={20} /><div><h2>与基线相比</h2><p>{tradeoffs.join('；')}。方案排序依据为当前策略权重。</p></div></div>}
    <div className="review-grid"><article><h2>目标值构成</h2><div className="cost-bars">{Object.entries(schedule.objective_breakdown).map(([key, value]) => <div key={key}><span>{breakdownLabels[key] || key}</span><i><b style={{ width: `${value / maxCost * 100}%` }} /></i><strong>{Math.round(value)}</strong></div>)}</div></article><article><h2>技师工作量</h2>{schedule.kpis.technician.map(item => <div className="util-row" key={item.technician_id}><span>{item.technician_id}</span><i><b style={{ width: `${Math.min(100, item.utilization * 100)}%` }} /></i><strong>{pct(item.utilization)}</strong><small>{item.assignment_count} 单</small></div>)}</article></div>
    <div className="decision-head"><div><span className="eyebrow">DECISION SUPPORT</span><h2>经营决策测算</h2><p>分析绑定 V{String(cost?.plan_number ?? risk?.plan_number ?? schedule.version).padStart(3, '0')} 的冻结方案、旅行模型、政策与代码来源。进入页面只读取已有 A；点击生成才会新增记录，不会生成 D 或 V。</p></div><div className="decision-actions"><label>成本/容量周期（工作日）<input aria-label="分析工作日" type="number" min="1" max={publicationRemaining ? 1 : 365} disabled={publicationRemaining} value={effectiveHorizonDays} onChange={event => setHorizonDays(Math.max(1, Math.min(365, Number(event.target.value) || 1)))} /><small>{publicationRemaining ? '剩余计划固定为当日一次性口径' : '完整计划可按工作日外推'}</small></label><button disabled={!planVersionId || !planTrusted || loadingDecision} onClick={runCostAndRisk}>生成成本与风险分析</button><label>容量参照<select aria-label="容量分析参照" value={capacityMode} onChange={event => { setCapacityMode(event.target.value as typeof capacityMode); setCapacity(undefined); setAnalysisNumbers(current => ({ ...current, capacity: undefined })) }}><option value="SELECTED_PLAN_DELTA">相对当前 V</option><option value="CONTROLLED_REOPTIMIZATION">相对同算法重算基线</option></select></label><button disabled={!planVersionId || !planTrusted || loadingDecision} onClick={runCapacity}>测算六种容量方案</button></div></div>
    {planEvidence && <div className={`evidence-ledger ${(planEvidence.integrity_status || 'LEGACY_UNATTESTED').toLowerCase()}`}><div><b>V{String(planEvidence.number).padStart(3, '0')} 发布证明 · {planEvidence.integrity_status || 'LEGACY_UNATTESTED'}</b><span>{!planEvidence.attestation_requirement || planEvidence.attestation_requirement === 'LEGACY_MIGRATED' ? '迁移前历史记录，未达到当前证明标准。' : `发布清单 ${planEvidence.publication_manifest_hash?.slice(0, 12)}…`}</span></div><details><summary>查看 PublicationContext 与验证证据</summary><pre>{JSON.stringify({ publication_planning_context: planEvidence.publication_planning_context, publication_verification_artifact: planEvidence.publication_verification_artifact }, null, 2)}</pre></details><button onClick={() => downloadEvidence(`V${String(planEvidence.number).padStart(3, '0')}-publication-evidence.json`, planEvidence)}>下载证据</button></div>}
    {evidenceRuns.some(run => run.attestation_requirement === 'LEGACY_MIGRATED') && <div className="decision-status legacy"><AlertTriangle size={16} />历史 A 记录来自证明制度启用前，可查看但不应与 VERIFIED 记录等同解释。</div>}
    {evidenceRuns.length > 0 && <div className="analysis-ledger">{evidenceRuns.slice(-8).map(run => <details key={run.id} className={(run.integrity_status || 'LEGACY_UNATTESTED').toLowerCase()}><summary>A{String(run.number).padStart(3, '0')} · {run.analysis_type} · {run.integrity_status || 'LEGACY_UNATTESTED'}</summary><p>{run.analysis_scope} · {run.status} · input {run.input_hash ? `${run.input_hash.slice(0, 12)}…` : 'legacy'}</p><button onClick={() => downloadEvidence(`A${String(run.number).padStart(3, '0')}-manifest.json`, { input_manifest: run.input_manifest, result_manifest: run.result_manifest, failure_manifest: run.failure_manifest, artifact_manifest: run.artifact_manifest, runtime_manifest: run.runtime_manifest, analysis_manifest_hash: run.analysis_manifest_hash })}>下载 Manifest</button></details>)}</div>}
    <div className="decision-scope-banner"><ShieldCheck size={18} /><div><b>{remainingScope ? '发布时剩余计划范围' : '完整冻结计划范围'}</b><span>{remainingScope ? '从重排发布时的路线入口开始，成本、容量和风险排除已冻结服务。' : '分析完整发布计划，不把查询时的新执行事实混入历史输入。'}{Math.max(cost?.current_execution_watermark || 0, risk?.current_execution_watermark || 0, capacity?.current_execution_watermark || 0) > 0 ? ` 已绑定发布执行水位 ${Math.max(cost?.current_execution_watermark || 0, risk?.current_execution_watermark || 0, capacity?.current_execution_watermark || 0)}。` : ''}</span></div></div>
    {!planVersionId && <div className="empty-view compact">当前显示的排程尚未对应公开版本，无法冻结经营测算输入。</div>}
    {loadingExisting && <div className="decision-status">正在读取已有分析记录…</div>}
    {loadingDecision && <div className="decision-status">正在生成冻结快照分析记录…</div>}
    {decisionErrors.map(error => <div className="decision-status error" key={error}>{error}</div>)}
    {retryableRuns.map(run => <div className="decision-status error retryable" key={run.id}><span>A{String(run.number).padStart(3, '0')} 已{run.status === 'INTERRUPTED' ? '中断' : '失败'}，原记录会保留。</span><button disabled={loadingDecision} onClick={() => retryAnalysis(run)}>重试 A{String(run.number).padStart(3, '0')}</button></div>)}
    {!loadingExisting && !cost && !risk && !capacity && planVersionId && <div className="empty-view compact">当前版本还没有经营分析。选择周期后显式生成，已执行事实不会被混入事前口径。</div>}
    {(cost || risk) && <div className="decision-summary">
      <article><small>预计现金运营成本</small><b>{cost ? money(cost.breakdown.cash_operating_cost_cents) : '—'}</b><p>{cost ? `单日口径 · A${String(analysisNumbers.cost).padStart(3, '0')}` : '正常人工、加班基础工资、溢价、行程与外包现金支出'}</p></article>
      <article><small>正常人工</small><b>{cost ? money(cost.breakdown.regular_labor_cost_cents) : '—'}</b><p>占用分钟或正常付费班次，取决于人工政策</p></article>
      <article><small>加班基础工资</small><b>{cost ? money(cost.breakdown.overtime_base_cost_cents) : '—'}</b><p>付费班次模式单列；占用分钟模式已含在正常人工中</p></article>
      <article><small>加班溢价</small><b>{cost ? money(cost.breakdown.overtime_premium_cost_cents) : '—'}</b><p>加班分钟 × 人工单价 × 溢价率</p></article>
      <article><small>预计服务损失</small><b>{cost ? money(cost.breakdown.service_failure_loss_cents) : '—'}</b><p>SLA 损失与未服务机会损失，不是现金支出</p></article>
      <article><small>{cost ? `${cost.analysis_horizon.days} 日总经济影响` : '总经济影响'}</small><b>{cost ? money(cost.horizon_total_economic_impact_cents) : '—'}</b><p>现金成本与服务损失之和，不等同于财务结算</p></article>
      <article><small>原发布承诺风险 SLA</small><b>{risk ? pct(risk.published_commitment_sla_rate) : '—'}</b><p>{risk ? `模拟均值抽样区间 ${pct(risk.monte_carlo_mean_ci_low)}–${pct(risk.monte_carlo_mean_ci_high)} · A${String(analysisNumbers.risk).padStart(3, '0')}` : '—'}</p></article>
      <article><small>全需求风险 SLA</small><b>{risk ? pct(risk.all_demand_sla_rate) : '—'}</b><p>{risk ? `紧急单完成 ${pct(risk.emergency_completion_rate)} · 按时 ${pct(risk.emergency_on_time_rate)}` : '原计划与实际发生的紧急需求使用同一分母'}</p></article>
      <article><small>{remainingScope ? '剩余范围总迟到 P95' : '冻结范围总迟到 P95'}</small><b>{risk ? `${risk.scope_total_late_minutes_p95} 分钟` : '—'}</b><p>P50 {risk?.scope_total_late_minutes_p50 ?? '—'} · P90 {risk?.scope_total_late_minutes_p90 ?? '—'} 分钟</p></article>
      <article><small>新增业务损害概率</small><b>{risk ? pct(risk.additional_disruption_probability) : '—'}</b><p>{risk ? `突发事件发生 ${pct(risk.emergency_event_probability)} · 实际致损 ${pct(risk.emergency_caused_failure_probability)} · 条件致损 ${pct(risk.emergency_failure_given_event_probability)}` : '—'}</p></article>
      <article><small>技师缺勤</small><b>{risk ? pct(risk.absence_caused_failure_probability) : '—'}</b><p>{risk ? `事件发生 ${pct(risk.technician_absence_event_probability)} · 只有实际改变结果才计为致损` : '—'}</p></article>
    </div>}
    {risk && <div className="risk-evidence-actions"><button disabled={!risk.scenario_set_artifact_id} onClick={showRiskScenarioArtifact}>查看共同随机场景集</button></div>}
    {riskScenarioArtifact && <article className="tradeoff-card capacity-evidence" aria-live="polite"><ShieldCheck size={20} /><div><h2>风险共同场景集</h2><p>完整性：{riskScenarioArtifact.integrity_status} · {riskScenarioArtifact.trials} trials · 突发事件 {riskScenarioArtifact.emergency_events.length} 个 · 指纹 {riskScenarioArtifact.scenario_set_hash.slice(0, 12)}…</p><details><summary>查看外生参数与事件样例</summary><pre>{JSON.stringify({ exogenous_parameters: riskScenarioArtifact.exogenous_parameters, emergency_events: riskScenarioArtifact.emergency_events.slice(0, 20) }, null, 2)}</pre></details><button onClick={() => downloadEvidence('risk-scenario-set.json', riskScenarioArtifact)}>下载场景集</button><button onClick={() => setRiskScenarioArtifact(undefined)}>关闭证据</button></div></article>}
    {capacity && <div className="capacity-table-wrap"><div className="capacity-reference"><b>{capacity.reference_mode === 'SELECTED_PLAN_DELTA' ? '相对当前 V · 尾部追加' : '相对同算法重算基线'}</b><span>{capacity.reference_mode === 'SELECTED_PLAN_DELTA' ? '保留现有 assignment，仅在路线尾部追加；这不是完整插入优化。' : '基准和选项使用相同贪心政策。'}{analysisNumbers.capacity ? ` · A${String(analysisNumbers.capacity).padStart(3, '0')}` : ''} · {capacity.analysis_horizon.days} 个工作日</span></div><table className="capacity-table"><thead><tr><th>容量方案</th><th>可执行性</th><th>完成率改善</th><th>SLA 改善</th><th>周期总影响</th><th>固定成本口径</th><th>证据</th></tr></thead><tbody>{capacity.options.map(item => <tr key={item.option_id} className={item.feasible ? '' : 'capacity-invalid'}><td><b>{item.name}</b><small>{item.assumption}</small></td><td>{item.decision_status === 'EXTERNAL_CONDITIONAL' ? '条件方案 · 供应商待确认' : item.feasible ? '可执行' : item.option_applicable ? '不可执行' : '不适用'}{item.violations.length ? <details><summary>{item.violations.length} 项说明</summary><ul>{item.violations.map((violation, index) => <li key={`${violation.code}-${index}`}>{violation.message}</li>)}</ul></details> : <small>完整约束校验通过</small>}</td><td>{pp(item.completion_improvement_percentage_points)}{item.conditional_upper_bound_kpis && <small>条件上界 {pct(item.conditional_upper_bound_kpis.completion_rate)}</small>}</td><td>{pp(item.sla_improvement_percentage_points)}{item.conditional_upper_bound_kpis && <small>条件上界 {pct(item.conditional_upper_bound_kpis.sla_on_time_rate)}</small>}</td><td>{item.horizon_total_impact_cents == null ? '—' : money(item.horizon_total_impact_cents)}</td><td>{cadenceLabel[item.fixed_cost_cadence]} {money(item.fixed_capacity_cost_cents)}<small>{item.cost_units_per_day} {unitLabel[item.cost_unit_type]}</small><small>{item.economic_impact_offset_days == null ? '无可计算经济影响抵消点' : `${item.economic_impact_offset_days} 日抵消经济影响`}</small></td><td><button disabled={!item.artifact_id} onClick={() => item.artifact_id && showCapacityArtifact(item.artifact_id)}>查看证据</button></td></tr>)}</tbody></table><p className="decision-note">条件外包只显示全部接受时的上界，不进入正式可执行排序；不可执行方案只保留诊断路线与违规。</p></div>}
    {capacityArtifact && <article className="tradeoff-card capacity-evidence" aria-live="polite"><ShieldCheck size={20} /><div><h2>{capacityArtifact.option_id} 反事实证据</h2><p>完整性：{capacityArtifact.integrity_status} · {capacityArtifact.formal_result_available ? '正式结果可用' : '仅条件测算'} · 结构校验 {capacityArtifact.structural_verification.valid ? '通过' : '失败'} · 商业验证 {capacityArtifact.commercial_verification_status} · 指纹 {capacityArtifact.artifact_hash.slice(0, 12)}…</p><p>内部分配 {capacityArtifact.counterfactual_kpis?.internal_assignment_count ?? '—'} · 外部承接 {capacityArtifact.counterfactual_kpis?.external_assignment_count ?? 0} · 未服务 {capacityArtifact.counterfactual_kpis?.unserved_count ?? '—'}</p>{capacityArtifact.external_assignments.length > 0 && <><p className="external-assumption-warning"><b>供应商容量未验证</b>承接数量和 SLA 仅是条件上界；没有供应商确认的开始、完成时间，不能作为现场承诺或正式比较结果。</p>{capacityArtifact.conditional_assumptions.length > 0 && <ul>{capacityArtifact.conditional_assumptions.map(item => <li key={item}>{item}</li>)}</ul>}<details><summary>外部承接清单（{capacityArtifact.external_assignments.length}）</summary><ul>{capacityArtifact.external_assignments.map(item => <li key={item.work_order_id}>{item.work_order_id} · {item.provider_id} · {item.capacity_verified ? '容量已验证' : '容量未验证'} · {item.assumed_on_time ? '假设 SLA 内完成' : '未承诺准时'}</li>)}</ul></details></>}<button onClick={() => setCapacityArtifact(undefined)}>关闭证据</button></div></article>}
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
  const executionLocked = initial?.status === 'started' || initial?.status === 'completed'
  const patch = <K extends keyof WorkOrder>(key: K, value: WorkOrder[K]) => setOrder(current => ({ ...current, [key]: value }))
  const submit = async (replan: boolean) => { if (!valid) return; setSaving(true); try { await onSave(order, replan) } finally { setSaving(false) } }
  return <div className="modal-backdrop"><section className="editor-modal" role="dialog" aria-modal="true" aria-labelledby="work-order-editor-title">
    <div className="editor-head"><div><span className="eyebrow">WORK ORDER</span><h2 id="work-order-editor-title">{initial ? `编辑 ${initial.id}` : order.is_emergency ? '登记突发工单' : '新增工单'}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭工单编辑"><X /></button></div>
    <div className="form-grid"><label className="span-2">客户名称<input disabled={executionLocked} value={order.customer_name} onChange={e => patch('customer_name', e.target.value)} placeholder="例如：衡安数据中心" /></label><label className="span-2">任务内容<input disabled={executionLocked} value={order.title} onChange={e => patch('title', e.target.value)} placeholder="例如：核心机房断电告警" /></label>
      <fieldset className="span-2"><legend>所需技能</legend>{Object.entries(skillLabel).map(([key, label]) => <label className="check" key={key}><input disabled={executionLocked} type="checkbox" checked={order.required_skills.includes(key)} onChange={e => patch('required_skills', e.target.checked ? [...order.required_skills, key] : order.required_skills.filter(item => item !== key))} />{label}</label>)}</fieldset>
      <label>允许开始时间<ServiceTimeInput disabled={executionLocked} label="允许开始时间" value={order.window_start} onChange={value => patch('window_start', value)} /></label><label>最晚开始时间<ServiceTimeInput disabled={executionLocked} label="最晚开始时间" value={order.window_end} onChange={value => patch('window_end', value)} /></label><label>SLA 截止<ServiceTimeInput disabled={executionLocked} label="SLA 截止" value={order.sla_deadline} onChange={value => patch('sla_deadline', value)} /></label><label>服务时长（分钟）<input disabled={executionLocked} type="number" min="5" max="480" value={order.service_duration} onChange={e => patch('service_duration', Number(e.target.value))} /></label>
      <label>横坐标<input disabled={executionLocked} type="number" min="0" max="100" value={order.location.x} onChange={e => patch('location', { ...order.location, x: Number(e.target.value) })} /></label><label>纵坐标<input disabled={executionLocked} type="number" min="0" max="100" value={order.location.y} onChange={e => patch('location', { ...order.location, y: Number(e.target.value) })} /></label>
      <label>优先级<select disabled={executionLocked} value={order.priority} onChange={e => patch('priority', e.target.value as WorkOrder['priority'])}><option value="urgent">紧急</option><option value="high">高</option><option value="normal">普通</option><option value="low">低</option></select></label><label>执行状态<span className="readonly-field">{{ pending: '待处理', started: '服务中', completed: '已完成' }[order.status]}</span></label>
      <label className="switch-line span-2"><input disabled={executionLocked} type="checkbox" checked={order.is_emergency} onChange={e => { if (e.target.checked) { setOrder(current => ({ ...current, is_emergency: true, priority: 'urgent', drop_penalty: Math.max(8000, current.drop_penalty), reported_at: current.reported_at ?? 600 })) } else { setOrder(current => ({ ...current, is_emergency: false, drop_penalty: current.priority === 'high' ? 4800 : current.priority === 'low' ? 1400 : 2500, reported_at: null })) } }} /><Zap size={15} />标记为突发工单<span>突发单会在局部重排中获得更高的保留优先级</span></label>
      {order.is_emergency && <label>接报时间<ServiceTimeInput disabled={executionLocked} label="接报时间" value={order.reported_at ?? 600} onChange={value => patch('reported_at', value)} /></label>}
      <label className="span-2">现场备注<textarea value={order.note} onChange={e => patch('note', e.target.value)} rows={3} /></label>
    </div>
    {!valid && <p className="form-error">请填写客户、任务和至少一项技能，并检查时间设置。</p>}
    <div className="editor-actions">{initial && onDelete && initial.status === 'pending' && <button className="delete-btn" disabled={saving} onClick={() => onDelete(order)}><Trash2 size={15} />删除工单</button>}<button onClick={onClose}>取消</button><button className="page-primary" disabled={!valid || saving} onClick={() => submit(false)}><Save size={15} />保存</button>{order.is_emergency && <button className="emergency-save" disabled={!valid || saving} onClick={() => submit(true)}><Zap size={15} />保存并局部重排</button>}</div>
  </section></div>
}

const blankTechnician = (): Technician => ({ id: `TECH-${String(Date.now()).slice(-3)}`, name: '', skills: ['electrical'], shift_start: 480, shift_end: 1020, start_location: { x: 48, y: 52 }, overtime_limit: 60, cost_per_minute_cents: 100, color: '#315c4b' })

export function TechnicianEditor({ initial, onClose, onSave }: { initial?: Technician; onClose: () => void; onSave: (tech: Technician) => Promise<void> }) {
  const [tech, setTech] = useState<Technician>(() => initial ? structuredClone(initial) : blankTechnician())
  const [saving, setSaving] = useState(false)
  const patch = <K extends keyof Technician>(key: K, value: Technician[K]) => setTech(current => ({ ...current, [key]: value }))
  const valid = tech.name.trim() && tech.skills.length && tech.shift_end > tech.shift_start
  return <div className="modal-backdrop"><section className="editor-modal compact" role="dialog" aria-modal="true" aria-labelledby="technician-editor-title"><div className="editor-head"><div><span className="eyebrow">TECHNICIAN</span><h2 id="technician-editor-title">{initial ? `编辑 ${initial.name}` : '新增技师'}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭技师编辑"><X /></button></div>
    <div className="form-grid"><label>技师编号<input value={tech.id} disabled={!!initial} onChange={e => patch('id', e.target.value)} /></label><label>姓名<input value={tech.name} onChange={e => patch('name', e.target.value)} /></label><fieldset className="span-2"><legend>技能</legend>{Object.entries(skillLabel).map(([key, label]) => <label className="check" key={key}><input type="checkbox" checked={tech.skills.includes(key)} onChange={e => patch('skills', e.target.checked ? [...tech.skills, key] : tech.skills.filter(item => item !== key))} />{label}</label>)}</fieldset><label>班次开始<ServiceTimeInput allowNextDay={false} label="班次开始" value={tech.shift_start} onChange={value => patch('shift_start', value)} /></label><label>班次结束<ServiceTimeInput label="班次结束" value={tech.shift_end} onChange={value => patch('shift_end', value)} /></label><label>加班上限（分钟）<input type="number" min="0" max="240" value={tech.overtime_limit} onChange={e => patch('overtime_limit', Number(e.target.value))} /></label><label>每分钟人工成本（分）<input type="number" min="1" step="1" value={tech.cost_per_minute_cents} onChange={e => patch('cost_per_minute_cents', Number(e.target.value))} /><small>整数分，180 表示 ¥1.80/分钟</small></label></div>
    <div className="editor-actions"><button onClick={onClose}>取消</button><button className="page-primary" disabled={!valid || saving} onClick={async () => { setSaving(true); try { await onSave(tech) } finally { setSaving(false) } }}><Save size={15} />保存技师</button></div>
  </section></div>
}
