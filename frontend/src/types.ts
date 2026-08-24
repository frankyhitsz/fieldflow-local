export type Point = { x: number; y: number }
export type Technician = {
  id: string; name: string; skills: string[]; shift_start: number; shift_end: number
  start_location: Point; overtime_limit: number; cost_per_minute_cents: number; color: string
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
  action: 'start' | 'complete'; sequence: number; occurred_at: number; scenario_revision: number
  plan_version_id: string; idempotency_key: string; created_at: string
  booking_id: string; source_assignment_hash: string; source_sequence: number
  planned_start_at: number | null; planned_finish_at: number | null
  actual_duration_minutes: number | null; actual_late_start_minutes: number
  early_start_override_reason: string | null; estimated_remaining_minutes: number | null; note: string
}
export type ExecutionResult = { scenario: Scenario; event: ExecutionEvent }
export type ManualReassignmentResult = {
  lock_persisted: boolean; replan_status: 'COMPLETED' | 'FAILED'; active_plan_preserved: boolean
  scenario: Scenario; schedule: Schedule | null; error: Record<string, unknown> | null
}
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
  source_sequence?: number | null; source_assignment_hash?: string | null; planning_fingerprint?: string | null
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
  solver_policy: { policy_version: string; profile_id: string | null; profile_name: string; profile_snapshot: Record<string, unknown>; solver_config: Record<string, unknown>; unassigned_penalty_scale: number | null; original_drop_penalties: Record<string, number>; effective_drop_penalties: Record<string, number>; time_limit_ms: number | null; solution_limit: number | null; first_solution_strategy: string | null; local_search_metaheuristic: string | null; fingerprint: string } | null
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
  lineage_source_version_id: string | null; stability_baseline_version_id: string | null
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
  completed_work_orders_reopened: string[]; started_work_orders_reopened: string[]
  executed_work_orders_deleted: string[]; affected_execution_event_ids: string[]
  technician_changes: string[]; lock_changes: string[]
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

export type CostBreakdown = {
  labor_cost_cents: number; travel_cost_cents: number; overtime_cost_cents: number
  sla_penalty_cents: number; unserved_revenue_cents: number; outsourcing_cost_cents: number
  cash_operating_cost_cents: number; service_failure_loss_cents: number; total_economic_impact_cents: number
  total_cost_cents: number; technician_cost_cents: Record<string, number>
}

export type CostAnalysis = {
  scenario_id: string; plan_version_id: string; plan_number: number; scenario_snapshot_hash: string
  schedule_signature: string; analysis_scope: 'FULL_DAY_PLAN'; travel_model_fingerprint: string
  analysis_code_version: string; analysis_input_hash: string
  policy: Record<string, unknown>; policy_fingerprint: string; breakdown: CostBreakdown; assumptions: string[]
}

export type CapacityOption = {
  option_id: 'add_technician' | 'add_skill' | 'extend_shift' | 'allow_overtime' | 'outsource_unserved' | 'relocate_one_technician_start'
  name: string; assumption: string; feasible: boolean
  completion_rate: number; sla_on_time_rate: number; unassigned_count: number
  travel_minutes: number; overtime_minutes: number
  completion_improvement_percentage_points: number; sla_improvement_percentage_points: number
  unassigned_delta: number; travel_delta_minutes: number; overtime_delta_minutes: number
  fixed_capacity_cost_cents: number; marginal_cost_cents: number; projected_total_cost_cents: number
  schedule_signature: string
}

export type CapacityAnalysis = {
  scenario_id: string; plan_version_id: string; plan_number: number; scenario_snapshot_hash: string
  analysis_scope: 'FULL_DAY_PLAN'; analysis_code_version: string; analysis_input_hash: string; evaluation_method: string
  reference_mode: 'SELECTED_PLAN_DELTA' | 'CONTROLLED_REOPTIMIZATION'
  selected_plan_signature: string; reference_schedule_signature: string
  reference_solver_policy_fingerprint: string; reference_travel_model_fingerprint: string
  reference_kpis: Kpis; cost_policy_fingerprint: string
  capacity_policy: Record<string, unknown>; capacity_policy_fingerprint: string
  base_schedule_signature: string; base_cost: CostBreakdown; options: CapacityOption[]
}

export type RiskSimulation = {
  scenario_id: string; plan_version_id: string; plan_number: number; scenario_snapshot_hash: string
  schedule_signature: string; analysis_scope: 'FULL_DAY_PLAN'; travel_model_fingerprint: string
  execution_policy: 'FOLLOW_PUBLISHED_SCHEDULE' | 'EARLIEST_FEASIBLE_EXECUTION'; execution_policy_version: string
  simulation_policy_version: string; analysis_code_version: string; simulation_input_hash: string; seed: number; trials: number
  expected_sla_on_time_rate: number; sla_rate_ci_low: number; sla_rate_ci_high: number
  late_minutes_p50: number; late_minutes_p90: number; late_minutes_p95: number
  expected_overtime_minutes: number; additional_disruption_probability: number
  baseline_unserved_orders: number; expected_total_unserved_orders: number
  plan_failure_probability: number; expected_unserved_orders: number
  assumptions: string[]
}

export type DecisionAnalysisRun<T = CostAnalysis | CapacityAnalysis | RiskSimulation> = {
  id: string; scenario_id: string; number: number; plan_version_id: string; plan_number: number
  analysis_type: 'COST' | 'CAPACITY' | 'RISK'; scenario_snapshot_hash: string; schedule_hash: string
  execution_watermark: number | null; travel_model_fingerprint: string
  policy_version: string; policy_snapshot: Record<string, unknown>; code_version: string; input_hash: string
  status: 'COMPLETED'; result: T; created_at: string
}
