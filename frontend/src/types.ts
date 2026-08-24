export type Point = { x: number; y: number }
export type Technician = {
  id: string; name: string; skills: string[]; shift_start: number; shift_end: number
  start_location: Point; overtime_limit: number; cost_per_minute: number; color: string
}
export type WorkOrder = {
  id: string; customer_name: string; title: string; required_skills: string[]; location: Point
  service_duration: number; window_start: number; window_end: number; sla_deadline: number
  priority: 'urgent' | 'high' | 'normal' | 'low'; drop_penalty: number
  status: 'pending' | 'started' | 'completed'; vip: boolean; is_emergency: boolean
  reported_at: number | null; note: string
}
export type ExecutionEvent = {
  id: string; scenario_id: string; work_order_id: string; technician_id: string
  action: 'start' | 'complete'; occurred_at: number; scenario_revision: number
  plan_version_id: string; idempotency_key: string; created_at: string
}
export type ExecutionResult = { scenario: Scenario; event: ExecutionEvent }
export type Scenario = {
  id: string; name: string; description: string; planning_date: string; seed: number
  technicians: Technician[]; work_orders: WorkOrder[]
  locked_assignments: { work_order_id: string; technician_id: string }[]
  revision: number
}
export type Assignment = {
  work_order_id: string; technician_id: string; sequence: number; arrival_time: number
  start_time: number; finish_time: number; travel_minutes: number; sla_late_minutes: number
  explanation: string[]; evidence: Record<string, unknown>; locked: boolean; changed: boolean
}
export type Unassigned = {
  work_order_id: string; reason: string; detail: string; suggestions: string[]; evidence: Record<string, unknown>
}
export type TechKpi = {
  technician_id: string; service_minutes: number; travel_minutes: number
  overtime_minutes: number; utilization: number; assignment_count: number
  waiting_minutes: number; occupied_minutes: number; service_utilization: number
  occupied_utilization: number; travel_ratio: number; waiting_ratio: number
  overtime_ratio: number; normalized_workload: number
}
export type Kpis = {
  completion_rate: number; sla_on_time_rate: number; sla_late_count: number
  total_travel_minutes: number; total_service_minutes: number; total_overtime_minutes: number
  average_utilization: number; unassigned_count: number; high_priority_missed: number
  workload_stddev: number; stability_rate: number | null; technician: TechKpi[]
  assigned_on_time_rate: number; committed_on_time_rate: number
  total_late_minutes: number; p90_late_minutes: number; total_waiting_minutes: number
  average_occupied_utilization: number; workload_range: number; normalized_workload_range: number
  same_technician_rate: number | null; adjacency_preservation_rate: number | null
  start_time_shift_median: number | null; start_time_shift_p90: number | null
  start_time_shift_over_15m_count: number | null; customer_notification_count: number | null
}
export type Schedule = {
  id: string; scenario_id: string; kind: 'baseline' | 'optimized' | 'replan'; version: number
  created_at: string; solver_status: 'OPTIMAL' | 'FEASIBLE' | 'TIME_LIMIT_FEASIBLE' | 'TIME_LIMIT_NO_SOLUTION' | 'INFEASIBLE' | 'NO_SOLUTION' | 'INVALID_MODEL' | 'FAILED' | 'CANCELLED' | 'TIME_LIMIT'
  runtime_ms: number; objective: number; assignments: Assignment[]; unassigned: Unassigned[]
  kpis: Kpis; source_schedule_id: string | null; solver_note: string
  scenario_revision: number
  strategy: 'baseline' | 'balanced' | 'completion' | 'punctuality' | 'low_travel' | 'low_overtime' | 'fair_workload' | 'stable' | 'custom'
  objective_breakdown: Record<string, number>
  requested_time_limit_ms: number | null; effective_time_limit_ms: number | null
  solver_status_code: number | null; termination_reason: string | null; solution_found: boolean
  solver_objective_value: number | null; business_score: number | null
  business_score_policy_version: string; scenario_snapshot_hash: string; solver_config_hash: string
  travel_model_version: string; travel_model_fingerprint: string; metric_policy_version: string; solver_name: string; solver_version: string
}
export type Comparison = {
  scenario_id: string; before: Schedule; after: Schedule
  delta: Record<string, number | null>; changed_orders: Record<string, unknown>[]
  comparable: boolean; same_scenario_snapshot: boolean; common_work_order_count: number
  added_work_orders: string[]; removed_work_orders: string[]; modified_work_orders: string[]
  common_technicians: string[]
}

export type Strategy = 'balanced' | 'completion' | 'punctuality' | 'low_travel' | 'low_overtime' | 'fair_workload'

export type PlanVersion = {
  id: string; scenario_id: string; number: number
  action: 'baseline' | 'optimize' | 'replan' | 'activate' | 'restore' | 'experiment_publish'
  label: string; data_revision: number; source_version_id: string | null
  relation: 'new' | 'optimized_from' | 'replanned_from' | 'reactivated_from' | 'restored_from' | 'published_from_experiment' | 'fresh_after_data_change'
  active: boolean; created_at: string; scenario_snapshot?: Scenario | null
  coverage_status: 'CURRENT_AND_COMPLETE' | 'PARTIAL_NEW_DEMAND' | 'STALE_DATA_CHANGED'
  selected: Schedule
  artifacts: { id: string; role: 'baseline' | 'selected' | 'candidate'; strategy: string; schedule: Schedule }[]
  candidate_id: string | null; scenario_snapshot_hash: string; source_plan_snapshot_hash: string | null
}

export type RollbackPreview = {
  scenario_id: string; source_version_id: string; expected_revision: number; confirmation_token: string
  current_plan_version_id: string | null; current_plan_number: number | null
  changed_plan_work_orders: string[]
  added_work_orders: string[]; removed_work_orders: string[]; modified_work_orders: string[]
  completed_work_orders_reopened: string[]; technician_changes: string[]; lock_changes: string[]
}

export type StrategyWeights = {
  travel_weight: number; sla_late_weight: number; overtime_weight: number
  imbalance_weight: number; replan_change_weight: number; unassigned_penalty_scale: number
}

export type StrategyProfile = {
  id: string; name: string; description: string; builtin: boolean
  weights: StrategyWeights; time_limit_seconds: number; created_at: string
}

export type StrategyCandidate = {
  id: string; profile_id: string; profile_name: string; schedule: Schedule
  evaluation_score: number; advantages: string[]; publishable: boolean
  schedule_candidate_id: string | null; pareto_optimal: boolean; dominated_by: string[]
}

export type StrategyExperiment = {
  id: string; scenario_id: string; dataset: string; data_revision: number
  status: 'QUEUED' | 'RUNNING' | 'CANCEL_REQUESTED' | 'CANCELLED' | 'COMPLETED' | 'COMPLETED_WITH_ERRORS' | 'FAILED' | 'INTERRUPTED'; progress: number
  error: string | null; created_at: string; profile_ids: string[]
  requested_time_limit_seconds: number | null; candidates: StrategyCandidate[]
  fingerprint: string; scenario_snapshot_hash: string; score_policy_version: string
  travel_model_version: string; travel_model_fingerprint: string; solver_version: string; candidate_errors: Record<string, string>
  finished_at: string | null; cancel_requested_at: string | null
  winner_candidate_id: string | null; winner_plan_version_id: string | null; published_at: string | null
}
