import type {
  CapacityAnalysis, CapacityCounterfactualArtifact, Comparison, CostAnalysis, DecisionAnalysisRun, PlanVersion, RiskSimulation, RollbackPreview, Scenario, Schedule, Strategy, StrategyExperiment,
  ExecutionEvent, ExecutionResult, ManualReassignmentResult, StrategyProfile, StrategyWeights, Technician, WorkOrder,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  const controller = new AbortController()
  const relayAbort = () => controller.abort()
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
      throw new Error('请求超过 35 秒未完成。求解仍可能在后台结束，请查看方案历史后再重试。')
    }
    if (init?.signal?.aborted) throw new Error('请求已取消')
    throw new Error('无法连接 FieldFlow 本地服务。请在项目目录运行“make demo”，并确认终端显示的访问地址与当前地址一致。')
  } finally {
    window.clearTimeout(timeout)
    init?.signal?.removeEventListener('abort', relayAbort)
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try { const body = await response.json(); message = body.detail?.message || body.detail || message } catch { /* noop */ }
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  scenarios: () => request<Scenario[]>('/api/scenarios'),
  scenario: (id: string) => request<Scenario>(`/api/scenarios/${id}`),
  resetScenario: (id: string) => request<Scenario>(`/api/scenarios/${id}/reset`, { method: 'POST' }),
  schedules: (id: string) => request<Schedule[]>(`/api/scenarios/${id}/schedules`),
  planVersions: (id: string) => request<PlanVersion[]>(`/api/scenarios/${id}/plan-versions`),
  planVersion: (id: string, versionId: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}`),
  renamePlanVersion: (id: string, versionId: string, label: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}`, { method: 'PATCH', body: JSON.stringify({ label }) }),
  rollbackPreview: (id: string, versionId: string) => request<RollbackPreview>(`/api/scenarios/${id}/plan-versions/${versionId}/rollback-preview`),
  rollbackPlanVersion: (id: string, versionId: string, expectedRevision: number, confirmationToken: string, reason: string, idempotencyKey: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}/restore`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, confirmation_token: confirmationToken, reason, idempotency_key: idempotencyKey }) }),
  activatePlanVersion: (id: string, versionId: string, expectedRevision: number, idempotencyKey: string) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}/activate`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotencyKey }) }),
  clonePlanScenario: (id: string, versionId: string, name: string, idempotencyKey: string) => request<Scenario>(`/api/scenarios/${id}/plan-versions/${versionId}/clone-scenario`, { method: 'POST', body: JSON.stringify({ name, idempotency_key: idempotencyKey }) }),
  baseline: (id: string, idempotencyKey?: string) => request<Schedule>(`/api/scenarios/${id}/baseline`, { method: 'POST', headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined }),
  optimize: (id: string, strategy: Strategy = 'balanced', profileId?: string, idempotencyKey?: string) => request<Schedule>(`/api/scenarios/${id}/optimize`, { method: 'POST', headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined, body: JSON.stringify({ strategy, profile_id: profileId }) }),
  replan: (id: string, currentTime = 600, strategy: Strategy | 'stable' = 'stable', emergencyOrder?: WorkOrder, idempotencyKey?: string) => request<Schedule>(`/api/scenarios/${id}/replan`, { method: 'POST', body: JSON.stringify({ current_time: currentTime, planning_time: currentTime, strategy, emergency_order: emergencyOrder, idempotency_key: idempotencyKey }) }),
  lock: (scenarioId: string, orderId: string, technicianId: string, locked: boolean) =>
    request<Scenario>(`/api/scenarios/${scenarioId}/lock`, {
      method: 'POST', body: JSON.stringify({ work_order_id: orderId, technician_id: technicianId, locked }),
    }),
  manualReassignment: (scenarioId: string, orderId: string, technicianId: string, planningTime: number, expectedRevision: number, idempotencyKey: string) =>
    request<ManualReassignmentResult>(`/api/scenarios/${scenarioId}/manual-reassignment`, {
      method: 'POST', body: JSON.stringify({ work_order_id: orderId, technician_id: technicianId, planning_time: planningTime, expected_revision: expectedRevision, idempotency_key: idempotencyKey }),
    }),
  comparison: (id: string, before?: string, after?: string) => {
    const query = new URLSearchParams()
    if (before) query.set('before', before)
    if (after) query.set('after', after)
    return request<Comparison>(`/api/scenarios/${id}/comparison${query.size ? `?${query}` : ''}`)
  },
  strategyProfiles: () => request<StrategyProfile[]>('/api/strategy-profiles'),
  createStrategyProfile: (profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>('/api/strategy-profiles', { method: 'POST', body: JSON.stringify(profile) }),
  updateStrategyProfile: (id: string, profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>(`/api/strategy-profiles/${id}`, { method: 'PUT', body: JSON.stringify(profile) }),
  deleteStrategyProfile: (id: string) => request<void>(`/api/strategy-profiles/${id}`, { method: 'DELETE' }),
  createExperiment: (id: string, profileIds: string[], timeLimit?: number) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments`, { method: 'POST', body: JSON.stringify({ dataset: 'current', profile_ids: profileIds, time_limit_seconds: timeLimit }) }),
  experiment: (id: string, experimentId: string, signal?: AbortSignal) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments/${experimentId}`, { signal }),
  cancelExperiment: (id: string, experimentId: string) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments/${experimentId}/cancel`, { method: 'POST' }),
  publishExperiment: (id: string, experimentId: string, candidateId: string, expectedRevision: number) => request<PlanVersion>(`/api/scenarios/${id}/strategy-experiments/${experimentId}/publish`, { method: 'POST', body: JSON.stringify({ candidate_id: candidateId, expected_revision: expectedRevision }) }),
  createWorkOrder: (scenarioId: string, order: WorkOrder) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders`, { method: 'POST', body: JSON.stringify(order) }),
  updateWorkOrder: (scenarioId: string, orderId: string, order: Partial<WorkOrder>) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'PUT', body: JSON.stringify(order) }),
  deleteWorkOrder: (scenarioId: string, orderId: string) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'DELETE' }),
  executeWorkOrder: (scenarioId: string, orderId: string, action: 'start' | 'complete', technicianId: string, occurredAt: number, expectedRevision: number, idempotencyKey: string, options?: { earlyStartOverrideReason?: string; estimatedRemainingMinutes?: number; note?: string }) => request<ExecutionResult>(`/api/scenarios/${scenarioId}/work-orders/${orderId}/${action}`, { method: 'POST', body: JSON.stringify({ technician_id: technicianId, occurred_at: occurredAt, expected_revision: expectedRevision, idempotency_key: idempotencyKey, early_start_override_reason: options?.earlyStartOverrideReason || null, estimated_remaining_minutes: action === 'start' ? options?.estimatedRemainingMinutes ?? null : null, note: options?.note || '' }) }),
  executionEvents: (scenarioId: string) => request<ExecutionEvent[]>(`/api/scenarios/${scenarioId}/execution-events`),
  analysisRuns: (scenarioId: string, versionId: string) => request<DecisionAnalysisRun[]>(`/api/scenarios/${scenarioId}/plan-versions/${versionId}/analysis-runs`),
  createDecisionAnalysisRun: <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, versionId: string, analysisType: 'COST' | 'CAPACITY' | 'RISK', options?: { referenceMode?: 'SELECTED_PLAN_DELTA' | 'CONTROLLED_REOPTIMIZATION'; seed?: number; trials?: number; horizonDays?: number }) => {
    const horizon = { days: options?.horizonDays ?? 1, workdays_per_month: 22, currency: 'CNY' }
    const parameters = analysisType === 'COST'
      ? { analysis_horizon: horizon }
      : analysisType === 'CAPACITY'
        ? { reference_mode: options?.referenceMode || 'SELECTED_PLAN_DELTA', analysis_horizon: horizon }
        : { seed: options?.seed ?? null, trials: options?.trials ?? 500 }
    return request<DecisionAnalysisRun<T>>(`/api/scenarios/${scenarioId}/plan-versions/${versionId}/analysis-runs`, { method: 'POST', body: JSON.stringify({ analysis_type: analysisType, analysis_scope: 'EX_ANTE_FROZEN_PLAN', request: parameters }) })
  },
  retryDecisionAnalysisRun: <T extends CostAnalysis | CapacityAnalysis | RiskSimulation>(scenarioId: string, analysisId: string) => request<DecisionAnalysisRun<T>>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}/retry`, { method: 'POST' }),
  decisionAnalysisArtifacts: (scenarioId: string, analysisId: string) => request<CapacityCounterfactualArtifact[]>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}/artifacts`),
  decisionAnalysisArtifact: (scenarioId: string, analysisId: string, artifactId: string) => request<CapacityCounterfactualArtifact>(`/api/scenarios/${scenarioId}/analysis-runs/${analysisId}/artifacts/${artifactId}`),
  createTechnician: (scenarioId: string, technician: Technician) => request<Scenario>(`/api/scenarios/${scenarioId}/technicians`, { method: 'POST', body: JSON.stringify(technician) }),
  updateTechnician: (scenarioId: string, technicianId: string, technician: Partial<Technician>) => request<Scenario>(`/api/scenarios/${scenarioId}/technicians/${technicianId}`, { method: 'PUT', body: JSON.stringify(technician) }),
}
