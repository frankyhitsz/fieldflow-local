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
  kpis: { completion_rate: 1, sla_on_time_rate: 1, sla_late_count: 0, total_travel_minutes: 10, total_service_minutes: 30, total_overtime_minutes: 0, average_utilization: .5, unassigned_count: 0, high_priority_missed: 0, workload_stddev: 0, stability_rate: null, assigned_on_time_rate: 1, committed_on_time_rate: 1, total_late_minutes: 0, p90_late_minutes: 0, total_waiting_minutes: 0, average_occupied_utilization: .67, workload_range: 0, normalized_workload_range: 0, min_normalized_workload: .67, max_normalized_workload: .67, same_technician_rate: null, adjacency_preservation_rate: null, start_time_shift_median: null, start_time_shift_p90: null, start_time_shift_over_15m_count: null, customer_notification_count: null, technician: [{ technician_id: 'TECH-01', service_minutes: 30, travel_minutes: 10, overtime_minutes: 0, utilization: .5, assignment_count: 1, waiting_minutes: 0, occupied_minutes: 40, service_utilization: .5, occupied_utilization: .67, travel_ratio: .25, waiting_ratio: 0, overtime_ratio: 0, normalized_workload: .67 }] },
}

const plan: PlanVersion = {
  id: 'PV-1', scenario_id: 'main', number: 1, action: 'optimize', label: '均衡优化', data_revision: 0,
  source_version_id: null, lineage_source_version_id: null, stability_baseline_version_id: null, relation: 'new', active: true, created_at: '2026-08-23T10:00:00Z',
  coverage_status: 'CURRENT_AND_COMPLETE',
  applicability: { route_executable: true, coverage_complete: true, planning_current: true, metrics_current: true, commercial_current: true, reoptimization_opportunity: false, invalid_assignment_ids: [], evaluated_scenario_revision: 0, evaluated_scenario_snapshot_hash: 'test', reducer_policy_version: 'FIELD_SERVICE_PLAN_APPLICABILITY_V2', projection_hash: 'projection-test' },
  selected: schedule, scenario_snapshot: scenario,
  artifacts: [{ id: 'ART-1', role: 'baseline', strategy: 'balanced', schedule: { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' } }],
  candidate_id: 'CAND-1', scenario_snapshot_hash: 'test', published_schedule_hash: 'schedule-test',
  publication_verification_policy_version: 'FIELD_SERVICE_PUBLICATION_VERIFICATION_V1',
  publication_verification_report_hash: 'verification-test', source_plan_snapshot_hash: null,
  publication_manifest_version: 'PLAN_PUBLICATION_MANIFEST_V2',
  attestation_requirement: 'REQUIRED', integrity_status: 'VERIFIED',
  schedule_integrity: 'VERIFIED', source_solver_provenance: null, inherited_source_solver_policy: null,
  replay_validation_policy: null, reattestation_mode: null,
  self_integrity: 'VERIFIED', effective_integrity: 'VERIFIED',
}

const profiles: StrategyProfile[] = [{ id: 'balanced', name: '均衡', description: '均衡业务指标', builtin: true, time_limit_seconds: 2, created_at: '2026-08-23T00:00:00Z', weights: { travel_weight: 4, sla_late_weight: 12, overtime_weight: 8, imbalance_weight: 1, replan_change_weight: 80, unassigned_penalty_scale: 1 } }]

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mockApi(activePlan: PlanVersion = plan, options: { failRisk?: boolean; existingFailed?: boolean; noEmergency?: boolean; riskGate?: Promise<void>; dispatchPlan?: PlanVersion } = {}) {
  const submittedAnalyses = new Map<string, { analysis_type: 'COST' | 'CAPACITY' | 'RISK'; analysis_scope?: string; request: { analysis_horizon?: { days: number } } }>()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const isAnalysisJob = url.endsWith('/analysis-jobs') && init?.method === 'POST'
    const submittedRequest = isAnalysisJob && init?.body ? JSON.parse(String(init.body)) as { analysis_type: 'COST' | 'CAPACITY' | 'RISK'; analysis_scope?: string; request: { analysis_horizon?: { days: number } } } : undefined
    if (submittedRequest) submittedAnalyses.set(`AN-${submittedRequest.analysis_type}`, submittedRequest)
    const analysisId = url.match(/\/analysis-runs\/(AN-(?:COST|CAPACITY|RISK))$/)?.[1]
    const analysisRequest = submittedRequest || (analysisId ? submittedAnalyses.get(analysisId) : undefined)
    if (analysisRequest?.analysis_type === 'RISK' && options.riskGate) await options.riskGate
    const effectiveUrl = analysisRequest ? analysisRequest.analysis_type === 'COST' ? '/cost-analysis' : analysisRequest.analysis_type === 'RISK' ? '/risk-simulation' : '/capacity-analysis' : url
    const context = { analysis_scope: 'EX_ANTE_FROZEN_PLAN', current_execution_watermark: 2, analysis_as_of_time: 630, execution_context_hash: 'events', actual_execution_included: false, algorithm_version: 'FIELD_SERVICE_DECISION_V3', build_sha: 'test-sha' }
    const breakdown = { regular_labor_cost_cents: 100000, full_day_committed_labor_cost_cents: 100000, remaining_incremental_labor_cost_cents: 100000, overtime_base_cost_cents: 1000, overtime_premium_cost_cents: 1000, labor_cost_cents: 100000, travel_cost_cents: 10000, overtime_cost_cents: 1000, sla_penalty_cents: 5000, unserved_revenue_cents: 6400, outsourcing_cost_cents: 0, cash_operating_cost_cents: 112000, service_failure_loss_cents: 11400, total_economic_impact_cents: 123400, total_cost_cents: 123400, technician_cost_cents: { 'TECH-01': 102000 } }
    const invalid = activePlan.applicability.invalid_assignment_ids.includes('WO-1')
    const assigned = activePlan.selected.assignments.some(item => item.work_order_id === 'WO-1')
    const disposition = invalid ? 'ASSIGNED_INVALID' : assigned ? 'ASSIGNED_VALID' : 'NEW_UNCOVERED'
    const operational = { scenario_id: 'main', scenario_revision: 0, scenario_snapshot_hash: 'test', active_plan_version_id: activePlan.id, plan_applicability: activePlan.applicability, execution_watermark: 0, execution_context_hash: 'execution-context', execution_integrity: 'VERIFIED', work_orders: [{ work_order_id: 'WO-1', disposition, assignment: assigned ? activePlan.selected.assignments.find(item => item.work_order_id === 'WO-1') : null, start_allowed: disposition === 'ASSIGNED_VALID', complete_allowed: false, start_blocking_reason_code: invalid ? 'INVALID_ASSIGNMENT_CANNOT_START' : assigned ? null : 'NEW_DEMAND_NOT_IN_ACTIVE_PLAN', complete_blocking_reason_code: 'WORK_ORDER_NOT_STARTED', blocking_reason_code: invalid ? 'INVALID_ASSIGNMENT_CANNOT_START' : assigned ? null : 'NEW_DEMAND_NOT_IN_ACTIVE_PLAN' }], current_metrics: { active_demand_count: 1, valid_assigned_count: disposition === 'ASSIGNED_VALID' ? 1 : 0, invalid_assignment_count: invalid ? 1 : 0, plan_unassigned_count: 0, new_uncovered_count: disposition === 'NEW_UNCOVERED' ? 1 : 0, current_actionable_coverage_rate: disposition === 'ASSIGNED_VALID' ? 1 : 0 } }
    const days = analysisRequest?.request.analysis_horizon?.days || 1
    let body: unknown = effectiveUrl.endsWith('/api/scenarios') ? [scenario]
      : effectiveUrl.endsWith('/api/strategy-profiles') ? profiles
      : effectiveUrl.endsWith('/api/scenarios/main/dispatch-snapshot') ? { scenario, scenario_head_snapshot_hash: 'test', latest_revision_hash: 'revision-test', scenario_proof_origin: 'NATIVE_ATTESTED', active_plan: options.dispatchPlan || activePlan, operational_view: options.dispatchPlan ? { ...operational, active_plan_version_id: options.dispatchPlan.id } : operational, execution_watermark: 0, execution_context_hash: 'execution-context', snapshot_token: `snapshot-${(options.dispatchPlan || activePlan).id}` }
      : effectiveUrl.endsWith('/api/scenarios/main/operational-view') ? operational
      : effectiveUrl.endsWith('/api/scenarios/strategy-medium') ? mediumScenario
      : effectiveUrl.endsWith('/cost-analysis') ? { ...context, scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', schedule_signature: 'selected', travel_model_fingerprint: 'travel', analysis_code_version: '0.5.3', analysis_input_hash: 'cost-input', analysis_horizon: { days, workdays_per_month: 22, currency: 'CNY' }, horizon_total_economic_impact_cents: breakdown.total_economic_impact_cents * days, policy: {}, policy_fingerprint: 'cost', assumptions: [], breakdown }
      : effectiveUrl.endsWith('/risk-simulation') ? { ...context, scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', schedule_signature: 'selected', travel_model_fingerprint: 'travel', execution_policy: 'FOLLOW_PUBLISHED_SCHEDULE', emergency_dispatch_policy: 'BETWEEN_VISITS_ONLY', emergency_responder_selection_policy: 'MYOPIC_EARLIEST_EMERGENCY_FINISH', emergency_location_policy: 'ALL_FROZEN_LOCATIONS_AS_SPATIAL_PROXY', emergency_location_work_order_ids: ['WO-1'], artifact_detail_policy: 'SUMMARY_ONLY', execution_policy_version: 'V2', simulation_policy_version: 'V6', analysis_code_version: '0.5.11', simulation_input_hash: 'risk', simulation_scenario_set_hash: 'scenario-set', seed: 7, trials: 500, expected_sla_on_time_rate: .875, published_commitment_sla_rate: .875, all_demand_sla_rate: .84, monte_carlo_mean_ci_low: .85, monte_carlo_mean_ci_high: .9, monte_carlo_interval_method: 'PERCENTILE_BOOTSTRAP_V1', sla_rate_ci_low: .85, sla_rate_ci_high: .9, full_day_total_late_minutes_p50: 10, full_day_total_late_minutes_p90: 28, full_day_total_late_minutes_p95: 35, scope_total_late_minutes_p50: 10, scope_total_late_minutes_p90: 28, scope_total_late_minutes_p95: 35, published_work_total_late_minutes_p50: 8, published_work_total_late_minutes_p90: 24, published_work_total_late_minutes_p95: 30, all_demand_total_late_minutes_p50: 10, all_demand_total_late_minutes_p90: 28, all_demand_total_late_minutes_p95: 35, emergency_late_minutes_mean: options.noEmergency ? null : 4, emergency_late_minutes_p50: options.noEmergency ? null : 2, emergency_late_minutes_p90: options.noEmergency ? null : 9, emergency_metric_sample_count: options.noEmergency ? 0 : 40, emergency_completed_sample_count: options.noEmergency ? 0 : 36, late_minutes_p50: 10, late_minutes_p90: 28, late_minutes_p95: 35, expected_overtime_minutes: 4.5, additional_disruption_probability: .125, absence_disruption_probability: .05, no_show_disruption_probability: .04, window_failure_probability: .03, overtime_failure_probability: .02, emergency_event_probability: options.noEmergency ? 0 : .08, emergency_event_count: options.noEmergency ? 0 : 40, emergency_caused_failure_probability: .06, emergency_failure_given_event_probability: options.noEmergency ? null : .75, emergency_caused_window_failure_probability: .03, emergency_caused_overtime_probability: .02, emergency_caused_unserved_probability: 0, emergency_caused_sla_degradation_probability: .04, emergency_capacity_disruption_probability: .06, emergency_completion_rate: options.noEmergency ? null : .9, emergency_on_time_rate: options.noEmergency ? null : .8, emergency_unserved_probability: options.noEmergency ? null : .1, emergency_incremental_late_minutes: options.noEmergency ? null : 4, emergency_incremental_overtime_minutes: options.noEmergency ? null : 2, emergency_incremental_unserved_orders: options.noEmergency ? null : .1, emergency_affected_work_order_count: options.noEmergency ? null : .2, emergency_disposition_changed_count: options.noEmergency ? null : .1, emergency_newly_unserved_count: options.noEmergency ? null : 0, emergency_newly_late_count: options.noEmergency ? null : .1, emergency_lateness_increased_count: options.noEmergency ? null : .1, baseline_unserved_orders: 0, expected_total_unserved_orders: .25, plan_failure_probability: .125, expected_unserved_orders: .25, assumptions: [] }
      : effectiveUrl.endsWith('/capacity-analysis') ? { ...context, scenario_id: 'main', plan_version_id: activePlan.id, plan_number: activePlan.number, scenario_snapshot_hash: 'test', analysis_code_version: '0.5.2', analysis_input_hash: 'capacity-input', evaluation_method: 'SELECTED_PLAN_TAIL_APPEND_COUNTERFACTUAL_V3', reference_mode: 'SELECTED_PLAN_DELTA', selected_plan_signature: 'selected', reference_schedule_signature: 'selected', reference_solver_policy_fingerprint: 'policy', reference_travel_model_fingerprint: 'travel', reference_kpis: schedule.kpis, cost_policy_fingerprint: 'cost', capacity_policy: {}, capacity_policy_fingerprint: 'capacity', analysis_horizon: { days, workdays_per_month: 22, currency: 'CNY' }, placement_mode: 'TAIL_APPEND_ONLY', base_schedule_signature: 'selected', base_cost: breakdown, options: [{ option_id: 'add_technician', name: '增加一名候选技师', assumption: '测算假设', option_applicable: true, decision_status: 'INTERNAL_VERIFIED', schedule_feasible: true, feasible: true, violations: [], changed_inputs: { skills: ['electrical'] }, placement_mode: 'TAIL_APPEND_ONLY', completion_rate: 1, sla_on_time_rate: 1, unassigned_count: 0, travel_minutes: 8, overtime_minutes: 0, completion_improvement_percentage_points: 5, sla_improvement_percentage_points: 8, unassigned_delta: -1, travel_delta_minutes: -2, overtime_delta_minutes: 0, fixed_capacity_cost_cents: 60000, fixed_cost_cadence: 'PER_DAY', cost_unit_type: 'PLAN_DAY', cost_units_per_day: 1, affected_entity_ids: ['TECH-01'], one_time_investment_cents: 0, daily_operating_delta_cents: -10000, horizon_total_impact_cents: 50000 * days, economic_impact_offset_days: null, cash_payback_days: null, break_even_days: null, marginal_cost_cents: 50000, projected_total_cost_cents: 173400, schedule_signature: 'capacity', diagnostic_metrics: {}, conditional_upper_bound_kpis: null, diagnostic_schedule: null, verification_report: null, route_diff: [], artifact_id: 'DAA-1' }] }
      : effectiveUrl.endsWith('/analysis-runs') ? options.existingFailed ? [{ id: 'AN-FAILED', scenario_id: 'main', number: 4, plan_version_id: activePlan.id, plan_number: activePlan.number, analysis_type: 'COST', status: 'FAILED', error: { message: '成本引擎中断' }, logical_analysis_id: 'AN-FAILED' }] : []
      : effectiveUrl.endsWith('/plan-versions') ? [activePlan]
      : effectiveUrl.includes('/plan-versions/') ? activePlan
      : effectiveUrl.endsWith('/schedules') ? [schedule]
      : effectiveUrl.endsWith('/baseline') ? { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline' }
      : scenario
    if (analysisRequest && isAnalysisJob) {
      const failed = analysisRequest.analysis_type === 'RISK' && options.failRisk
      body = { id: `JOB-${analysisRequest.analysis_type}`, job_type: `${analysisRequest.analysis_type}_ANALYSIS`, scenario_id: 'main', status: failed ? 'FAILED' : 'COMPLETED', progress: 100, input_payload: {}, input_manifest_hash: 'job-input', dedupe_key: 'test', lease_owner: null, lease_expires_at: null, heartbeat_at: null, attempt_number: 1, result_resource_type: failed ? null : 'decision_analysis', result_resource_id: failed ? null : `AN-${analysisRequest.analysis_type}`, error: failed ? { message: '风险引擎暂不可用' } : null, created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-24T00:00:01Z', finished_at: '2026-08-24T00:00:01Z' }
    } else if (analysisRequest) body = { id: `AN-${analysisRequest.analysis_type}`, scenario_id: 'main', number: { COST: 1, RISK: 2, CAPACITY: 3 }[analysisRequest.analysis_type], plan_version_id: activePlan.id, plan_number: activePlan.number, analysis_type: analysisRequest.analysis_type, ...context, active_booking_ids: [], scenario_snapshot_hash: 'test', schedule_hash: 'selected', travel_model_fingerprint: 'travel', policy_version: 'V3', policy_snapshot: {}, code_version: '0.5.3', input_hash: `${analysisRequest.analysis_type}-input`, request_snapshot: analysisRequest, logical_analysis_id: `AN-${analysisRequest.analysis_type}`, retry_of_analysis_id: null, attempt_number: 1, status: 'COMPLETED', result: body, error: null, self_integrity: 'VERIFIED', parent_plan_integrity: 'VERIFIED', effective_integrity: 'VERIFIED', created_at: '2026-08-24T00:00:00Z', finished_at: '2026-08-24T00:00:01Z' }
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

  it('keeps current uncovered demand visible in risk queue, map, and KPI', async () => {
    mockApi(); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    expect(await screen.findByText('0 / 1 单可执行 · 0 单分配失效')).toBeInTheDocument()
    expect(screen.getByText('新增未纳入当前方案')).toBeInTheDocument()
    expect(screen.getByText('当前可执行覆盖率')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看工单 WO-1' })).toHaveClass('risk')
  })

  it('rejects an operational snapshot from a different active plan at the same revision', async () => {
    const newerPlan: PlanVersion = { ...plan, id: 'PV-NEWER', number: 2, selected: { ...schedule, id: 'SCH-NEWER', version: 2 } }
    mockApi(plan, { dispatchPlan: newerPlan }); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    await waitFor(() => expect(screen.getByText('正在核对当前业务状态')).toBeInTheDocument())
    expect(screen.queryByText('0 / 1 单可执行 · 0 单分配失效')).not.toBeInTheDocument()
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
    expect(screen.getByRole('combobox', { name: '最晚开始时间日期' })).toHaveValue('1')
    expect(screen.getByLabelText('最晚开始时间', { selector: 'input' })).toHaveValue('01:00')
    expect(screen.getByLabelText('SLA 截止', { selector: 'input' })).toHaveValue('01:30')
  })

  it('filters plan history by action, strategy, and solver status', () => {
    const baselinePlan: PlanVersion = {
      ...plan, id: 'PV-BASE', number: 1, action: 'baseline', label: '人工基线', active: false,
      selected: { ...schedule, id: 'SCH-BASE', kind: 'baseline', version: 1, strategy: 'baseline', solver_status: 'OPTIMAL' },
    }
    const optimizedPlan: PlanVersion = { ...plan, id: 'PV-OPT', number: 2 }
    const callbacks = {
      onOpen: () => undefined, onActivate: () => undefined, onReattest: () => undefined, onClone: () => undefined,
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

  it('disables starting an invalidated assignment until replanning', async () => {
    const validSchedule: Schedule = {
      ...schedule,
      assignments: [{ ...schedule.assignments[0], work_order_id: 'WO-1' }],
    }
    const invalidPlan: PlanVersion = {
      ...plan,
      selected: validSchedule,
      applicability: { ...plan.applicability, route_executable: false, invalid_assignment_ids: ['WO-1'] },
    }
    mockApi(invalidPlan); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    await waitFor(() => expect(screen.getByText('0 / 1 单可执行 · 1 单分配失效')).toBeInTheDocument())
    expect(screen.getByText('分配已失效，需重排').closest('button')).toHaveClass('invalid')
    expect(screen.getByRole('button', { name: '查看工单 WO-1' })).toHaveClass('risk')
    expect(screen.getByTitle(/WO-1.*分配已失效/)).toHaveClass('invalid')
    fireEvent.click(screen.getByRole('button', { name: '全部' }))
    fireEvent.click(screen.getByText('WO-1'))
    expect(screen.getByText(/开始服务前必须局部重排/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '分配已失效' })).toBeDisabled()
  })

  it('reads analyses without writing and creates explicit frozen analyses on demand', async () => {
    mockApi(); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    expect(await screen.findByText(/当前版本还没有经营分析/)).toBeInTheDocument()
    expect(screen.getByText('完整冻结计划范围')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).endsWith('/analysis-jobs') && init?.method === 'POST')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: '生成成本与风险分析' }))
    expect(await screen.findByText(/1,234\.00/)).toBeInTheDocument()
    expect(screen.getByText('88%')).toBeInTheDocument()
    expect(screen.getByText('35 分钟')).toBeInTheDocument()
    const analysisBodies = vi.mocked(fetch).mock.calls.filter(([input, init]) => String(input).endsWith('/analysis-jobs') && init?.method === 'POST').map(([, init]) => JSON.parse(String(init?.body)) as { analysis_scope?: string })
    expect(analysisBodies).toHaveLength(2)
    expect(analysisBodies.every(body => body.analysis_scope === undefined)).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '测算六种容量方案' }))
    expect(await screen.findByText('增加一名候选技师')).toBeInTheDocument()
    expect(screen.getByText('+5pp')).toBeInTheDocument()
  })

  it('keeps the successful cost result when risk analysis fails', async () => {
    mockApi(plan, { failRisk: true }); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    await screen.findByText(/当前版本还没有经营分析/)
    fireEvent.click(screen.getByRole('button', { name: '生成成本与风险分析' }))
    expect(await screen.findByText(/1,234\.00/)).toBeInTheDocument()
    expect(screen.getByText('风险引擎暂不可用')).toBeInTheDocument()
    expect(screen.queryByText('88%')).not.toBeInTheDocument()
  })

  it('shows a completed cost result without waiting for the risk simulation', async () => {
    let releaseRisk: (() => void) | undefined
    const riskGate = new Promise<void>(resolve => { releaseRisk = resolve })
    mockApi(plan, { riskGate }); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    await screen.findByText(/当前版本还没有经营分析/)
    fireEvent.click(screen.getByRole('button', { name: '生成成本与风险分析' }))
    expect(await screen.findByText(/1,234\.00/)).toBeInTheDocument()
    expect(screen.queryByText('88%')).not.toBeInTheDocument()
    releaseRisk?.()
    expect(await screen.findByText('88%')).toBeInTheDocument()
  })

  it('labels overtime base wage and premium as separate cost components', async () => {
    mockApi(); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    await screen.findByText(/当前版本还没有经营分析/)
    fireEvent.click(screen.getByRole('button', { name: '生成成本与风险分析' }))
    expect(await screen.findByText('正常人工')).toBeInTheDocument()
    expect(screen.getByText('加班基础工资')).toBeInTheDocument()
    expect(screen.getByText('加班溢价')).toBeInTheDocument()
  })

  it('renders zero emergency observations as not applicable', async () => {
    mockApi(plan, { noEmergency: true }); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    await screen.findByText(/当前版本还没有经营分析/)
    fireEvent.click(screen.getByRole('button', { name: '生成成本与风险分析' }))
    expect(await screen.findByText(/紧急指标不适用：本次模拟未发生紧急事件/)).toBeInTheDocument()
  })

  it('offers an explicit retry for a failed analysis record', async () => {
    mockApi(plan, { existingFailed: true }); render(<App />)
    await screen.findByRole('heading', { name: '今日调度测试' })
    fireEvent.click(screen.getByRole('button', { name: '运营复盘' }))
    expect(await screen.findByRole('button', { name: '重试 A004' })).toBeInTheDocument()
    expect(screen.getByText(/原记录会保留/)).toBeInTheDocument()
  })
})
