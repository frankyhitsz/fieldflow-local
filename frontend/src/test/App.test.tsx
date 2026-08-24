import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../App'
import { VersionsView, WorkOrderEditor } from '../Management'
import type { PlanVersion, Scenario, Schedule, StrategyProfile } from '../types'

const scenario: Scenario = {
  id: 'main', name: '今日调度测试', description: '交互验收', planning_date: '2026-08-23', seed: 1, revision: 0,
  locked_assignments: [],
  technicians: [{ id: 'TECH-01', name: '林乔', skills: ['electrical'], shift_start: 480, shift_end: 1020, start_location: { x: 48, y: 52 }, overtime_limit: 60, cost_per_minute_cents: 100, color: '#315c4b' }],
  work_orders: [{ id: 'WO-1', customer_name: '测试客户', title: '线路检修', required_skills: ['electrical'], location: { x: 55, y: 60 }, service_duration: 30, window_start: 540, window_end: 660, sla_deadline: 630, priority: 'normal', drop_penalty: 2500, status: 'pending', vip: false, is_emergency: false, reported_at: null, note: '' }],
}
const mediumScenario: Scenario = { ...scenario, id: 'strategy-medium', name: '策略中型数据' }

const schedule: Schedule = {
  id: 'SCH-1', scenario_id: 'main', kind: 'optimized', version: 2, created_at: '2026-08-23T10:00:00Z', solver_status: 'FEASIBLE', runtime_ms: 72, objective: 100,
  // Deliberately reference a missing order: this used to crash RouteMap/Timeline.
  assignments: [{ work_order_id: 'WO-MISSING', technician_id: 'TECH-01', sequence: 1, arrival_time: 560, start_time: 560, finish_time: 590, travel_minutes: 10, sla_late_minutes: 0, explanation: [], evidence: {}, locked: false, changed: false }],
  unassigned: [], source_schedule_id: null, solver_note: '测试计划', scenario_revision: 0, strategy: 'balanced',
  objective_breakdown: { travel: 20, sla_late: 0, overtime: 0, unassigned: 0, imbalance: 0, replan_changes: 80 },
  requested_time_limit_ms: 1000, effective_time_limit_ms: 1000, solver_status_code: 1,
  termination_reason: 'ROUTING_SUCCESS', solution_found: true, solver_objective_value: 100,
  business_score: 100, business_score_policy_version: 'FIELD_SERVICE_SCORE_V2',
  scenario_snapshot_hash: 'test', solver_config_hash: 'test', solver_policy: null, travel_model_version: 'EUCLIDEAN_GRID_V2', travel_model_fingerprint: 'test',
  metric_policy_version: 'FIELD_SERVICE_METRICS_V2', solver_name: 'test', solver_version: '1',
  kpis: { completion_rate: 1, sla_on_time_rate: 1, sla_late_count: 0, total_travel_minutes: 10, total_service_minutes: 30, total_overtime_minutes: 0, average_utilization: .5, unassigned_count: 0, high_priority_missed: 0, workload_stddev: 0, stability_rate: null, assigned_on_time_rate: 1, committed_on_time_rate: 1, total_late_minutes: 0, p90_late_minutes: 0, total_waiting_minutes: 0, average_occupied_utilization: .67, workload_range: 0, normalized_workload_range: 0, same_technician_rate: null, adjacency_preservation_rate: null, start_time_shift_median: null, start_time_shift_p90: null, start_time_shift_over_15m_count: null, customer_notification_count: null, technician: [{ technician_id: 'TECH-01', service_minutes: 30, travel_minutes: 10, overtime_minutes: 0, utilization: .5, assignment_count: 1, waiting_minutes: 0, occupied_minutes: 40, service_utilization: .5, occupied_utilization: .67, travel_ratio: .25, waiting_ratio: 0, overtime_ratio: 0, normalized_workload: .67 }] },
}

const plan: PlanVersion = {
  id: 'PV-1', scenario_id: 'main', number: 1, action: 'optimize', label: '均衡优化', data_revision: 0,
  source_version_id: null, lineage_source_version_id: null, stability_baseline_version_id: null, relation: 'new', active: true, created_at: '2026-08-23T10:00:00Z',
  coverage_status: 'CURRENT_AND_COMPLETE',
  selected: schedule, scenario_snapshot: scenario,
  artifacts: [{ id: 'ART-1', role: 'baseline', strategy: 'balanced', schedule: { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' } }],
  candidate_id: 'CAND-1', scenario_snapshot_hash: 'test', source_plan_snapshot_hash: null,
}

const profiles: StrategyProfile[] = [{ id: 'balanced', name: '均衡', description: '均衡业务指标', builtin: true, time_limit_seconds: 2, created_at: '2026-08-23T00:00:00Z', weights: { travel_weight: 4, sla_late_weight: 12, overtime_weight: 8, imbalance_weight: 1, replan_change_weight: 80, unassigned_penalty_scale: 1 } }]

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mockApi(activePlan: PlanVersion = plan) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const analysisRequest = url.endsWith('/analysis-runs') && init?.body ? JSON.parse(String(init.body)) as { analysis_type: 'COST' | 'CAPACITY' | 'RISK' } : undefined
    const effectiveUrl = analysisRequest ? analysisRequest.analysis_type === 'COST' ? '/cost-analysis' : analysisRequest.analysis_type === 'RISK' ? '/risk-simulation' : '/capacity-analysis' : url
    let body: unknown = effectiveUrl.endsWith('/api/scenarios') ? [scenario]
      : effectiveUrl.endsWith('/api/strategy-profiles') ? profiles
      : effectiveUrl.endsWith('/api/scenarios/strategy-medium') ? mediumScenario
      : effectiveUrl.endsWith('/cost-analysis') ? { scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', schedule_signature: 'selected', analysis_scope: 'FULL_DAY_PLAN', travel_model_fingerprint: 'travel', analysis_code_version: '0.5.1', analysis_input_hash: 'cost-input', policy: {}, policy_fingerprint: 'cost', assumptions: [], breakdown: { labor_cost_cents: 100000, travel_cost_cents: 10000, overtime_cost_cents: 2000, sla_penalty_cents: 5000, unserved_revenue_cents: 6400, outsourcing_cost_cents: 0, cash_operating_cost_cents: 112000, service_failure_loss_cents: 11400, total_economic_impact_cents: 123400, total_cost_cents: 123400, technician_cost_cents: { 'TECH-01': 102000 } } }
      : effectiveUrl.endsWith('/risk-simulation') ? { scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', schedule_signature: 'selected', analysis_scope: 'FULL_DAY_PLAN', travel_model_fingerprint: 'travel', execution_policy: 'FOLLOW_PUBLISHED_SCHEDULE', execution_policy_version: 'V2', simulation_policy_version: 'V2', analysis_code_version: '0.5.1', simulation_input_hash: 'risk', seed: 7, trials: 500, expected_sla_on_time_rate: .875, sla_rate_ci_low: .85, sla_rate_ci_high: .9, late_minutes_p50: 10, late_minutes_p90: 28, late_minutes_p95: 35, expected_overtime_minutes: 4.5, additional_disruption_probability: .125, baseline_unserved_orders: 0, expected_total_unserved_orders: .25, plan_failure_probability: .125, expected_unserved_orders: .25, assumptions: [] }
      : effectiveUrl.endsWith('/capacity-analysis') ? { scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', analysis_scope: 'FULL_DAY_PLAN', analysis_code_version: '0.5.1', analysis_input_hash: 'capacity-input', evaluation_method: 'SELECTED_PLAN_ANCHORED_INCREMENTAL_GREEDY_V2', reference_mode: 'SELECTED_PLAN_DELTA', selected_plan_signature: 'selected', reference_schedule_signature: 'selected', reference_solver_policy_fingerprint: 'policy', reference_travel_model_fingerprint: 'travel', reference_kpis: schedule.kpis, cost_policy_fingerprint: 'cost', capacity_policy: {}, capacity_policy_fingerprint: 'capacity', base_schedule_signature: 'selected', base_cost: {}, options: [{ option_id: 'add_technician', name: '增加一名复合技能技师', assumption: '测算假设', feasible: true, completion_rate: 1, sla_on_time_rate: 1, unassigned_count: 0, travel_minutes: 8, overtime_minutes: 0, completion_improvement_percentage_points: 5, sla_improvement_percentage_points: 8, unassigned_delta: -1, travel_delta_minutes: -2, overtime_delta_minutes: 0, fixed_capacity_cost_cents: 60000, marginal_cost_cents: 50000, projected_total_cost_cents: 173400, schedule_signature: 'capacity' }] }
      : effectiveUrl.endsWith('/plan-versions') ? [activePlan]
      : effectiveUrl.includes('/plan-versions/') ? activePlan
      : effectiveUrl.endsWith('/schedules') ? [schedule]
      : effectiveUrl.endsWith('/baseline') ? { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' }
      : scenario
    if (analysisRequest) body = { id: `AN-${analysisRequest.analysis_type}`, scenario_id: 'main', number: { COST: 1, RISK: 2, CAPACITY: 3 }[analysisRequest.analysis_type], plan_version_id: activePlan.id, plan_number: activePlan.number, analysis_type: analysisRequest.analysis_type, scenario_snapshot_hash: 'test', schedule_hash: 'selected', execution_watermark: null, travel_model_fingerprint: 'travel', policy_version: 'V2', policy_snapshot: {}, code_version: '0.5.1', input_hash: `${analysisRequest.analysis_type}-input`, status: 'COMPLETED', result: body, created_at: '2026-08-24T00:00:00Z' }
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
}

describe('FieldFlow navigation and render safety', () => {
  it('opens every primary navigation page', async () => {
    mockApi(); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '方案版本' }))
    expect(screen.getByRole('heading', { name: '方案版本' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '技师与技能' }))
    expect(screen.getByRole('heading', { name: '技师与技能' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '策略实验室' }))
    expect(screen.getByRole('heading', { name: '策略实验室' })).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '策略中型数据' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    expect(screen.getByRole('heading', { name: '运营复盘' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '今日调度' }))
    expect(screen.getByRole('heading', { name: '工单队列' })).toBeInTheDocument()
  })

  it('does not white-screen when a schedule contains an unknown order', async () => {
    mockApi(); render(<App />)
    await waitFor(() => expect(screen.getByRole('heading', { name: '工单队列' })).toBeInTheDocument())
    expect(screen.getByRole('img', { name: '工单位置与技师路线图' })).toBeInTheDocument()
    expect(screen.queryByText('页面数据没有完整加载')).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).endsWith('/schedules'))).toBe(false)
  })

  it('shows an actionable startup diagnosis when the backend is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network unavailable') }))
    render(<App />)
    expect(await screen.findByText(/请在项目目录运行“make demo”/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新连接' })).toBeInTheDocument()
  })

  it('edits next-day service times without wrapping them to the current day', () => {
    const order = { ...scenario.work_orders[0], window_end: 1500, sla_deadline: 1530 }
    render(<WorkOrderEditor initial={order} onClose={() => undefined} onSave={async () => undefined} />)
    expect(screen.getByRole('combobox', { name: '时间窗结束日期' })).toHaveValue('1')
    expect(screen.getByLabelText('时间窗结束', { selector: 'input' })).toHaveValue('01:00')
    expect(screen.getByLabelText('SLA 截止', { selector: 'input' })).toHaveValue('01:30')
  })

  it('filters plan history by action, strategy, and solver status', () => {
    const baselinePlan: PlanVersion = {
      ...plan, id: 'PV-BASE', number: 1, action: 'baseline', label: '人工基线', active: false,
      selected: { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline', solver_status: 'OPTIMAL' },
    }
    const optimizedPlan: PlanVersion = { ...plan, id: 'PV-OPT', number: 2 }
    const callbacks = {
      onOpen: () => undefined, onActivate: () => undefined, onClone: () => undefined,
      onRestore: () => undefined, onCompare: () => undefined, onRename: () => undefined,
      onReset: () => undefined,
    }
    render(<VersionsView scenario={scenario} plans={[baselinePlan, optimizedPlan]} {...callbacks} />)
    expect(screen.getByRole('button', { name: '打开 V001' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开 V002' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('按策略筛选'), { target: { value: 'baseline' } })
    expect(screen.getByRole('button', { name: '打开 V001' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '打开 V002' })).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('按策略筛选'), { target: { value: 'all' } })
    fireEvent.change(screen.getByLabelText('按求解状态筛选'), { target: { value: 'FEASIBLE' } })
    expect(screen.queryByRole('button', { name: '打开 V001' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开 V002' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('按动作筛选'), { target: { value: 'baseline' } })
    expect(screen.getByText('当前筛选下没有方案。生成基线、优化或发布实验候选后会从 V001 开始记录。')).toBeInTheDocument()
  })

  it('uses an independent actual execution time instead of the replan cutoff', async () => {
    const validSchedule: Schedule = {
      ...schedule,
      assignments: [{ ...schedule.assignments[0], work_order_id: 'WO-1', arrival_time: 550, start_time: 560, finish_time: 590 }],
    }
    const validPlan: PlanVersion = { ...plan, selected: validSchedule }
    mockApi(validPlan)
    render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.change(screen.getByLabelText('重排时点（当日起分钟数）'), { target: { value: '900' } })
    fireEvent.click(screen.getByRole('button', { name: '全部' }))
    fireEvent.click(screen.getByText('WO-1'))
    fireEvent.click(screen.getByRole('button', { name: '开始服务' }))
    expect(screen.getByRole('dialog', { name: '登记开始服务' })).toBeInTheDocument()
    expect(screen.getByLabelText('实际发生时间')).toHaveValue(560)
    expect(screen.getByText('09:20，与重排时点相互独立')).toBeInTheDocument()
  })

  it('loads frozen cost and risk analysis and runs capacity what-if', async () => {
    mockApi(); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    expect(await screen.findByText(/1,234\.00/)).toBeInTheDocument()
    expect(screen.getByText('88%')).toBeInTheDocument()
    expect(screen.getByText('35 分钟')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '测算六种容量方案' }))
    expect(await screen.findByText('增加一名复合技能技师')).toBeInTheDocument()
    expect(screen.getByText('+5pp')).toBeInTheDocument()
  })
})
