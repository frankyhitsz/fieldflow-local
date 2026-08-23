import type {
  Comparison, PlanVersion, Scenario, Schedule, Strategy, StrategyExperiment,
  StrategyProfile, StrategyWeights, Technician, WorkOrder,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    })
  } catch {
    throw new Error('无法连接 FieldFlow 本地服务。请在项目目录运行“make demo”，并确认终端显示的访问地址与当前地址一致。')
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
  restorePlanVersion: (id: string, versionId: string, expectedRevision: number) => request<PlanVersion>(`/api/scenarios/${id}/plan-versions/${versionId}/restore`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision }) }),
  baseline: (id: string) => request<Schedule>(`/api/scenarios/${id}/baseline`, { method: 'POST' }),
  optimize: (id: string, strategy: Strategy = 'balanced', profileId?: string) => request<Schedule>(`/api/scenarios/${id}/optimize`, { method: 'POST', body: JSON.stringify({ strategy, profile_id: profileId }) }),
  replan: (id: string, currentTime = 600, strategy: Strategy | 'stable' = 'stable') => request<Schedule>(`/api/scenarios/${id}/replan`, { method: 'POST', body: JSON.stringify({ current_time: currentTime, strategy }) }),
  lock: (scenarioId: string, orderId: string, technicianId: string, locked: boolean) =>
    request<Scenario>(`/api/scenarios/${scenarioId}/lock`, {
      method: 'POST', body: JSON.stringify({ work_order_id: orderId, technician_id: technicianId, locked }),
    }),
  comparison: (id: string, before?: string, after?: string) => request<Comparison>(`/api/scenarios/${id}/comparison${before && after ? `?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}` : ''}`),
  strategyProfiles: () => request<StrategyProfile[]>('/api/strategy-profiles'),
  createStrategyProfile: (profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>('/api/strategy-profiles', { method: 'POST', body: JSON.stringify(profile) }),
  updateStrategyProfile: (id: string, profile: { name: string; description: string; weights: StrategyWeights; time_limit_seconds: number }) => request<StrategyProfile>(`/api/strategy-profiles/${id}`, { method: 'PUT', body: JSON.stringify(profile) }),
  deleteStrategyProfile: (id: string) => request<void>(`/api/strategy-profiles/${id}`, { method: 'DELETE' }),
  createExperiment: (id: string, profileIds: string[], timeLimit?: number) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments`, { method: 'POST', body: JSON.stringify({ dataset: 'current', profile_ids: profileIds, time_limit_seconds: timeLimit }) }),
  experiment: (id: string, experimentId: string) => request<StrategyExperiment>(`/api/scenarios/${id}/strategy-experiments/${experimentId}`),
  publishExperiment: (id: string, experimentId: string, candidateId: string, expectedRevision: number) => request<PlanVersion>(`/api/scenarios/${id}/strategy-experiments/${experimentId}/publish`, { method: 'POST', body: JSON.stringify({ candidate_id: candidateId, expected_revision: expectedRevision }) }),
  createWorkOrder: (scenarioId: string, order: WorkOrder) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders`, { method: 'POST', body: JSON.stringify(order) }),
  updateWorkOrder: (scenarioId: string, orderId: string, order: Partial<WorkOrder>) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'PUT', body: JSON.stringify(order) }),
  deleteWorkOrder: (scenarioId: string, orderId: string) => request<Scenario>(`/api/scenarios/${scenarioId}/work-orders/${orderId}`, { method: 'DELETE' }),
  createTechnician: (scenarioId: string, technician: Technician) => request<Scenario>(`/api/scenarios/${scenarioId}/technicians`, { method: 'POST', body: JSON.stringify(technician) }),
  updateTechnician: (scenarioId: string, technicianId: string, technician: Partial<Technician>) => request<Scenario>(`/api/scenarios/${scenarioId}/technicians/${technicianId}`, { method: 'PUT', body: JSON.stringify(technician) }),
}
