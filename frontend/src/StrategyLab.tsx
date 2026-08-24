import { useEffect, useMemo, useState } from 'react'
import { Beaker, Check, Edit3, Gauge, Plus, RefreshCw, Save, Square, Trash2, X } from 'lucide-react'
import { api } from './api'
import type { PlanVersion, Scenario, StrategyExperiment, StrategyProfile, StrategyWeights } from './types'

const pct = (value: number) => `${Math.round(value * 100)}%`
const experimentStatusLabel: Record<StrategyExperiment['status'], string> = {
  QUEUED: '等待运行', RUNNING: '运行中', CANCEL_REQUESTED: '正在取消', CANCELLED: '已取消',
  COMPLETED: '已完成', COMPLETED_WITH_ERRORS: '部分完成', FAILED: '失败', INTERRUPTED: '已中断',
}
const activeExperimentStatuses: StrategyExperiment['status'][] = ['QUEUED', 'RUNNING', 'CANCEL_REQUESTED']
const publishableExperimentStatuses: StrategyExperiment['status'][] = ['COMPLETED', 'COMPLETED_WITH_ERRORS']
const defaultWeights: StrategyWeights = {
  travel_weight: 4, sla_late_weight: 12, overtime_weight: 30,
  imbalance_weight: 1, replan_change_weight: 80, unassigned_penalty_scale: 1,
}

type ProfileDraft = { id?: string; name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }

function ProfileEditor({ initial, onClose, onSaved }: { initial?: StrategyProfile; onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState<ProfileDraft>(() => initial ? { id: initial.id, name: initial.name, description: initial.description, weights: { ...initial.weights }, time_limit_seconds: initial.time_limit_seconds } : { name: '我的调度策略', description: '按当前排班要求设置权重', weights: { ...defaultWeights }, time_limit_seconds: 2 })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const weightFields: { key: keyof StrategyWeights; label: string; step?: number; min?: number; max?: number }[] = [
    { key: 'travel_weight', label: '行程权重', min: 0, max: 1000 },
    { key: 'sla_late_weight', label: 'SLA 延迟权重', min: 0, max: 1000 },
    { key: 'overtime_weight', label: '加班权重', min: 0, max: 1000 },
    { key: 'imbalance_weight', label: '工作量失衡权重', min: 0, max: 1000 },
    { key: 'replan_change_weight', label: '重排变更权重', min: 0, max: 2000 },
    { key: 'unassigned_penalty_scale', label: '未分配惩罚倍率', min: .1, max: 5, step: .1 },
  ]
  const save = async () => {
    setSaving(true); setError('')
    try {
      const payload = { name: draft.name, description: draft.description, weights: draft.weights, time_limit_seconds: draft.time_limit_seconds }
      if (draft.id) await api.updateStrategyProfile(draft.id, payload)
      else await api.createStrategyProfile(payload)
      onSaved(); onClose()
    } catch (cause) { setError(cause instanceof Error ? cause.message : '策略保存失败') }
    finally { setSaving(false) }
  }
  return <div className="modal-backdrop"><section className="editor-modal strategy-editor" role="dialog" aria-modal="true" aria-labelledby="strategy-editor-title">
    <div className="editor-head"><div><span className="eyebrow">STRATEGY PROFILE</span><h2 id="strategy-editor-title">{initial ? '编辑自定义策略' : '新建自定义策略'}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭策略编辑"><X /></button></div>
    <div className="form-grid"><label>策略名称<input value={draft.name} onChange={event => setDraft(current => ({ ...current, name: event.target.value }))} /></label><label>单项求解时限（秒）<input type="number" min="1" max="30" step="1" value={draft.time_limit_seconds} onChange={event => setDraft(current => ({ ...current, time_limit_seconds: Number(event.target.value) }))} /></label><label className="span-2">使用说明<textarea rows={2} value={draft.description} onChange={event => setDraft(current => ({ ...current, description: event.target.value }))} /></label>
      {weightFields.map(field => <label key={field.key}>{field.label}<input type="number" min={field.min} max={field.max} step={field.step || 1} value={draft.weights[field.key]} onChange={event => setDraft(current => ({ ...current, weights: { ...current.weights, [field.key]: Number(event.target.value) } }))} /></label>)}
    </div>
    <p className="profile-note">原始目标值受策略权重影响，不能横向比较。实验结果会另按同一套公式计分。</p>
    {error && <p className="form-error">{error}</p>}
    <div className="editor-actions"><button onClick={onClose}>取消</button><button className="page-primary" disabled={saving || draft.name.trim().length < 2} onClick={save}><Save size={15} />保存策略</button></div>
  </section></div>
}

export function StrategyLab({ scenario, profiles, loadingDataset, onSelectDataset, onReloadProfiles, onPublished, onToast }: { scenario: Scenario; profiles: StrategyProfile[]; loadingDataset: boolean; onSelectDataset: (id: string) => void; onReloadProfiles: () => Promise<void>; onPublished: (plan: PlanVersion) => void; onToast: (message: string) => void }) {
  const selectable = profiles.filter(profile => profile.id !== 'stable')
  const [selected, setSelected] = useState<string[]>([])
  const [experiment, setExperiment] = useState<StrategyExperiment>()
  const [editor, setEditor] = useState<StrategyProfile | null | undefined>()
  const [working, setWorking] = useState('')

  useEffect(() => {
    if (!selected.length && selectable.length) setSelected(selectable.map(profile => profile.id))
  }, [selectable.length])

  useEffect(() => {
    if (!experiment || !activeExperimentStatuses.includes(experiment.status)) return
    let cancelled = false
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.experiment(experiment.scenario_id, experiment.id, controller.signal)
        if (!cancelled) setExperiment(next)
      } catch (cause) {
        if (!cancelled && !controller.signal.aborted) onToast(cause instanceof Error ? cause.message : '实验进度读取失败')
      }
    }, 650)
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer) }
  }, [experiment?.id, experiment?.status])

  const run = async () => {
    setWorking('正在准备实验')
    try { setExperiment(await api.createExperiment(scenario.id, selected)); onToast('实验已开始。发布候选前不会生成 V 版本。') }
    catch (cause) { onToast(cause instanceof Error ? cause.message : '策略实验启动失败') }
    finally { setWorking('') }
  }

  const publish = async (candidateId: string) => {
    if (!experiment) return
    setWorking('正在发布候选方案')
    try {
      const plan = await api.publishExperiment(scenario.id, experiment.id, candidateId, scenario.revision)
      onPublished(plan); onToast(`已发布为 V${String(plan.number).padStart(3, '0')}`)
    } catch (cause) { onToast(cause instanceof Error ? cause.message : '候选发布失败') }
    finally { setWorking('') }
  }

  const cancel = async () => {
    if (!experiment) return
    setWorking('正在取消实验')
    try { setExperiment(await api.cancelExperiment(scenario.id, experiment.id)); onToast('已提交取消请求，当前求解结束后会停止。') }
    catch (cause) { onToast(cause instanceof Error ? cause.message : '实验取消失败') }
    finally { setWorking('') }
  }

  const bestScore = useMemo(() => {
    const candidates = experiment?.candidates.filter(item => item.publishable) || []
    return candidates.length ? Math.min(...candidates.map(item => item.evaluation_score)) : undefined
  }, [experiment])
  return <section className="page-view strategy-lab" aria-busy={loadingDataset}>
    <div className="page-title"><div><span className="eyebrow">STRATEGY LAB</span><h1>策略实验室</h1><p>在同一份数据上比较各策略的计划覆盖、计划 SLA、行程、加班和工作量。</p></div><button className="page-primary" disabled={loadingDataset} onClick={() => setEditor(null)}><Plus size={15} />自定义策略</button></div>
    <div className="lab-dataset"><div><span>实验数据集</span><strong>{loadingDataset ? '正在载入数据…' : scenario.name}</strong><small>{scenario.technicians.length} 名技师 · {scenario.work_orders.length} 个工单 · D{String(scenario.revision).padStart(3, '0')}</small></div><div><button disabled={loadingDataset} className={scenario.id === 'strategy-medium' ? 'active' : ''} onClick={() => onSelectDataset('strategy-medium')}>中型 8 / 60</button><button disabled={loadingDataset} className={scenario.id === 'strategy-stress' ? 'active' : ''} onClick={() => onSelectDataset('strategy-stress')}>压力型 12 / 100</button></div></div>
    <div className="profile-selector"><div className="lab-section-head"><div><span className="eyebrow">CANDIDATES</span><h2>选择参与实验的策略</h2></div><div className="experiment-controls"><button className={`run-experiment ${experiment && activeExperimentStatuses.includes(experiment.status) ? 'running' : ''}`} disabled={loadingDataset || !!working || !!(experiment && activeExperimentStatuses.includes(experiment.status)) || !selected.length} onClick={run}>{experiment && activeExperimentStatuses.includes(experiment.status) ? <RefreshCw size={16} /> : <Beaker size={16} />}{experiment?.status === 'QUEUED' ? '等待前序实验' : experiment?.status === 'RUNNING' ? `运行中 ${experiment.progress}%` : experiment?.status === 'CANCEL_REQUESTED' ? '正在取消' : '运行所选策略'}</button>{experiment && ['QUEUED', 'RUNNING'].includes(experiment.status) && <button className="cancel-experiment" disabled={!!working} onClick={cancel}><Square size={14} />取消实验</button>}</div></div>
      <div className="profile-grid">{selectable.map(profile => <article className={`profile-card ${selected.includes(profile.id) ? 'selected' : ''}`} key={profile.id}><label><input type="checkbox" disabled={loadingDataset} checked={selected.includes(profile.id)} onChange={event => setSelected(current => event.target.checked ? [...current, profile.id] : current.filter(id => id !== profile.id))} /><span><strong>{profile.name}</strong><small>{profile.description}</small></span></label><div className="profile-weights"><span>行程 {profile.weights.travel_weight}</span><span>SLA {profile.weights.sla_late_weight}</span><span>加班 {profile.weights.overtime_weight}</span><span>公平 {profile.weights.imbalance_weight}</span></div>{!profile.builtin && <div className="profile-actions"><button disabled={loadingDataset} onClick={() => setEditor(profile)}><Edit3 size={14} />编辑</button><button disabled={loadingDataset} onClick={async () => { if (!window.confirm(`删除策略“${profile.name}”？`)) return; try { await api.deleteStrategyProfile(profile.id); await onReloadProfiles(); setSelected(current => current.filter(id => id !== profile.id)) } catch (cause) { onToast(cause instanceof Error ? cause.message : '删除失败') } }}><Trash2 size={14} />删除</button></div>}</article>)}</div>
    </div>
    {experiment && <div className="experiment-results"><div className="lab-section-head"><div><span className="eyebrow">RESULT MATRIX</span><h2>同场结果</h2><p>对比得分越低越好。所有候选都按 {experiment.score_policy_version} 公式重新计分，不直接比较求解器原始目标值。</p></div><span className={`experiment-status ${experiment.status.toLowerCase()}`}>{experiment.status === 'RUNNING' ? `${experimentStatusLabel.RUNNING} ${experiment.progress}%` : experimentStatusLabel[experiment.status]}</span></div>
      {experiment.error && <div className="stale-banner"><Gauge size={16} /><div><strong>{experiment.status === 'COMPLETED_WITH_ERRORS' ? '部分策略未完成' : '实验没有完成'}</strong><span>{experiment.error}</span></div></div>}
      {Object.keys(experiment.candidate_errors).length > 0 && <div className="candidate-errors" role="alert">{Object.entries(experiment.candidate_errors).map(([profileId, message]) => <p key={profileId}><b>{profiles.find(item => item.id === profileId)?.name || profileId}：</b>{message}</p>)}</div>}
      {!!experiment.candidates.length && <div className="candidate-table-wrap"><table className="candidate-table"><thead><tr><th>策略</th><th>计划覆盖</th><th>计划 SLA</th><th>行程</th><th>加班</th><th>负载差</th><th>对比得分</th><th>操作</th></tr></thead><tbody>{experiment.candidates.map(candidate => <tr key={candidate.id} className={`${candidate.publishable && candidate.evaluation_score === bestScore ? 'recommended' : ''} ${candidate.pareto_optimal ? 'pareto' : 'dominated'}`}><td><strong>{candidate.profile_name}</strong><div>{candidate.pareto_optimal && <span>帕累托前沿</span>}{candidate.advantages.map(item => <span key={item}>{item}</span>)}</div></td><td>{pct(candidate.schedule.kpis.completion_rate)}</td><td>{pct(candidate.schedule.kpis.committed_on_time_rate)}</td><td>{candidate.schedule.kpis.total_travel_minutes}′</td><td>{candidate.schedule.kpis.total_overtime_minutes}′</td><td>{candidate.schedule.kpis.normalized_workload_range.toFixed(2)}</td><td><b>{candidate.evaluation_score.toLocaleString()}</b><small>{candidate.schedule.solver_status} · {candidate.schedule.runtime_ms} ms</small>{!candidate.pareto_optimal && <small>被 {candidate.dominated_by.length} 个候选支配</small>}</td><td><button title={candidate.publishable ? '发布这一候选' : '该候选未通过完整性和约束校验'} disabled={!candidate.publishable || !!working || !publishableExperimentStatuses.includes(experiment.status) || !!experiment.winner_candidate_id} onClick={() => publish(candidate.id)}><Check size={14} />{experiment.winner_candidate_id === candidate.id ? '已发布' : '选择并发布'}</button></td></tr>)}</tbody></table></div>}
    </div>}
    {working && <div className="working lab-working"><RefreshCw size={14} />{working}</div>}
    {editor !== undefined && <ProfileEditor initial={editor || undefined} onClose={() => setEditor(undefined)} onSaved={onReloadProfiles} />}
  </section>
}
