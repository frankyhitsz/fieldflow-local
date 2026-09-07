import type {
  CapacityAnalysis, Comparison, CostAnalysis, DecisionAnalysisRun, PlanVersion, RiskSimulation, RollbackPreview, Scenario, Schedule, Strategy, StrategyExperiment,
  DecisionAnalysisArtifact, DispatchSnapshot, ExecutionEvent, ExecutionResult, ManualReassignmentResult, OperationalView, StrategyProfile, StrategyWeights, Technician, WorkOrder,
  RuntimeJob,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly details?: Record<string, unknown>,
  ) { super(message); this.name = 'ApiError' }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  const controller = new AbortController()
  const relayAbort = () => controller.abort()
  if (init?.signal?.aborted) controller.abort()
  init?.signal?.addEventListener('abort', relayAbort, { once: true })
  const timeout = window.setTimeout(() => controller.abort(), 35_000)
  try {
    response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    })
  } catch (error) {
    if (controller.signal.aborted && !init?.signal?.aborted) {
      throw new Error('请求超过 35 秒未完成。求解仍可能在后台结束，请查看方案历史后再重试。', { cause: error })
    }
    if (init?.signal?.aborted) throw new Error('请求已取消', { cause: error })
    throw new Error('无法连接 FieldFlow 本地服务。请在项目目录运行“make demo”，并确认终端显示的访问地址与当前地址一致。', { cause: error })
  } finally {
    window.clearTimeout(timeout)
    init?.signal?.removeEventListener('abort', relayAbort)
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    let code: string | undefined
    let details: Record<string, unknown> | undefined
    try {
      const body = await response.json()
      const detail = body.detail
      if (detail && typeof detail === 'object') {
        details = detail as Record<string, unknown>
        code = typeof details.code === 'string' ? details.code : undefined
        message = typeof details.message === 'string' ? details.message : message
      } else if (typeof detail === 'string') message = detail
    } catch { /* noop */ }
    if (details?.refresh_required || (code && ['SCENARIO_REVISION_CONFLICT', 'SCENARIO_CHANGED_DURING_COMMAND', 'ACTIVE_PLAN_CHANGED', 'ACTIVE_PLAN_CHANGED_DURING_COMMAND', 'ACTIVE_PLAN_VERSION_CONFLICT'].includes(code))) {
      window.dispatchEvent(new Event('fieldflow-dispatch-stale'))
    }
    throw new ApiError(message, response.status, code, details)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function rememberJob(job: RuntimeJob): void {
  // The server list is authoritative; this hint also survives a reload between
  // submission and the first poll. Storage may be unavailable in private mode.
  try { localStorage.setItem(`fieldflow-job:${job.id}`, JSON.stringify({ id: job.id, scenarioId: job.scenario_id })) } catch { /* server list remains available */ }
  window.dispatchEvent(new Event('fieldflow-jobs-changed'))
}

function pollDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('停止等待；后台任务仍保留', 'AbortError')) }
    const timer = window.setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, milliseconds)
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) abort()
  })
}

async function waitForAnalysis<T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(job: RuntimeJob, signal?: AbortSignal): Promise<DecisionAnalysisRun<T>> {
  rememberJob(job)
  let current = job
  for (let attempt = 0; attempt < 240; attempt += 1) {
    if (signal?.aborted) throw new DOMException('停止等待；后台任务仍保留', 'AbortError')
    if (current.status === 'COMPLETED' && current.result_resource_id) {
      return request<DecisionAnalysisRun<T>>(`/api/scenarios/${job.scenario_id}/analysis-runs/${current.result_resource_id}`, { signal })
    }
    if (current.status === 'FAILED' || current.status === 'CANCELLED') {
      throw new ApiError(typeof current.error?.message === 'string' ? current.error.message : '任务未完成，请在任务中心查看', 409, current.status, { job_id: current.id })
    }
    await pollDelay(Math.min(250 * 2 ** Math.min(attempt, 4), 4000), signal)
    current = await request<RuntimeJob>(`/api/v2/scenarios/${job.scenario_id}/jobs/${job.id}`, { signal })
  }
  throw new Error('任务仍保留在后台，请在任务中心继续查看。')
}

export const api = {
  jobs: (scenarioId: string, signal?: AbortSignal, offset = 0) => request<RuntimeJob[]>(`/api/v2/scenarios/${scenarioId}/jobs?limit=100&offset=${offset}`, { signal }),
  cancelJob: (scenarioId: string, id: string) => request<RuntimeJob>(`/api/v2/scenarios/${scenarioId}/jobs/${id}/cancel`, { method: 'POST' }),
  scenarios: () => request<Scenario[]>('/api/scenarios'),
  scenario: (id: string) => request<Scenario>(`/api/scenarios/${id}`),
  operationalView: (id: string) => request<OperationalView>(`/api/scenarios/${id}/operational-view`),
  dispatchSnapshot: (id: string, signal?: AbortSignal) => request<DispatchSnapshot>(`/api/scenarios/${id}/dispatch-snapshot`, { signal }),
  resetScenario: (id: string, expectedRevision: number) => request<Scenario>(`/api/v2/scenarios/${id}/reset`, { method: 'POST', headers: { 'If-Match': `D${expectedRevision}` } }),
  schedules: (id: string) => request<Schedule[]>(`/api/scenarios/${id}/schedules`),
  planVersions: (id: string) => request<PlanVersion[]>(`/api/scenarios/${id}/plan-versions`),
  olderPlanVersions: (id: string, offset: number) => request<PlanVersion[]>(`/api/scenarios/${id}/plan-versions?limit=100&offset=${offset}`),
  planVersion: (id: string, versionId: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}`),
  renamePlanVersion: (id: string, versionId: string, label: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}`, { method: 'PATCH', body: JSON.stringify({ label }) }),
  rollbackPreview: (id: string, versionId: string) => request<RollbackPreview>(`/api/scenarios/${id}/plan-versions/${versionId}/rollback-preview`),
  rollbackPlanVersion: (id: string, versionId: string, expectedRevision: number, confirmationToken: string, reason: string, idempotencyKey: string, expectedActivePlanId: string | null) => request<PlanVersion>(`/api/v2/scenarios/${id}/plan-versions/${versionId}/restore`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, expected_active_plan_version_id: expectedActivePlanId, confirmation_token: confirmationToken, reason, idempotency_key: idempotencyKey }) }),
  activatePlanVersion: (id: string, versionId: string, expectedRevision: number, idempotencyKey: string, expectedActivePlanId: string | null) => request<PlanVersion>(`/api/v2/scenarios/${id}/plan-versions/${versionId}/activate`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, expected_active_plan_version_id: expectedActivePlanId, idempotency_key: idempotencyKey }) }),
  reattestPlanVersion: (id: string, versionId: string, expectedRevision: number, idempotencyKey: string, mode: 'EXACT_SNAPSHOT' | 'PLANNING_EQUIVALENT', expectedActivePlanId: string | null) => request<PlanVersion>(`/api/v2/scenarios/${id}/plan-versions/${versionId}/reattest`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, expected_active_plan_version_id: expectedActivePlanId, idempotency_key: idempotencyKey, mode }) }),
  clonePlanScenario: (id: string, versionId: string, name: string, idempotencyKey: string) => request<Scenario>(`/api/scenarios/${id}/plan-versions/${versionId}/clone-scenario`, { method: 'POST', body: JSON.stringify({ name, idempotency_key: idempotencyKey }) }),
  baseline: (id: string, expectedRevision: number, expectedActivePlanId: string | null, idempotencyKey: string) => request<Schedule>(`/api/v2/scenarios/${id}/baseline`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey, 'If-Match': `D${expectedRevision}` }, body: JSON.stringify({ expected_active_plan_version_id: expectedActivePlanId }) }),
  optimize: (id: string, strategy: Strategy, profileId: string | undefined, idempotencyKey: string, expectedActivePlanId: string | null, expectedRevision: number) => request<Schedule>(`/api/v2/scenarios/${id}/optimize`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey, 'If-Match': `D${expectedRevision}` }, body: JSON.stringify({ strategy, profile_id: profileId, expected_active_plan_version_id: expectedActivePlanId }) }),
  replan: (id: string, currentTime: number, strategy: Strategy | 'stable', emergencyOrder: WorkOrder | undefined, idempotencyKey: string, expectedActivePlanId: string | null, expectedRevision: number) => {
    const emergencyPayload = emergencyOrder ? Object.fromEntries(Object.entries(emergencyOrder).filter(([key]) => key !== 'status')) : undefined
    return request<Schedule>(`/api/v2/scenarios/${id}/replan`, { method: 'POST', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify({ current_time: currentTime, planning_time: currentTime, strategy, emergency_order: emergencyPayload, idempotency_key: idempotencyKey, expected_active_plan_version_id: expectedActivePlanId }) })
  },
  lock: (scenarioId: string, orderId: string, technicianId: string, locked: boolean, expectedRevision: number) =>
    request<Scenario>(`/api/v2/scenarios/${scenarioId}/lock`, {
      method: 'POST', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify({ work_order_id: orderId, technician_id: technicianId, locked }),
    }),
  manualReassignment: (scenarioId: string, orderId: string, technicianId: string, planningTime: number, expectedRevision: number, idempotencyKey: string, expectedActivePlanId: string | null) =>
    request<ManualReassignmentResult>(`/api/scenarios/${scenarioId}/manual-reassignment`, {
      method: 'POST', body: JSON.stringify({ work_order_id: orderId, technician_id: technicianId, planning_time: planningTime, expected_revision: expectedRevision, expected_active_plan_version_id: expectedActivePlanId, idempotency_key: idempotencyKey }),
    }),
  comparison: (id: string, before?: string, after?: string, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (before) query.set('before', before)
    if (after) query.set('after', after)
    return request<Comparison>(`/api/scenarios/${id}/comparison${query.size ? `?${query}` : ''}`, { signal })
  },
  strategyProfiles: () => request<StrategyProfile[]>('/api/strategy-profiles'),
  createStrategyProfile: (profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>('/api/strategy-profiles', { method: 'POST', body: JSON.stringify(profile) }),
  updateStrategyProfile: (id: string, profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>(`/api/strategy-profiles/${id}`, { method: 'PUT', body: JSON.stringify(profile) }),
  deleteStrategyProfile: (id: string) => request<void>(`/api/strategy-profiles/${id}`, { method: 'DELETE' }),
  createExperiment: (id: string, profileIds: string[], timeLimit?: number) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments`, { method: 'POST', body: JSON.stringify({ dataset: 'current', profile_ids: profileIds, time_limit_seconds: timeLimit }) }),
  experiment: (id: string, experimentId: string, signal?: AbortSignal) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments/${experimentId}`, { signal }),
  cancelExperiment: (id: string, experimentId: string) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments/${experimentId}/cancel`, { method: 'POST' }),
  publishExperiment: (id: string, experimentId: string, candidateId: string, expectedRevision: number, expectedActivePlanId: string | null) => request<PlanVersion>(`/api/v2/scenarios/${id}/strategy-experiments/${experimentId}/publish`, { method: 'POST', body: JSON.stringify({ candidate_id: candidateId, expected_revision: expectedRevision, expected_active_plan_version_id: expectedActivePlanId }) }),
  createWorkOrder: (scenarioId: string, order: WorkOrder, expectedRevision: number) => request<Scenario>(`/api/v2/scenarios/${scenarioId}/work-orders`, { method: 'POST', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify(Object.fromEntries(Object.entries(order).filter(([key]) => key !== 'status'))) }),
  updateWorkOrder: (scenarioId: string, orderId: string, order: Partial<WorkOrder>, expectedRevision: number) => {
    const payload = Object.fromEntries(Object.entries(order).filter(([key]) => key !== 'id' && key !== 'status'))
    return request<Scenario>(`/api/v2/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'PUT', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify(payload) })
  },
  deleteWorkOrder: (scenarioId: string, orderId: string, expectedRevision: number) => request<Scenario>(`/api/v2/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'DELETE', headers: { 'If-Match': `D${expectedRevision}` } }),
  cancelEmergencyIntake: (scenarioId: string, orderId: string, expectedRevision: number, idempotencyKey = `emergency-cancel-${crypto.randomUUID()}`) => request<Scenario>(`/api/v2/scenarios/${scenarioId}/emergency-intakes/${orderId}/cancel`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotencyKey }) }),
  executeWorkOrder: (scenarioId: string, orderId: string, action: 'start' | 'complete', technicianId: string, occurredAt: number, expectedRevision: number, idempotencyKey: string, options?: { earlyStartOverrideReason?: string; estimatedRemainingMinutes?: number; note?: string }) => request<ExecutionResult>(`/api/scenarios/${scenarioId}/work-orders/${orderId}/${action}`, { method: 'POST', body: JSON.stringify({ technician_id: technicianId, occurred_at: occurredAt, expected_revision: expectedRevision, idempotency_key: idempotencyKey, early_start_override_reason: options?.earlyStartOverrideReason || null, estimated_remaining_minutes: action === 'start' ? options?.estimatedRemainingMinutes ?? null : null, note: options?.note || '' }) }),
  executionEvents: (scenarioId: string) => request<ExecutionEvent[]>(`/api/scenarios/${scenarioId}/execution-events`),
  analysisRuns: (scenarioId: string, versionId: string, offset = 0) => request<DecisionAnalysisRun[]>(`/api/scenarios/${scenarioId}/plan-versions/${versionId}/analysis-runs${offset ? `?limit=100&offset=${offset}` : ''}`),
  createDecisionAnalysisRun: async <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, versionId: string, analysisType: 'COST' | 'CAPACITY' | 'RISK', options?: { referenceMode?: 'SELECTED_PLAN_DELTA' | 'CONTROLLED_REOPTIMIZATION'; seed?: number; trials?: number; horizonDays?: number; emergencyLocationPolicy?: RiskSimulation['emergency_location_policy']; artifactDetailPolicy?: RiskSimulation['artifact_detail_policy']; signal?: AbortSignal }): Promise<DecisionAnalysisRun<T>> => {
    const horizon = { days: options?.horizonDays ?? 1, workdays_per_month: 22, currency: 'CNY' }
    const parameters = analysisType === 'COST'
      ? { analysis_horizon: horizon }
      : analysisType === 'CAPACITY'
        ? { reference_mode: options?.referenceMode || 'SELECTED_PLAN_DELTA', analysis_horizon: horizon }
        : { seed: options?.seed ?? null, trials: options?.trials ?? 500, emergency_location_policy: options?.emergencyLocationPolicy ?? 'ALL_FROZEN_LOCATIONS_AS_SPATIAL_PROXY', artifact_detail_policy: options?.artifactDetailPolicy ?? 'SUMMARY_ONLY' }
    const job = await request<RuntimeJob>(`/api/v2/scenarios/${scenarioId}/plan-versions/${versionId}/analysis-jobs`, {
      method: 'POST',
      headers: { 'Idempotency-Key': `analysis-${crypto.randomUUID()}` },
      body: JSON.stringify({ analysis_type: analysisType, request: parameters }),
    })
    return waitForAnalysis<T>(job, options?.signal)
  },
  decisionAnalysisRun: <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, analysisId: string) => request<DecisionAnalysisRun<T>>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}`),
  retryDecisionAnalysisRun: async <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, analysisId: string, idempotencyKey = `analysis-retry-${crypto.randomUUID()}`, signal?: AbortSignal) => waitForAnalysis<T>(await request<RuntimeJob>(`/api/v2/scenarios/${scenarioId}/analysis-runs/${analysisId}/retry-jobs`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }), signal),
  rerunDecisionAnalysisCurrent: async <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, analysisId: string, idempotencyKey = `analysis-rerun-${crypto.randomUUID()}`, signal?: AbortSignal) => waitForAnalysis<T>(await request<RuntimeJob>(`/api/v2/scenarios/${scenarioId}/analysis-runs/${analysisId}/rerun-current-jobs`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }), signal),
  decisionAnalysisArtifacts: (scenarioId: string, analysisId: string) => request<DecisionAnalysisArtifact[]>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}/artifacts`),
  decisionAnalysisArtifact: (scenarioId: string, analysisId: string, artifactId: string) => request<DecisionAnalysisArtifact>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}/artifacts/${artifactId}`),
  createTechnician: (scenarioId: string, technician: Technician, expectedRevision: number) => request<Scenario>(`/api/v2/scenarios/${scenarioId}/technicians`, { method: 'POST', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify(technician) }),
  updateTechnician: (scenarioId: string, technicianId: string, technician: Partial<Technician>, expectedRevision: number) => request<Scenario>(`/api/v2/scenarios/${scenarioId}/technicians/${technicianId}`, { method: 'PUT', headers: { 'If-Match': `D${expectedRevision}` }, body: JSON.stringify(technician) }),
}
