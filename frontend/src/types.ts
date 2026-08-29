import type { components as OpenApiComponents } from './generated/openapi'

type ApiSchemas = OpenApiComponents['schemas']
type Hydrated<T> = { [Key in keyof T]-?: Exclude<T[Key], undefined> }

export type Point = ApiSchemas['Point']
export type Technician = Hydrated<ApiSchemas['Technician']>
export type WorkOrder = Hydrated<ApiSchemas['WorkOrder']>
export type ExecutionEvent = Hydrated<ApiSchemas['WorkOrderExecutionEvent']>
export type ExecutionResult = { scenario: Scenario; event: ExecutionEvent }
type ApiManualReassignmentResult = ApiSchemas['ManualReassignmentResult']
export type ManualReassignmentResult = Omit<Required<ApiManualReassignmentResult>, 'scenario' | 'schedule'> & {
  scenario: Scenario
  schedule: Schedule | null
}
export type Scenario = Omit<Hydrated<Pick<ApiSchemas['ScheduleScenario'], 'id' | 'name' | 'description' | 'planning_date' | 'seed' | 'technicians' | 'work_orders' | 'locked_assignments' | 'revision'>>, 'technicians' | 'work_orders'> & {
  technicians: Technician[]
  work_orders: WorkOrder[]
}
export type CurrentWorkOrderDisposition = ApiSchemas['CurrentWorkOrderDisposition']
type ApiOperationalWorkOrder = ApiSchemas['OperationalWorkOrderView']
type ApiOperationalView = ApiSchemas['ScenarioOperationalView']
type ApiDispatchSnapshot = ApiSchemas['DispatchSnapshot']
export type OperationalView = Omit<Required<ApiOperationalView>, 'plan_applicability' | 'work_orders'> & {
  plan_applicability: PlanVersion['applicability'] | null
  work_orders: Array<Omit<Required<ApiOperationalWorkOrder>, 'assignment'> & { assignment: Assignment | null }>
}
export type DispatchSnapshot = Omit<Required<ApiDispatchSnapshot>, 'scenario' | 'active_plan' | 'operational_view'> & {
  scenario: Scenario
  active_plan: PlanVersion | null
  operational_view: OperationalView
}
type ApiAssignment = ApiSchemas['ScheduleAssignment']
export type Assignment = Omit<Hydrated<ApiAssignment>, 'planning_fingerprint' | 'source_assignment_hash' | 'source_sequence'> & Pick<ApiAssignment, 'planning_fingerprint' | 'source_assignment_hash' | 'source_sequence'>
export type Unassigned = Hydrated<ApiSchemas['UnassignedWorkOrder']>
export type TechKpi = Hydrated<ApiSchemas['TechnicianKPI']>
export type Kpis = Hydrated<ApiSchemas['ScheduleKPI']>
export type Schedule = Omit<Hydrated<ApiSchemas['ScheduleResult']>, 'assignments' | 'unassigned' | 'kpis'> & {
  assignments: Assignment[]
  unassigned: Unassigned[]
  kpis: Kpis
}
export type Comparison = Omit<Hydrated<ApiSchemas['Comparison']>, 'before' | 'after'> & {
  before: Schedule
  after: Schedule
}

export type Strategy = 'balanced' | 'completion' | 'punctuality' | 'low_travel' | 'low_overtime' | 'fair_workload'
export type IntegrityStatus = 'VERIFIED' | 'FAILED' | 'LEGACY_UNATTESTED'

type ApiPlanVersion = ApiSchemas['PlanVersion']
type OptionalPlanProofFields = 'publication_manifest_hash' | 'publication_planning_context' | 'publication_planning_context_hash' | 'publication_verification_artifact' | 'scenario_snapshot'
export type PlanVersion = Omit<Hydrated<ApiPlanVersion>, OptionalPlanProofFields | 'selected' | 'artifacts' | 'applicability'> & Partial<Pick<ApiPlanVersion, OptionalPlanProofFields>> & {
  scenario_snapshot?: Scenario | null
  selected: Schedule
  artifacts: Array<Omit<ApiSchemas['ScheduleArtifact'], 'schedule'> & { schedule: Schedule }>
  applicability: Hydrated<ApiSchemas['PlanApplicability']>
}
export type RollbackPreview = Hydrated<ApiSchemas['RollbackPreview']>
export type StrategyWeights = Hydrated<ApiSchemas['StrategyWeights']>
export type StrategyProfile = Omit<Hydrated<ApiSchemas['StrategyProfile']>, 'weights'> & { weights: StrategyWeights }
export type StrategyCandidate = Omit<Hydrated<ApiSchemas['StrategyCandidate']>, 'schedule'> & { schedule: Schedule }
export type StrategyExperiment = Omit<Hydrated<ApiSchemas['StrategyExperiment']>, 'candidates'> & { candidates: StrategyCandidate[] }
export type RuntimeJob = Hydrated<ApiSchemas['RuntimeJob']>

export type CostBreakdown = Hydrated<ApiSchemas['PlanCostBreakdown']>
export type DecisionAnalysisScope = ApiSchemas['DecisionAnalysisScope']
export type AnalysisHorizon = Hydrated<ApiSchemas['AnalysisHorizon']>
export type CostCadence = ApiSchemas['CostCadence']
export type AnalysisContextFields = {
  analysis_scope: DecisionAnalysisScope; current_execution_watermark: number
  analysis_as_of_time: number | null; execution_context_hash: string | null
  actual_execution_included: boolean; algorithm_version: string; build_sha: string
}

export type CostAnalysis = Omit<Hydrated<ApiSchemas['CostAnalysis']>, 'breakdown' | 'analysis_horizon'> & {
  breakdown: CostBreakdown
  analysis_horizon: AnalysisHorizon
}
export type CapacityViolation = Hydrated<ApiSchemas['VerificationIssue']>
export type CapacityOption = Omit<Hydrated<ApiSchemas['CapacityOptionResult']>, 'diagnostic_schedule' | 'violations'> & {
  diagnostic_schedule: Schedule | null
  violations: CapacityViolation[]
}
export type CapacityAnalysis = Omit<Hydrated<ApiSchemas['CapacityAnalysis']>, 'base_cost' | 'options' | 'reference_kpis'> & {
  base_cost: CostBreakdown
  options: CapacityOption[]
  reference_kpis: Kpis
}
export type RiskSimulation = Hydrated<ApiSchemas['RiskSimulationResult']>

export type DecisionAnalysisRun<T = CostAnalysis | CapacityAnalysis | RiskSimulation> = Omit<Hydrated<ApiSchemas['DecisionAnalysisRun']>, 'result'> & {
  result: T | null
}

export type CapacityCounterfactualArtifact = Omit<Hydrated<ApiSchemas['CapacityCounterfactualArtifact']>, 'schedule'> & { schedule: Schedule }
export type SimulationScenarioSetArtifact = Hydrated<ApiSchemas['SimulationScenarioSetArtifact']>
export type RiskTrialOutcomeArtifact = Hydrated<ApiSchemas['RiskTrialOutcomeArtifact']>

export type DecisionAnalysisArtifact = CapacityCounterfactualArtifact | SimulationScenarioSetArtifact | RiskTrialOutcomeArtifact
