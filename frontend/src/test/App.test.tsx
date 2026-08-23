import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from '../App'
import { WorkOrderEditor } from '../Management'
import type { PlanVersion, Scenario, Schedule, StrategyProfile } from '../types'

const scenario: Scenario = {
  id: 'main', name: '今日调度测试', description: '交互验收', planning_date: '2026-08-23', seed: 1, revision: 0,
  locked_assignments: [],
  technicians: [{ id: 'TECH-01', name: '林乔', skills: ['electrical'], shift_start: 480, shift_end: 1020, start_location: { x: 48, y: 52 }, overtime_limit: 60, cost_per_minute: 1, color: '#315c4b' }],
  work_orders: [{ id: 'WO-1', customer_name: '测试客户', title: '线路检修', required_skills: ['electrical'], location: { x: 55, y: 60 }, service_duration: 30, window_start: 540, window_end: 660, sla_deadline: 630, priority: 'normal', drop_penalty: 2500, status: 'pending', vip: false, is_emergency: false, reported_at: null, note: '' }],
}

const schedule: Schedule = {
  id: 'SCH-1', scenario_id: 'main', kind: 'optimized', version: 2, created_at: '2026-08-23T10:00:00Z', solver_status: 'FEASIBLE', runtime_ms: 72, objective: 100,
  // Deliberately reference a missing order: this used to crash RouteMap/Timeline.
  assignments: [{ work_order_id: 'WO-MISSING', technician_id: 'TECH-01', sequence: 1, arrival_time: 560, start_time: 560, finish_time: 590, travel_minutes: 10, sla_late_minutes: 0, explanation: [], evidence: {}, locked: false, changed: false }],
  unassigned: [], source_schedule_id: null, solver_note: '测试计划', scenario_revision: 0, strategy: 'balanced',
  objective_breakdown: { travel: 20, sla_late: 0, overtime: 0, unassigned: 0, imbalance: 0, replan_changes: 80 },
  requested_time_limit_ms: 1000, effective_time_limit_ms: 1000, solver_status_code: 1,
  termination_reason: 'ROUTING_SUCCESS', solution_found: true, solver_objective_value: 100,
  business_score: 100, business_score_policy_version: 'FIELD_SERVICE_SCORE_V2',
  scenario_snapshot_hash: 'test', solver_config_hash: 'test', travel_model_version: 'EUCLIDEAN_GRID_V2',
  metric_policy_version: 'FIELD_SERVICE_METRICS_V2', solver_name: 'test', solver_version: '1',
  kpis: { completion_rate: 1, sla_on_time_rate: 1, sla_late_count: 0, total_travel_minutes: 10, total_service_minutes: 30, total_overtime_minutes: 0, average_utilization: .5, unassigned_count: 0, high_priority_missed: 0, workload_stddev: 0, stability_rate: null, assigned_on_time_rate: 1, committed_on_time_rate: 1, total_late_minutes: 0, p90_late_minutes: 0, total_waiting_minutes: 0, average_occupied_utilization: .67, workload_range: 0, normalized_workload_range: 0, same_technician_rate: null, adjacency_preservation_rate: null, start_time_shift_median: null, start_time_shift_p90: null, start_time_shift_over_15m_count: null, customer_notification_count: null, technician: [{ technician_id: 'TECH-01', service_minutes: 30, travel_minutes: 10, overtime_minutes: 0, utilization: .5, assignment_count: 1, waiting_minutes: 0, occupied_minutes: 40, service_utilization: .5, occupied_utilization: .67, travel_ratio: .25, waiting_ratio: 0, overtime_ratio: 0, normalized_workload: .67 }] },
}

const plan: PlanVersion = {
  id: 'PV-1', scenario_id: 'main', number: 1, action: 'optimize', label: '均衡优化', data_revision: 0,
  source_version_id: null, relation: 'new', active: true, created_at: '2026-08-23T10:00:00Z',
  selected: schedule, scenario_snapshot: scenario,
  artifacts: [{ id: 'ART-1', role: 'baseline', strategy: 'balanced', schedule: { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' } }],
  candidate_id: 'CAND-1', scenario_snapshot_hash: 'test', source_plan_snapshot_hash: null,
}

const profiles: StrategyProfile[] = [{ id: 'balanced', name: '均衡', description: '均衡业务指标', builtin: true, time_limit_seconds: 2, created_at: '2026-08-23T00:00:00Z', weights: { travel_weight: 4, sla_late_weight: 12, overtime_weight: 8, imbalance_weight: 1, replan_change_weight: 80, unassigned_penalty_scale: 1 } }]

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mockApi() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const body = url.endsWith('/api/scenarios') ? [scenario]
      : url.endsWith('/api/strategy-profiles') ? profiles
      : url.endsWith('/plan-versions') ? [plan]
      : url.includes('/plan-versions/') ? plan
      : url.endsWith('/schedules') ? [schedule]
      : url.endsWith('/baseline') ? { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' }
      : scenario
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
})
