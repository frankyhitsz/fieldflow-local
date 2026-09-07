import type { DispatchSnapshot } from './types'

export type DispatchState = {
  status: 'LOADING' | 'READY' | 'STALE' | 'FAILED'
  snapshot?: DispatchSnapshot
  error?: string
}
export type DispatchAction = { type: 'LOAD'; scenarioId: string } | { type: 'READY'; snapshot: DispatchSnapshot } | { type: 'FAILED'; error: string }
export function dispatchReducer(state: DispatchState, action: DispatchAction): DispatchState {
  if (action.type === 'READY') return { status: 'READY', snapshot: action.snapshot }
  if (action.type === 'FAILED') return { ...state, status: 'FAILED', error: action.error }
  const snapshot = state.snapshot?.scenario.id === action.scenarioId ? state.snapshot : undefined
  return { status: snapshot ? 'STALE' : 'LOADING', snapshot }
}

export function validateDispatch(snapshot: DispatchSnapshot, scenarioId: string): void {
  if (snapshot.scenario?.id !== scenarioId
    || !snapshot.operational_view
    || snapshot.scenario.revision !== snapshot.operational_view.scenario_revision
    || snapshot.scenario_head_snapshot_hash !== snapshot.operational_view.scenario_snapshot_hash
    || snapshot.operational_view.active_plan_version_id !== (snapshot.active_plan?.id ?? null)
    || snapshot.execution_watermark !== snapshot.operational_view.execution_watermark
    || snapshot.execution_context_hash !== snapshot.operational_view.execution_context_hash) {
    throw new Error('调度快照的 D/V/E 证明不一致，已暂停业务操作')
  }
}
