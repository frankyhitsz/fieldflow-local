/* This file is generated from docs/openapi.json. Do not edit it by hand. */
/* Run: python3 scripts/generate_openapi_types.py --write */

export interface components {
  schemas: {
    ActivatePlanRequest: {
          expected_revision: number;
          idempotency_key: string;
        };
    AdditionalTechnicianCostMode: "WAGE_ONLY" | "FIXED_ONLY" | "WAGE_PLUS_FIXED";
    AnalysisFailureManifest: {
          error_code: string;
          error_hash: string;
          failure_stage: string;
          finished_at: string;
          policy_version?: string;
          status: "FAILED" | "INTERRUPTED";
        };
    AnalysisHorizon: {
          currency?: "CNY";
          days?: number;
          workdays_per_month?: number;
        };
    AnalysisIntegrityStatus: "VERIFIED" | "FAILED" | "LEGACY_UNATTESTED";
    AnalysisReservationManifest: {
          analysis_id: string;
          input_hash: string;
          plan_manifest_hash: string;
          policy_version?: string;
          reservation_hash: string;
          started_at: string;
        };
    AttestationRequirement: "REQUIRED" | "LEGACY_MIGRATED";
    CapacityAnalysis: {
          actual_execution_included?: boolean;
          algorithm_version?: string;
          analysis_as_of_time?: (number) | (null);
          analysis_code_version: string;
          analysis_horizon?: components['schemas']["AnalysisHorizon"];
          analysis_input_hash: string;
          analysis_scope?: components['schemas']["DecisionAnalysisScope"];
          base_cost: components['schemas']["PlanCostBreakdown"];
          base_schedule_signature: string;
          build_sha?: string;
          capacity_policy: components['schemas']["CapacityPolicy"];
          capacity_policy_fingerprint: string;
          cost_policy_fingerprint: string;
          current_execution_watermark?: number;
          evaluation_method: string;
          execution_context_hash?: (string) | (null);
          options: Array<components['schemas']["CapacityOptionResult"]>;
          placement_mode?: components['schemas']["CapacityPlacementMode"];
          plan_number: number;
          plan_version_id: string;
          reference_kpis: components['schemas']["ScheduleKPI"];
          reference_mode: components['schemas']["CapacityReferenceMode"];
          reference_schedule_signature: string;
          reference_solver_policy_fingerprint: string;
          reference_travel_model_fingerprint: string;
          scenario_id: string;
          scenario_snapshot_hash: string;
          selected_plan_signature: string;
        };
    CapacityAnalysisParameters: {
          analysis_horizon?: components['schemas']["AnalysisHorizon"];
          candidate_technician?: (components['schemas']["TechnicianArchetype"]) | (null);
          capacity_policy?: components['schemas']["CapacityPolicy"];
          cost_policy?: components['schemas']["DecisionCostPolicy"];
          option_ids?: Array<"add_technician" | "add_skill" | "extend_shift" | "allow_overtime" | "outsource_unserved" | "relocate_one_technician_start">;
          placement_mode?: components['schemas']["CapacityPlacementMode"];
          reference_mode?: components['schemas']["CapacityReferenceMode"];
          skill_investment_target?: (components['schemas']["SkillInvestmentTarget"]) | (null);
        };
    CapacityAnalysisRequest: {
          analysis_horizon?: components['schemas']["AnalysisHorizon"];
          analysis_scope?: (components['schemas']["DecisionAnalysisScope"]) | (null);
          candidate_technician?: (components['schemas']["TechnicianArchetype"]) | (null);
          capacity_policy?: components['schemas']["CapacityPolicy"];
          cost_policy?: components['schemas']["DecisionCostPolicy"];
          option_ids?: Array<"add_technician" | "add_skill" | "extend_shift" | "allow_overtime" | "outsource_unserved" | "relocate_one_technician_start">;
          placement_mode?: components['schemas']["CapacityPlacementMode"];
          reference_mode?: components['schemas']["CapacityReferenceMode"];
          skill_investment_target?: (components['schemas']["SkillInvestmentTarget"]) | (null);
        };
    CapacityCostSource: "COST_POLICY" | "CAPACITY_POLICY";
    CapacityCounterfactualArtifact: {
          analysis_run_id: string;
          artifact_hash?: string;
          artifact_type?: "CAPACITY_COUNTERFACTUAL";
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          changed_inputs?: {
            [key: string]: unknown;
          };
          commercial_verification_status: "VERIFIED" | "UNVERIFIED" | "NOT_APPLICABLE";
          conditional_assumptions?: Array<string>;
          conditional_upper_bound_kpis?: (components['schemas']["CapacityCounterfactualKPI"]) | (null);
          counterfactual_kpis?: (components['schemas']["CapacityCounterfactualKPI"]) | (null);
          created_at: string;
          decision_status: components['schemas']["CapacityDecisionStatus"];
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          external_assignments?: Array<components['schemas']["ExternalAssignment"]>;
          formal_result_available: boolean;
          id: string;
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          option_id: "add_technician" | "add_skill" | "extend_shift" | "allow_overtime" | "outsource_unserved" | "relocate_one_technician_start";
          parent_analysis_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          route_diff?: Array<{
            [key: string]: unknown;
          }>;
          scenario_id: string;
          schedule: components['schemas']["ScheduleResult"];
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          structural_verification: components['schemas']["CapacityVerificationReport"];
          verification_report: components['schemas']["CapacityVerificationReport"];
          work_order_dispositions?: Array<components['schemas']["WorkOrderDisposition"]>;
        };
    CapacityCounterfactualKPI: {
          active_work_order_count: number;
          completion_rate: number;
          external_assignment_count: number;
          internal_assignment_count: number;
          overtime_minutes: number;
          sla_on_time_rate: number;
          travel_minutes: number;
          unserved_count: number;
        };
    CapacityDecisionAnalysisRunRequest: {
          analysis_scope?: (components['schemas']["DecisionAnalysisScope"]) | (null);
          analysis_type: "CAPACITY";
          request?: components['schemas']["CapacityAnalysisParameters"];
        };
    CapacityDecisionStatus: "INTERNAL_VERIFIED" | "EXTERNAL_CONDITIONAL" | "EXTERNAL_CONFIRMED" | "INFEASIBLE" | "NOT_APPLICABLE";
    CapacityOptionResult: {
          affected_entity_ids?: Array<string>;
          artifact_hash?: (string) | (null);
          artifact_id?: (string) | (null);
          assumption: string;
          break_even_days?: (number) | (null);
          cash_payback_days?: (number) | (null);
          changed_inputs?: {
            [key: string]: unknown;
          };
          completion_improvement_percentage_points?: (number) | (null);
          completion_rate?: (number) | (null);
          conditional_upper_bound_kpis?: (components['schemas']["CapacityCounterfactualKPI"]) | (null);
          cost_unit_type?: components['schemas']["CostUnitType"];
          cost_units_per_day?: number;
          counterfactual_cost?: (components['schemas']["PlanCostBreakdown"]) | (null);
          counterfactual_kpis?: (components['schemas']["CapacityCounterfactualKPI"]) | (null);
          daily_operating_delta_cents?: (number) | (null);
          decision_status?: components['schemas']["CapacityDecisionStatus"];
          diagnostic_metrics?: {
            [key: string]: (number) | (number);
          };
          diagnostic_schedule?: (components['schemas']["ScheduleResult"]) | (null);
          economic_impact_offset_days?: (number) | (null);
          external_assignments?: Array<components['schemas']["ExternalAssignment"]>;
          feasible: boolean;
          fixed_capacity_cost_cents: number;
          fixed_cost_cadence?: components['schemas']["CostCadence"];
          horizon_total_impact_cents?: (number) | (null);
          marginal_cost_cents?: (number) | (null);
          name: string;
          one_time_investment_cents?: number;
          option_applicable?: boolean;
          option_id: "add_technician" | "add_skill" | "extend_shift" | "allow_overtime" | "outsource_unserved" | "relocate_one_technician_start";
          overtime_delta_minutes?: (number) | (null);
          overtime_minutes?: (number) | (null);
          placement_mode?: components['schemas']["CapacityPlacementMode"];
          projected_total_cost_cents?: (number) | (null);
          route_diff?: Array<{
            [key: string]: unknown;
          }>;
          schedule_feasible?: boolean;
          schedule_signature: string;
          sla_improvement_percentage_points?: (number) | (null);
          sla_on_time_rate?: (number) | (null);
          travel_delta_minutes?: (number) | (null);
          travel_minutes?: (number) | (null);
          unassigned_count?: (number) | (null);
          unassigned_delta?: (number) | (null);
          verification_report?: (components['schemas']["CapacityVerificationReport"]) | (null);
          violations?: Array<components['schemas']["CapacityViolation"]>;
          work_order_dispositions?: Array<components['schemas']["WorkOrderDisposition"]>;
        };
    CapacityPlacementMode: "TAIL_APPEND_ONLY";
    CapacityPolicy: {
          add_skill_cost_cadence?: components['schemas']["CostCadence"];
          add_skill_fixed_cost_cents?: number;
          add_technician_cost_cadence?: components['schemas']["CostCadence"];
          add_technician_cost_mode?: components['schemas']["AdditionalTechnicianCostMode"];
          add_technician_fixed_cost_cents?: number;
          allow_overtime_cost_cadence?: components['schemas']["CostCadence"];
          allow_overtime_fixed_cost_cents?: number;
          extend_shift_cost_cadence?: components['schemas']["CostCadence"];
          extend_shift_fixed_cost_cents?: number;
          outsource_cost_source?: components['schemas']["CapacityCostSource"];
          outsource_unserved_cost_cadence?: components['schemas']["CostCadence"];
          outsource_unserved_fixed_cost_cents?: number;
          policy_version?: string;
          relocate_one_technician_start_cost_cadence?: components['schemas']["CostCadence"];
          relocate_one_technician_start_fixed_cost_cents?: number;
        };
    CapacityReferenceMode: "SELECTED_PLAN_DELTA" | "CONTROLLED_REOPTIMIZATION";
    CapacityVerificationReport: {
          valid: boolean;
          violations?: Array<components['schemas']["CapacityViolation"]>;
        };
    CapacityViolation: {
          code: string;
          message: string;
          technician_id?: (string) | (null);
          work_order_id?: (string) | (null);
        };
    CloneScenarioRequest: {
          idempotency_key: string;
          name: string;
        };
    Comparison: {
          added_work_orders?: Array<string>;
          after: components['schemas']["ScheduleResult"];
          before: components['schemas']["ScheduleResult"];
          changed_orders: Array<{
            [key: string]: unknown;
          }>;
          common_technicians?: Array<string>;
          common_work_order_count?: number;
          comparable?: boolean;
          delta: {
            [key: string]: (number) | (number) | (null);
          };
          modified_work_orders?: Array<string>;
          removed_work_orders?: Array<string>;
          same_scenario_snapshot?: boolean;
          scenario_id: string;
        };
    CostAnalysis: {
          actual_execution_included?: boolean;
          algorithm_version?: string;
          analysis_as_of_time?: (number) | (null);
          analysis_code_version: string;
          analysis_horizon?: components['schemas']["AnalysisHorizon"];
          analysis_input_hash: string;
          analysis_scope?: components['schemas']["DecisionAnalysisScope"];
          assumptions?: Array<string>;
          breakdown: components['schemas']["PlanCostBreakdown"];
          build_sha?: string;
          current_execution_watermark?: number;
          execution_context_hash?: (string) | (null);
          horizon_total_economic_impact_cents?: number;
          plan_number: number;
          plan_version_id: string;
          policy: components['schemas']["DecisionCostPolicy"];
          policy_fingerprint: string;
          scenario_id: string;
          scenario_snapshot_hash: string;
          schedule_signature: string;
          travel_model_fingerprint: string;
        };
    CostAnalysisParameters: {
          analysis_horizon?: components['schemas']["AnalysisHorizon"];
          cost_policy?: components['schemas']["DecisionCostPolicy"];
        };
    CostCadence: "ONE_TIME" | "PER_DAY" | "PER_SHIFT" | "PER_ORDER" | "PER_MONTH";
    CostComponent: {
          amount_cents: number;
          component: components['schemas']["CostComponentKind"];
          scope: components['schemas']["DecisionAnalysisScope"];
          source_id: string;
        };
    CostComponentKind: "REGULAR_LABOR" | "OVERTIME_BASE" | "OVERTIME_PREMIUM" | "TRAVEL" | "OUTSOURCING" | "SLA_PENALTY" | "UNSERVED_REVENUE";
    CostDecisionAnalysisRunRequest: {
          analysis_scope?: (components['schemas']["DecisionAnalysisScope"]) | (null);
          analysis_type: "COST";
          request?: components['schemas']["CostAnalysisParameters"];
        };
    CostLedger: {
          components?: Array<components['schemas']["CostComponent"]>;
          policy_version?: string;
        };
    CostUnitType: "INVESTMENT" | "PLAN_DAY" | "TECHNICIAN_SHIFT" | "WORK_ORDER" | "WORK_MONTH";
    CoverageSummary: {
          active_work_orders: number;
          assigned_work_orders: number;
          duplicate_assignments?: Array<string>;
          duplicate_unassigned?: Array<string>;
          missing_work_orders?: Array<string>;
          overlapping_work_orders?: Array<string>;
          unassigned_work_orders: number;
        };
    DecisionAnalysisRun: {
          active_booking_ids?: Array<string>;
          actual_execution_included?: boolean;
          algorithm_version?: string;
          analysis_as_of_time?: (number) | (null);
          analysis_manifest_hash?: (string) | (null);
          analysis_scope?: components['schemas']["DecisionAnalysisScope"];
          analysis_type: "COST" | "CAPACITY" | "RISK";
          artifact_manifest?: Array<{
            [key: string]: string;
          }>;
          attempt_number?: number;
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          build_sha?: string;
          code_version: string;
          created_at: string;
          current_execution_watermark?: number;
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          error?: ({
            [key: string]: unknown;
          }) | (null);
          execution_context_hash?: (string) | (null);
          failure_manifest?: (components['schemas']["AnalysisFailureManifest"]) | (null);
          finished_at?: (string) | (null);
          id: string;
          input_hash: string;
          input_manifest?: (components['schemas']["DecisionInputManifest"]) | (null);
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          logical_analysis_id?: string;
          number: number;
          parent_plan_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          plan_number: number;
          plan_version_id: string;
          policy_snapshot: {
            [key: string]: unknown;
          };
          policy_version: string;
          release_manifest?: (components['schemas']["ReleaseManifest"]) | (null);
          request_snapshot?: {
            [key: string]: unknown;
          };
          reservation_manifest?: (components['schemas']["AnalysisReservationManifest"]) | (null);
          result?: (components['schemas']["CostAnalysis"]) | (components['schemas']["CapacityAnalysis"]) | (components['schemas']["RiskSimulationResult"]) | (null);
          result_hash?: (string) | (null);
          result_manifest?: (components['schemas']["DecisionResultManifest"]) | (null);
          retry_of_analysis_id?: (string) | (null);
          runtime_manifest?: (components['schemas']["DecisionRuntimeManifest"]) | (null);
          scenario_id: string;
          scenario_snapshot_hash: string;
          schedule_hash: string;
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          status?: "RUNNING" | "COMPLETED" | "FAILED" | "INTERRUPTED";
          supersedes_analysis_id?: (string) | (null);
          travel_model_fingerprint: string;
        };
    DecisionAnalysisScope: "FROZEN_FULL_PLAN" | "PUBLICATION_REMAINING_PLAN" | "EX_ANTE_FROZEN_PLAN" | "INCURRED_ACTUAL" | "REMAINING_FORECAST" | "ACTUAL_PLUS_FORECAST";
    DecisionCostPolicy: {
          currency?: "CNY";
          labor_cost_mode?: components['schemas']["LaborCostMode"];
          outsourcing_cost_per_order_cents?: number;
          overtime_premium_basis_points?: number;
          policy_version?: string;
          sla_penalty_per_late_minute_cents?: number;
          travel_cost_per_minute_cents?: number;
          unserved_high_revenue_cents?: number;
          unserved_low_revenue_cents?: number;
          unserved_normal_revenue_cents?: number;
          unserved_urgent_revenue_cents?: number;
          vip_revenue_premium_cents?: number;
        };
    DecisionInputManifest: {
          analysis_context_hash: string;
          plan_manifest_hash: string;
          policy_hash: string;
          policy_version?: string;
          request_hash: string;
          runtime_manifest_hash: string;
          scenario_snapshot_hash: string;
          schedule_hash: string;
          semantic_input_hash: string;
          travel_model_fingerprint: string;
        };
    DecisionResultManifest: {
          finished_at: string;
          policy_version?: string;
          result_hash: string;
          status?: "COMPLETED";
        };
    DecisionRuntimeManifest: {
          architecture: string;
          build_sha: string;
          dependency_lock_hash: string;
          hash_schema_version: string;
          operating_system: string;
          ortools_version: string;
          policy_version?: string;
          pydantic_version: string;
          python_version: string;
          sqlite_version: string;
        };
    EmergencyDecisionInformationSet: {
          candidate_technician_ids?: Array<string>;
          decision_time: number;
          deterministic_dispatch_by_technician?: {
            [key: string]: number;
          };
          deterministic_finish_by_technician?: {
            [key: string]: number;
          };
          dispatch_location?: (components['schemas']["Point"]) | (null);
          dispatch_time?: (number) | (null);
          event_time: number;
          excluded_candidate_reasons?: {
            [key: string]: string;
          };
          policy_version?: string;
          selected_technician_id?: (string) | (null);
          selection_policy?: components['schemas']["EmergencyResponderSelectionPolicy"];
        };
    EmergencyDispatchPolicy: "BETWEEN_VISITS_ONLY";
    EmergencyResponderSelectionPolicy: "MYOPIC_EARLIEST_EMERGENCY_FINISH";
    EmergencyWorkOrderCreate: {
          customer_name: string;
          drop_penalty?: number;
          id: string;
          is_emergency?: true;
          location: components['schemas']["Point"];
          note?: string;
          priority?: components['schemas']["Priority"];
          reported_at?: (number) | (null);
          required_skills: Array<components['schemas']["Skill"]>;
          service_duration: number;
          sla_deadline: number;
          title: string;
          vip?: boolean;
          window_end: number;
          window_start: number;
        };
    ExecutionSourceAssignment: {
          actual_start_at?: (number) | (null);
          booking_id?: (string) | (null);
          future_sequence?: (number) | (null);
          planned_finish_at: number;
          planned_start_at: number;
          projected_available_at: number;
          sequence: number;
          source_assignment_hash: string;
          source_schedule_id: string;
          source_sequence?: (number) | (null);
          technician_id: string;
          work_order_id: string;
        };
    ExecutionSourceContext: {
          active_plan_snapshot_hash?: (string) | (null);
          active_plan_version_id?: (string) | (null);
          active_schedule_id?: (string) | (null);
          completed_assignments?: Array<components['schemas']["ExecutionSourceAssignment"]>;
          execution_event_sequence: number;
          started_assignments?: Array<components['schemas']["ExecutionSourceAssignment"]>;
          technician_projections?: Array<components['schemas']["TechnicianExecutionProjection"]>;
        };
    ExperimentPublishRequest: {
          candidate_id: string;
          expected_revision: number;
        };
    ExternalAssignment: {
          assumed_on_time?: boolean;
          capacity_verified?: boolean;
          committed_finish_time?: (number) | (null);
          committed_start_time?: (number) | (null);
          cost_cents?: number;
          provider_id?: string;
          service_assumption?: "SAME_DAY_WITHIN_SLA";
          sla_commitment?: "UNVERIFIED_ASSUMPTION";
          work_order_id: string;
        };
    FreezeReason: "STARTED" | "COMPLETED";
    FrozenAssignment: {
          finish_time: number;
          reason: components['schemas']["FreezeReason"];
          sequence: number;
          source_assignment_hash?: (string) | (null);
          source_sequence?: (number) | (null);
          start_time: number;
          technician_id: string;
          work_order_id: string;
        };
    FrozenBookingIdentity: {
          booking_id?: (string) | (null);
          source_assignment_hash?: (string) | (null);
          source_sequence?: (number) | (null);
          technician_id: string;
          work_order_id: string;
        };
    HTTPValidationError: {
          detail?: Array<components['schemas']["ValidationError"]>;
        };
    LaborCostMode: "OCCUPIED_MINUTES" | "PAID_SHIFT" | "SALARIED_ALLOCATION";
    LockRequest: {
          locked?: boolean;
          technician_id: string;
          work_order_id: string;
        };
    LockedAssignment: {
          technician_id: string;
          work_order_id: string;
        };
    ManualReassignmentRequest: {
          expected_revision: number;
          idempotency_key: string;
          planning_time: number;
          technician_id: string;
          work_order_id: string;
        };
    ManualReassignmentResult: {
          active_plan_preserved: boolean;
          error?: ({
            [key: string]: unknown;
          }) | (null);
          lock_persisted: boolean;
          replan_status: "COMPLETED" | "FAILED";
          scenario: components['schemas']["ScheduleScenario"];
          schedule?: (components['schemas']["ScheduleResult"]) | (null);
        };
    OptimizeRequest: {
          profile_id?: (string) | (null);
          strategy?: "balanced" | "completion" | "punctuality" | "low_travel" | "low_overtime" | "fair_workload";
          time_limit_seconds?: (number) | (null);
        };
    PairedMetricSummary: {
          ci_high?: (number) | (null);
          ci_low?: (number) | (null);
          conditioning_event?: (string) | (null);
          effective_sample_count?: number;
          interpretation_status?: "ESTIMATED" | "INSUFFICIENT_EVENT_TRIALS";
          interval_method?: string;
          loss_count: number;
          mean_delta?: (number) | (null);
          tie_count: number;
          win_count: number;
        };
    PlanApplicability: {
          commercial_current?: boolean;
          coverage_complete?: boolean;
          invalid_assignment_ids?: Array<string>;
          metrics_current?: boolean;
          planning_current?: boolean;
          reoptimization_opportunity?: boolean;
          route_executable?: boolean;
        };
    PlanCostBreakdown: {
          cash_operating_cost_cents: number;
          full_day_committed_labor_cost_cents?: number;
          labor_cost_cents: number;
          ledger?: (components['schemas']["CostLedger"]) | (null);
          outsourcing_cost_cents: number;
          overtime_base_cost_cents?: number;
          overtime_cost_cents: number;
          overtime_premium_cost_cents?: number;
          regular_labor_cost_cents?: number;
          remaining_incremental_labor_cost_cents?: number;
          service_failure_loss_cents: number;
          sla_penalty_cents: number;
          technician_cost_cents?: {
            [key: string]: number;
          };
          total_cost_cents: number;
          total_economic_impact_cents: number;
          travel_cost_cents: number;
          unserved_revenue_cents: number;
        };
    PlanCoverageStatus: "CURRENT_AND_COMPLETE" | "PARTIAL_NEW_DEMAND" | "STALE_DATA_CHANGED";
    PlanVersion: {
          action: "baseline" | "optimize" | "replan" | "activate" | "restore" | "experiment_publish" | "reattest";
          active?: boolean;
          applicability?: components['schemas']["PlanApplicability"];
          artifacts?: Array<components['schemas']["ScheduleArtifact"]>;
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          candidate_id?: (string) | (null);
          coverage_status?: components['schemas']["PlanCoverageStatus"];
          created_at: string;
          data_revision: number;
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          id: string;
          inherited_source_solver_policy?: (components['schemas']["SolverPolicySnapshot"]) | (null);
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          label: string;
          lineage_source_version_id?: (string) | (null);
          number: number;
          publication_manifest_hash?: string;
          publication_manifest_version?: string;
          publication_planning_context?: (components['schemas']["PublicationPlanningContext"]) | (null);
          publication_planning_context_hash?: (string) | (null);
          publication_verification_artifact?: (components['schemas']["PublicationVerificationArtifact"]) | (null);
          publication_verification_policy_version?: string;
          publication_verification_report_hash?: string;
          published_schedule_hash?: string;
          reattestation_mode?: (components['schemas']["ReattestationMode"]) | (null);
          relation?: "new" | "optimized_from" | "replanned_from" | "reactivated_from" | "restored_from" | "published_from_experiment" | "reattested_from" | "fresh_after_data_change";
          replay_validation_policy?: (string) | (null);
          scenario_id: string;
          scenario_snapshot?: (components['schemas']["ScheduleScenario"]) | (null);
          scenario_snapshot_hash?: string;
          schedule_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          selected: components['schemas']["ScheduleResult"];
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          source_plan_snapshot_hash?: (string) | (null);
          source_solver_provenance?: (components['schemas']["SourceSolverProvenance"]) | (null);
          source_version_id?: (string) | (null);
          stability_baseline_version_id?: (string) | (null);
        };
    PlanVersionPatch: {
          label: string;
        };
    PlanningContext: {
          execution_source_context?: (components['schemas']["ExecutionSourceContext"]) | (null);
          execution_warnings?: Array<string>;
          frozen_assignments?: Array<components['schemas']["FrozenAssignment"]>;
          inferred_departure_warnings?: Array<string>;
          planning_time: number;
          scenario_revision: number;
          source_plan_snapshot_hash?: (string) | (null);
          source_plan_version_id?: (string) | (null);
        };
    Point: {
          x: number;
          y: number;
        };
    Priority: "urgent" | "high" | "normal" | "low";
    PublicationPlanningContext: {
          context_fingerprint: string;
          execution_event_sequence: number;
          frozen_booking_identities?: Array<components['schemas']["FrozenBookingIdentity"]>;
          planning_time: number;
          policy_version?: string;
          route_entries?: Array<components['schemas']["RouteEntryContext"]>;
          scenario_revision: number;
          source_plan_snapshot_hash?: (string) | (null);
          source_plan_version_id?: (string) | (null);
        };
    PublicationVerificationArtifact: {
          artifact_hash: string;
          candidate_snapshot: {
            [key: string]: unknown;
          };
          planning_context_snapshot?: ({
            [key: string]: unknown;
          }) | (null);
          policy_version?: string;
          transaction_verification_report: {
            [key: string]: unknown;
          };
          verified_schedule_hash: string;
        };
    ReattestPlanRequest: {
          expected_revision: number;
          idempotency_key: string;
          mode?: components['schemas']["ReattestationMode"];
        };
    ReattestationMode: "EXACT_SNAPSHOT" | "PLANNING_EQUIVALENT";
    ReleaseManifest: {
          frontend_dependency_lock_hash: string;
          policy_version?: string;
          release_build_sha: string;
        };
    ReplanRequest: {
          current_time?: (number) | (null);
          emergency_order?: (components['schemas']["EmergencyWorkOrderCreate"]) | (null);
          idempotency_key?: (string) | (null);
          intake_idempotency_key?: (string) | (null);
          planning_time?: (number) | (null);
          profile_id?: (string) | (null);
          strategy?: "balanced" | "completion" | "punctuality" | "low_travel" | "low_overtime" | "fair_workload" | "stable";
          time_limit_seconds?: (number) | (null);
        };
    RestoreRequest: {
          allow_delete_new_orders?: boolean;
          /** 兼容旧客户端；执行事件不可变，服务端始终拒绝重新打开已完成工单 */
          allow_reopen_completed?: boolean;
          confirmation_token: string;
          expected_revision: number;
          idempotency_key: string;
          reason: string;
        };
    RiskComparisonResult: {
          delta?: {
            [key: string]: number;
          };
          paired_all_demand_sla_delta: components['schemas']["PairedMetricSummary"];
          paired_disruption_delta: components['schemas']["PairedMetricSummary"];
          paired_emergency_completion_delta: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_emergency_on_time_delta: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_overtime_delta: components['schemas']["PairedMetricSummary"];
          paired_published_sla_delta: components['schemas']["PairedMetricSummary"];
          paired_unconditional_emergency_completion_impact: components['schemas']["PairedMetricSummary"];
          paired_unserved_delta: components['schemas']["PairedMetricSummary"];
        };
    RiskComparisonRun: {
          after_analysis_id: string;
          after_analysis_manifest_hash?: string;
          after_plan_version_id: string;
          after_scenario_artifact_hash?: string;
          after_scenario_artifact_id?: string;
          after_trial_artifact_hash?: string;
          after_trial_artifact_id?: string;
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          before_analysis_id: string;
          before_analysis_manifest_hash?: string;
          before_plan_version_id: string;
          before_scenario_artifact_hash?: string;
          before_scenario_artifact_id?: string;
          before_trial_artifact_hash?: string;
          before_trial_artifact_id?: string;
          business_result_available?: boolean;
          comparison_hash: string;
          comparison_input_hash?: string;
          created_at: string;
          delta?: {
            [key: string]: number;
          };
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          id: string;
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          number: number;
          paired_all_demand_sla_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_disruption_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_emergency_completion_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_emergency_on_time_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_overtime_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_sla_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          paired_unserved_delta?: (components['schemas']["PairedMetricSummary"]) | (null);
          result?: (components['schemas']["RiskComparisonResult"]) | (null);
          scenario_id: string;
          scenario_set_hash: string;
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          trials: number;
        };
    RiskDecisionAnalysisRunRequest: {
          analysis_scope?: (components['schemas']["DecisionAnalysisScope"]) | (null);
          analysis_type: "RISK";
          request?: components['schemas']["RiskSimulationParameters"];
        };
    RiskExecutionPolicy: "FOLLOW_PUBLISHED_SCHEDULE" | "EARLIEST_FEASIBLE_EXECUTION";
    RiskSimulationParameters: {
          customer_no_show_basis_points?: number;
          emergency_dispatch_policy?: components['schemas']["EmergencyDispatchPolicy"];
          emergency_order_basis_points?: number;
          emergency_responder_selection_policy?: components['schemas']["EmergencyResponderSelectionPolicy"];
          execution_policy?: components['schemas']["RiskExecutionPolicy"];
          seed?: (number) | (null);
          service_duration_jitter_percent?: number;
          technician_absence_basis_points?: number;
          travel_delay_max_percent?: number;
          trials?: number;
        };
    RiskSimulationRequest: {
          analysis_scope?: (components['schemas']["DecisionAnalysisScope"]) | (null);
          customer_no_show_basis_points?: number;
          emergency_dispatch_policy?: components['schemas']["EmergencyDispatchPolicy"];
          emergency_order_basis_points?: number;
          emergency_responder_selection_policy?: components['schemas']["EmergencyResponderSelectionPolicy"];
          execution_policy?: components['schemas']["RiskExecutionPolicy"];
          seed?: (number) | (null);
          service_duration_jitter_percent?: number;
          technician_absence_basis_points?: number;
          travel_delay_max_percent?: number;
          trials?: number;
        };
    RiskSimulationResult: {
          absence_caused_failure_probability?: number;
          absence_caused_overtime_probability?: number;
          absence_caused_sla_degradation_probability?: number;
          absence_caused_unserved_probability?: number;
          absence_disruption_probability?: number;
          actual_execution_included?: boolean;
          additional_disruption_probability: number;
          algorithm_version?: string;
          all_demand_sla_rate?: number;
          analysis_as_of_time?: (number) | (null);
          analysis_code_version: string;
          analysis_scope?: components['schemas']["DecisionAnalysisScope"];
          assumptions?: Array<string>;
          baseline_unserved_orders: number;
          build_sha?: string;
          current_execution_watermark?: number;
          emergency_affected_work_order_count?: (number) | (null);
          emergency_capacity_disruption_probability?: number;
          emergency_caused_failure_probability?: number;
          emergency_caused_overtime_probability?: number;
          emergency_caused_sla_degradation_probability?: number;
          emergency_caused_unserved_probability?: number;
          emergency_caused_window_failure_probability?: number;
          emergency_completion_rate?: (number) | (null);
          emergency_dispatch_policy?: components['schemas']["EmergencyDispatchPolicy"];
          emergency_event_count?: number;
          emergency_event_probability?: number;
          emergency_failure_given_event_probability?: number;
          emergency_incremental_late_minutes?: (number) | (null);
          emergency_incremental_overtime_minutes?: (number) | (null);
          emergency_incremental_unserved_orders?: (number) | (null);
          emergency_on_time_rate?: (number) | (null);
          emergency_responder_selection_policy?: components['schemas']["EmergencyResponderSelectionPolicy"];
          emergency_unserved_probability?: (number) | (null);
          execution_context_hash?: (string) | (null);
          execution_policy: components['schemas']["RiskExecutionPolicy"];
          execution_policy_version?: string;
          expected_overtime_minutes: number;
          expected_sla_on_time_rate: number;
          expected_total_unserved_orders: number;
          expected_unserved_orders: number;
          full_day_total_late_minutes_p50?: number;
          full_day_total_late_minutes_p90?: number;
          full_day_total_late_minutes_p95?: number;
          late_minutes_p50: number;
          late_minutes_p90: number;
          late_minutes_p95: number;
          monte_carlo_mean_ci_high?: number;
          monte_carlo_mean_ci_low?: number;
          no_show_disruption_probability?: number;
          overtime_failure_probability?: number;
          plan_failure_probability: number;
          plan_number: number;
          plan_version_id: string;
          published_commitment_sla_rate?: number;
          scenario_id: string;
          scenario_set_artifact_id?: (string) | (null);
          scenario_snapshot_hash: string;
          schedule_signature: string;
          scope_total_late_minutes_p50?: number;
          scope_total_late_minutes_p90?: number;
          scope_total_late_minutes_p95?: number;
          seed: number;
          simulation_input_hash: string;
          simulation_policy_version?: string;
          simulation_scenario_set_hash?: string;
          sla_rate_ci_high: number;
          sla_rate_ci_low: number;
          technician_absence_event_probability?: number;
          travel_model_fingerprint: string;
          trial_outcome_artifact_id?: (string) | (null);
          trials: number;
          window_failure_probability?: number;
        };
    RiskTrialMetric: {
          all_demand_sla_rate?: number;
          disrupted: boolean;
          emergency_affected_work_order_count?: number;
          emergency_completed?: boolean;
          emergency_decision_information_set?: (components['schemas']["EmergencyDecisionInformationSet"]) | (null);
          emergency_dispatch_location?: (components['schemas']["Point"]) | (null);
          emergency_dispatch_time?: (number) | (null);
          emergency_event?: boolean;
          emergency_finish_time?: (number) | (null);
          emergency_incremental_late_minutes?: number;
          emergency_incremental_overtime_minutes?: number;
          emergency_incremental_unserved_orders?: number;
          emergency_on_time?: boolean;
          emergency_technician_id?: (string) | (null);
          published_commitment_sla_rate?: number;
          sla_on_time_rate: number;
          total_overtime_minutes: number;
          total_unserved_orders: number;
          trial: number;
        };
    RiskTrialOutcomeArtifact: {
          analysis_run_id: string;
          artifact_hash?: string;
          artifact_type?: "RISK_TRIAL_OUTCOMES";
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          created_at: string;
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          id: string;
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          metrics: Array<components['schemas']["RiskTrialMetric"]>;
          parent_analysis_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          scenario_id: string;
          scenario_set_hash: string;
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
        };
    RollbackPreview: {
          added_work_orders?: Array<string>;
          affected_execution_event_ids?: Array<string>;
          changed_plan_work_orders?: Array<string>;
          completed_work_orders_reopened?: Array<string>;
          confirmation_token: string;
          current_plan_number?: (number) | (null);
          current_plan_version_id?: (string) | (null);
          executed_work_orders_deleted?: Array<string>;
          expected_revision: number;
          lock_changes?: Array<string>;
          modified_work_orders?: Array<string>;
          removed_work_orders?: Array<string>;
          scenario_id: string;
          source_version_id: string;
          started_work_orders_reopened?: Array<string>;
          technician_changes?: Array<string>;
        };
    RouteEntryContext: {
          available_at: number;
          first_future_work_order_id?: (string) | (null);
          location: components['schemas']["Point"];
          return_location: components['schemas']["Point"];
          source_execution_event_sequence?: (number) | (null);
          source_work_order_id?: (string) | (null);
          technician_id: string;
        };
    ScenarioCreate: {
          fixture_id?: string;
          name?: (string) | (null);
        };
    ScheduleArtifact: {
          id: string;
          role: "baseline" | "selected" | "candidate";
          schedule: components['schemas']["ScheduleResult"];
          strategy: string;
        };
    ScheduleAssignment: {
          arrival_time: number;
          changed?: boolean;
          evidence?: {
            [key: string]: unknown;
          };
          explanation: Array<string>;
          finish_time: number;
          locked?: boolean;
          planning_fingerprint?: (string) | (null);
          sequence: number;
          sla_late_minutes: number;
          source_assignment_hash?: (string) | (null);
          source_sequence?: (number) | (null);
          start_time: number;
          technician_id: string;
          travel_minutes: number;
          work_order_id: string;
        };
    ScheduleCandidate: {
          created_at: string;
          id: string;
          planning_context?: (components['schemas']["PlanningContext"]) | (null);
          planning_context_hash?: (string) | (null);
          publishable: boolean;
          run_id: string;
          scenario_id: string;
          scenario_revision: number;
          scenario_snapshot_hash: string;
          schedule: components['schemas']["ScheduleResult"];
          solver_config_hash: string;
          solver_policy_fingerprint?: string;
          source_plan_version_id?: (string) | (null);
          verification_report: components['schemas']["ScheduleVerificationReport"];
        };
    ScheduleKPI: {
          adjacency_preservation_rate?: (number) | (null);
          assigned_on_time_rate?: number;
          average_occupied_utilization?: number;
          average_utilization: number;
          committed_on_time_rate?: number;
          completion_rate: number;
          customer_notification_count?: (number) | (null);
          high_priority_missed: number;
          max_normalized_workload?: number;
          min_normalized_workload?: number;
          normalized_workload_range?: number;
          p90_late_minutes?: number;
          same_technician_rate?: (number) | (null);
          sla_late_count: number;
          sla_on_time_rate: number;
          stability_rate?: (number) | (null);
          start_time_shift_median?: (number) | (null);
          start_time_shift_over_15m_count?: (number) | (null);
          start_time_shift_p90?: (number) | (null);
          technician: Array<components['schemas']["TechnicianKPI"]>;
          total_late_minutes?: number;
          total_overtime_minutes: number;
          total_service_minutes: number;
          total_travel_minutes: number;
          total_waiting_minutes?: number;
          unassigned_count: number;
          workload_range?: number;
          workload_stddev: number;
        };
    ScheduleResult: {
          assignments: Array<components['schemas']["ScheduleAssignment"]>;
          business_score?: (number) | (null);
          business_score_policy_version?: string;
          created_at: string;
          effective_time_limit_ms?: (number) | (null);
          id: string;
          kind: "baseline" | "optimized" | "replan";
          kpis: components['schemas']["ScheduleKPI"];
          metric_policy_version?: string;
          objective: number;
          objective_breakdown?: {
            [key: string]: number;
          };
          requested_time_limit_ms?: (number) | (null);
          runtime_ms: number;
          scenario_id: string;
          scenario_revision?: number;
          scenario_snapshot_hash?: string;
          solution_found?: boolean;
          solver_config_hash?: string;
          solver_name?: string;
          solver_note?: string;
          solver_objective_value?: (number) | (null);
          solver_policy?: (components['schemas']["SolverPolicySnapshot"]) | (null);
          solver_status: components['schemas']["SolverStatus"];
          solver_status_code?: (number) | (null);
          solver_version?: string;
          source_schedule_id?: (string) | (null);
          strategy?: "baseline" | "balanced" | "completion" | "punctuality" | "low_travel" | "low_overtime" | "fair_workload" | "stable" | "custom";
          termination_reason?: (string) | (null);
          travel_model_fingerprint?: string;
          travel_model_version?: string;
          unassigned: Array<components['schemas']["UnassignedWorkOrder"]>;
          version: number;
        };
    ScheduleRun: {
          action: "baseline" | "optimize" | "replan" | "activate" | "restore" | "reattest" | "experiment";
          candidate_id?: (string) | (null);
          effective_time_limit_ms: number;
          finished_at?: (string) | (null);
          id: string;
          planning_context?: (components['schemas']["PlanningContext"]) | (null);
          planning_context_hash?: (string) | (null);
          requested_time_limit_ms: number;
          scenario_id: string;
          scenario_revision: number;
          scenario_snapshot_hash: string;
          solution_found?: boolean;
          solver_config_hash: string;
          solver_name: string;
          solver_policy_fingerprint?: string;
          solver_version: string;
          source_plan_snapshot_hash?: (string) | (null);
          source_plan_version_id?: (string) | (null);
          started_at: string;
          status: components['schemas']["ScheduleRunStatus"];
          termination_reason?: (string) | (null);
        };
    ScheduleRunStatus: "QUEUED" | "RUNNING" | "OPTIMAL" | "FEASIBLE" | "TIME_LIMIT_FEASIBLE" | "TIME_LIMIT_NO_SOLUTION" | "INFEASIBLE" | "NO_SOLUTION" | "INVALID_MODEL" | "FAILED" | "CANCELLED";
    ScheduleScenario: {
          description: string;
          id: string;
          locked_assignments?: Array<components['schemas']["LockedAssignment"]>;
          name: string;
          planning_date?: string;
          revision?: number;
          seed?: number;
          solver_config?: components['schemas']["SolverConfig"];
          source_scenario_id?: (string) | (null);
          technicians: Array<components['schemas']["Technician"]>;
          work_orders: Array<components['schemas']["WorkOrder"]>;
        };
    ScheduleVerificationReport: {
          checked_at: string;
          coverage: components['schemas']["CoverageSummary"];
          errors?: Array<components['schemas']["VerificationIssue"]>;
          publishable: boolean;
          recomputed_kpis?: (components['schemas']["ScheduleKPI"]) | (null);
          valid: boolean;
          warnings?: Array<components['schemas']["VerificationIssue"]>;
        };
    SimulationEmergencyEvent: {
          duration_minutes: number;
          event_id?: string;
          event_time: number;
          location: components['schemas']["Point"];
          required_skill: components['schemas']["Skill"];
          sla_deadline?: number;
          technician_id?: (string) | (null);
          trial: number;
        };
    SimulationScenarioSetArtifact: {
          analysis_run_id: string;
          artifact_hash?: string;
          artifact_type?: "SIMULATION_SCENARIO_SET";
          attestation_requirement?: components['schemas']["AttestationRequirement"];
          created_at: string;
          effective_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          emergency_dispatch_policy?: components['schemas']["EmergencyDispatchPolicy"];
          emergency_events?: Array<components['schemas']["SimulationEmergencyEvent"]>;
          emergency_responder_selection_policy?: components['schemas']["EmergencyResponderSelectionPolicy"];
          exogenous_parameters: {
            [key: string]: number;
          };
          id: string;
          integrity_status?: components['schemas']["AnalysisIntegrityStatus"];
          keyed_random_version?: string;
          parent_analysis_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          policy_version?: string;
          scenario_id: string;
          scenario_set_hash: string;
          scenario_snapshot_hash: string;
          seed: number;
          self_integrity?: components['schemas']["AnalysisIntegrityStatus"];
          technician_ids: Array<string>;
          trials: number;
          work_order_ids: Array<string>;
        };
    Skill: "electrical" | "hvac" | "network";
    SkillInvestmentTarget: {
          skill: components['schemas']["Skill"];
          technician_id: string;
        };
    SolverConfig: {
          active_service_default_remaining_minutes?: number;
          imbalance_weight?: number;
          overtime_weight?: number;
          replan_change_weight?: number;
          sla_late_weight?: number;
          time_limit_seconds?: number;
          travel_weight?: number;
        };
    SolverPolicySnapshot: {
          effective_drop_penalties?: {
            [key: string]: number;
          };
          fingerprint: string;
          first_solution_strategy?: (string) | (null);
          local_search_metaheuristic?: (string) | (null);
          original_drop_penalties?: {
            [key: string]: number;
          };
          policy_version?: string;
          profile_id?: (string) | (null);
          profile_name: string;
          profile_snapshot?: {
            [key: string]: unknown;
          };
          solution_limit?: (number) | (null);
          solver_config: components['schemas']["SolverConfig"];
          time_limit_ms?: (number) | (null);
          unassigned_penalty_scale?: (number) | (null);
        };
    SolverStatus: "OPTIMAL" | "FEASIBLE" | "TIME_LIMIT_FEASIBLE" | "TIME_LIMIT_NO_SOLUTION" | "INFEASIBLE" | "NO_SOLUTION" | "INVALID_MODEL" | "FAILED" | "CANCELLED" | "TIME_LIMIT";
    SourceSolverProvenance: {
          claimed_policy_snapshot?: (components['schemas']["SolverPolicySnapshot"]) | (null);
          claimed_solver_name?: (string) | (null);
          claimed_solver_version?: (string) | (null);
          integrity?: components['schemas']["AnalysisIntegrityStatus"];
        };
    StrategyCandidate: {
          advantages?: Array<string>;
          dominated_by?: Array<string>;
          evaluation_score: number;
          id: string;
          pareto_optimal?: boolean;
          profile_id: string;
          profile_name: string;
          publishable?: boolean;
          schedule: components['schemas']["ScheduleResult"];
          schedule_candidate_id?: (string) | (null);
          verification_report?: (components['schemas']["ScheduleVerificationReport"]) | (null);
        };
    StrategyExperiment: {
          cancel_requested_at?: (string) | (null);
          candidate_errors?: {
            [key: string]: string;
          };
          candidates?: Array<components['schemas']["StrategyCandidate"]>;
          created_at: string;
          data_revision: number;
          dataset: string;
          error?: (string) | (null);
          fingerprint?: string;
          finished_at?: (string) | (null);
          id: string;
          profile_ids?: Array<string>;
          profile_snapshots?: Array<components['schemas']["StrategyProfile"]>;
          progress: number;
          published_at?: (string) | (null);
          requested_time_limit_seconds?: (number) | (null);
          scenario_id: string;
          scenario_snapshot?: (components['schemas']["ScheduleScenario"]) | (null);
          scenario_snapshot_hash?: string;
          score_policy_snapshot?: {
            [key: string]: number;
          };
          score_policy_version?: string;
          solver_version?: string;
          status: "QUEUED" | "RUNNING" | "CANCEL_REQUESTED" | "CANCELLED" | "COMPLETED" | "COMPLETED_WITH_ERRORS" | "FAILED" | "INTERRUPTED";
          travel_model_fingerprint?: string;
          travel_model_version?: string;
          winner_candidate_id?: (string) | (null);
          winner_plan_version_id?: (string) | (null);
        };
    StrategyExperimentRequest: {
          dataset?: "current" | "strategy-medium" | "strategy-stress";
          profile_ids?: Array<string>;
          time_limit_seconds?: (number) | (null);
        };
    StrategyProfile: {
          builtin?: boolean;
          created_at: string;
          description?: string;
          id: string;
          name: string;
          time_limit_seconds?: number;
          weights?: components['schemas']["StrategyWeights"];
        };
    StrategyProfileCreate: {
          description?: string;
          name: string;
          time_limit_seconds?: number;
          weights?: components['schemas']["StrategyWeights"];
        };
    StrategyWeights: {
          imbalance_weight?: number;
          overtime_weight?: number;
          replan_change_weight?: number;
          sla_late_weight?: number;
          travel_weight?: number;
          unassigned_penalty_scale?: number;
        };
    Technician: {
          color?: string;
          cost_per_minute_cents?: number;
          id: string;
          name: string;
          overtime_limit?: number;
          shift_end: number;
          shift_start: number;
          skills: Array<components['schemas']["Skill"]>;
          start_location: components['schemas']["Point"];
        };
    TechnicianArchetype: {
          cost_per_minute_cents?: number;
          name?: string;
          overtime_limit?: number;
          shift_end: number;
          shift_start: number;
          skills: Array<components['schemas']["Skill"]>;
          start_location: components['schemas']["Point"];
        };
    TechnicianExecutionProjection: {
          available_at: number;
          effective_location: components['schemas']["Point"];
          estimated_remaining_minutes?: number;
          execution_event_sequence: number;
          overrun?: boolean;
          source_work_order_id: string;
          state: "started" | "completed";
          technician_id: string;
        };
    TechnicianKPI: {
          assignment_count: number;
          normalized_workload?: number;
          occupied_minutes?: number;
          occupied_utilization?: number;
          overtime_minutes: number;
          overtime_ratio?: number;
          service_minutes: number;
          service_utilization?: number;
          technician_id: string;
          travel_minutes: number;
          travel_ratio?: number;
          utilization: number;
          waiting_minutes?: number;
          waiting_ratio?: number;
        };
    TechnicianUpdate: {
          color?: (string) | (null);
          cost_per_minute_cents?: (number) | (null);
          name?: (string) | (null);
          overtime_limit?: (number) | (null);
          shift_end?: (number) | (null);
          shift_start?: (number) | (null);
          skills?: (Array<components['schemas']["Skill"]>) | (null);
          start_location?: (components['schemas']["Point"]) | (null);
        };
    UnassignedReason: "NO_ELIGIBLE_TECHNICIAN" | "TIME_WINDOW_INFEASIBLE" | "SHIFT_CAPACITY_EXCEEDED" | "DROPPED_BY_OBJECTIVE" | "LOCKED_PLAN_CONFLICT";
    UnassignedWorkOrder: {
          detail: string;
          evidence?: {
            [key: string]: unknown;
          };
          reason: components['schemas']["UnassignedReason"];
          suggestions?: Array<string>;
          work_order_id: string;
        };
    ValidationError: {
          ctx?: {
          };
          input?: unknown;
          loc: Array<(string) | (number)>;
          msg: string;
          type: string;
        };
    VerificationIssue: {
          code: string;
          message: string;
          technician_id?: (string) | (null);
          work_order_id?: (string) | (null);
        };
    WorkOrder: {
          customer_name: string;
          drop_penalty?: number;
          id: string;
          is_emergency?: boolean;
          location: components['schemas']["Point"];
          note?: string;
          priority?: components['schemas']["Priority"];
          reported_at?: (number) | (null);
          required_skills: Array<components['schemas']["Skill"]>;
          service_duration: number;
          sla_deadline: number;
          status?: components['schemas']["WorkOrderStatus"];
          title: string;
          vip?: boolean;
          window_end: number;
          window_start: number;
        };
    WorkOrderCreate: {
          customer_name: string;
          drop_penalty?: number;
          id: string;
          is_emergency?: boolean;
          location: components['schemas']["Point"];
          note?: string;
          priority?: components['schemas']["Priority"];
          reported_at?: (number) | (null);
          required_skills: Array<components['schemas']["Skill"]>;
          service_duration: number;
          sla_deadline: number;
          title: string;
          vip?: boolean;
          window_end: number;
          window_start: number;
        };
    WorkOrderDisposition: {
          disposition: "INTERNAL" | "EXTERNAL" | "UNSERVED";
          external_provider_id?: (string) | (null);
          technician_id?: (string) | (null);
          work_order_id: string;
        };
    WorkOrderExecutionEvent: {
          action: "start" | "complete";
          actual_duration_minutes?: (number) | (null);
          actual_late_start_minutes?: number;
          booking_id?: string;
          created_at: string;
          customer_window_late_start_minutes?: number;
          early_start_override_reason?: (string) | (null);
          estimated_remaining_minutes?: (number) | (null);
          event_content_hash?: string;
          id: string;
          idempotency_key: string;
          note?: string;
          occurred_at: number;
          plan_version_id: string;
          planned_finish_at?: (number) | (null);
          planned_start_at?: (number) | (null);
          planned_start_variance_minutes?: (number) | (null);
          scenario_id: string;
          scenario_revision: number;
          sequence?: number;
          source_assignment_hash?: string;
          source_sequence?: number;
          technician_id: string;
          work_order_id: string;
        };
    WorkOrderExecutionRequest: {
          early_start_override_reason?: (string) | (null);
          estimated_remaining_minutes?: (number) | (null);
          expected_revision: number;
          idempotency_key: string;
          note?: string;
          occurred_at: number;
          technician_id: string;
        };
    WorkOrderExecutionResult: {
          event: components['schemas']["WorkOrderExecutionEvent"];
          scenario: components['schemas']["ScheduleScenario"];
        };
    WorkOrderStatus: "pending" | "started" | "completed";
    WorkOrderUpdate: {
          customer_name?: (string) | (null);
          drop_penalty?: (number) | (null);
          is_emergency?: (boolean) | (null);
          location?: (components['schemas']["Point"]) | (null);
          note?: (string) | (null);
          priority?: (components['schemas']["Priority"]) | (null);
          reported_at?: (number) | (null);
          required_skills?: (Array<components['schemas']["Skill"]>) | (null);
          service_duration?: (number) | (null);
          sla_deadline?: (number) | (null);
          title?: (string) | (null);
          vip?: (boolean) | (null);
          window_end?: (number) | (null);
          window_start?: (number) | (null);
        };
  };
}
