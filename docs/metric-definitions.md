# Metric definitions

Metric policy: `FIELD_SERVICE_METRICS_V2`  
Business score policy: `FIELD_SERVICE_SCORE_V2`

## Service

- `completion_rate`: planning coverage — assigned active work orders divided by all active work orders. It is not an execution completion rate.
- `assigned_on_time_rate`: on-time assigned work orders divided by assigned work orders.
- `committed_on_time_rate`: planned SLA coverage — on-time planned work divided by all active work orders. Unassigned work remains in the denominator; actual completion events are not used.
- `total_late_minutes` and `p90_late_minutes`: lateness across assigned active work.

## Capacity

- `service_utilization`: service minutes divided by normal shift minutes.
- `occupied_utilization`: travel, waiting, and service minutes divided by normal shift minutes.
- `travel_ratio` and `waiting_ratio`: each component divided by occupied minutes.
- `overtime_ratio`: overtime divided by normal shift minutes.
- `normalized_workload`: service minutes divided by normal shift minutes.
- `normalized_workload_range`: highest normalized workload minus lowest.

## Replanning stability

- `same_technician_rate`: pending work that keeps its technician.
- `adjacency_preservation_rate`: prior same-technician predecessor pairs that remain adjacent.
- `start_time_shift_median` / `start_time_shift_p90`: absolute start-time movement.
- `customer_notification_count`: removed work, technician changes, or start-time movement over 15 minutes.

## Two objective values

`solver_objective_value` is returned by OR-Tools and only has meaning for that solver configuration. `business_score` is recomputed from common business metrics and carries a policy version. Strategy experiments compare the latter and show the raw KPI beside it.

## Decision analysis

- `regular_labor_cost_cents`: occupied minutes in `OCCUPIED_MINUTES`, or the normal paid shift in `PAID_SHIFT`.
- `full_day_committed_labor_cost_cents`: full normal-shift wage exposure for the day. In a remaining-plan view this is already committed context, not the incremental recommendation cost.
- `remaining_incremental_labor_cost_cents`: labor exposure after the publication route-entry time for technicians who still have scheduled work. It is the formal PAID_SHIFT labor figure for `PUBLICATION_REMAINING_PLAN`.
- `overtime_base_cost_cents`: overtime base wage that is separate only in `PAID_SHIFT`; it is zero when occupied labor already includes those minutes.
- `overtime_premium_cost_cents`: overtime minutes multiplied by the technician rate and premium percentage.
- `cash_operating_cost_cents`: regular labor, overtime base, overtime premium, travel and outsourcing cash cost.
- `service_failure_loss_cents`: modeled SLA lateness and unserved-demand economic loss.
- `total_economic_impact_cents`: the sum of the previous two. It is not an accounting total.
- `additional_disruption_probability`: probability that absence, no-show, window failure, overtime breach or an emergency-caused degradation harms the published plan. A harmless emergency event is not counted as disruption.
- `emergency_event_probability`: probability that the simulated emergency capacity event occurs.
- `emergency_caused_failure_probability`: probability that the event causes a new window, overtime, unserved or SLA degradation outcome. `emergency_failure_given_event_probability` conditions that count on event occurrence.
- `published_commitment_sla_rate`: on-time rate for work in the published plan; emergency demand is not in this denominator.
- `all_demand_sla_rate`: on-time rate for published work plus simulated emergency demand. Every emergency event adds one item to this denominator.
- `published_work_total_late_minutes_p50/p90/p95`: trial percentiles of total lateness for published work only.
- `all_demand_total_late_minutes_p50/p90/p95`: trial percentiles after simulated emergency demand is included. `late_minutes_p*`, `scope_total_late_minutes_p*`, and `full_day_total_late_minutes_p*` are deprecated compatibility aliases for this all-demand series and are scheduled for removal in v0.7.
- `emergency_late_minutes_mean/p50/p90`: lateness severity for the simulated emergency order among trials where an emergency occurs. These values are `null` when there is no emergency sample.
- `emergency_completion_rate`, `emergency_on_time_rate`, `emergency_unserved_probability`: conditional rates among trials where an emergency occurs.
- `emergency_event_count` / `emergency_metric_sample_count`: number of event trials used by conditional disruption statistics. `emergency_completed_sample_count` is the denominator available for completed-emergency lateness. With no event sample, emergency-only rates and severity statistics are `null`, not a perfect score.
- `emergency_incremental_late_minutes`, `emergency_incremental_overtime_minutes`, and `emergency_incremental_unserved_orders`: mean severity among event trials.
- `emergency_disposition_changed_count`, `emergency_newly_unserved_count`, `emergency_newly_late_count`, and `emergency_lateness_increased_count`: mean count for each explicitly named impact. `emergency_affected_work_order_count` is the de-duplicated union, so one work order is counted at most once per trial.
- `baseline_unserved_orders`: work already unserved by the published plan.
- `expected_total_unserved_orders`: baseline unserved work plus simulated additional loss.

Decision figures are valid only for the recorded snapshot, complete schedule hash, publication context, travel model, policy, runtime manifest, algorithm version, build SHA and input fingerprint. Non-replan plans use `FROZEN_FULL_PLAN`. Replans use `PUBLICATION_REMAINING_PLAN`: figures begin at each technician's frozen publication-time route entry and exclude work already frozen as started or completed at publication. Remaining-plan cost is a one-day, non-repeatable scope; requests with a multi-day horizon are rejected.

The risk field `monte_carlo_mean_ci_low/high` is an interval for simulation mean error, not a confidence interval for uncertain real-world parameters. Late-minute percentiles are percentiles of a trial's total lateness, not percentiles of individual work-order lateness. `EmergencyLocationPolicy` makes the spatial population explicit: `ACTIVE_DEMAND_LOCATIONS` samples the analysis work view, `ALL_FROZEN_LOCATIONS_AS_SPATIAL_PROXY` uses every frozen work-order location as a declared proxy, and `UNIFORM_SERVICE_AREA` samples the configured coordinate bounds. `EXTERNAL_EMPIRICAL_DISTRIBUTION` is rejected until an external distribution is configured. The selected policy, eligible work-order IDs and resulting locations are in the request, input hash and scenario-set artifact. Keyed random draws let plans with the same snapshot, seed and risk policy share `simulation_scenario_set_hash` for paired comparison. The scenario-set artifact freezes each emergency event's target technician, time, location, duration and skill before either plan is evaluated. Responder selection uses only the event-time information set and deterministic route projections; no-show, future service duration, future return delay, and other post-decision draws are sampled only after the responder is fixed. Paired summaries state their conditioning event and effective sample size and use paired resampling; a conditional event sample below 20 is labeled `INSUFFICIENT_EVENT_TRIALS` and has no numeric estimate or interval. Unconditional emergency-impact metrics remain available so conditioning cannot hide event frequency. Trial artifacts default to `SUMMARY_ONLY`; `FULL_TRIAL_DETAIL` is accepted only up to 1000 trials.

Capacity `option_applicable` says whether an input change can be constructed. `schedule_feasible` says whether the resulting complete schedule passes coverage, uniqueness, skill, start-window, travel-continuity, lock, fixed-assignment, return-point, and overtime checks. The compatibility field `feasible` is true only when both are true. Invalid options expose only diagnostic metrics and evidence. `cost_unit_type`, `cost_units_per_day` and `affected_entity_ids` define per-shift and per-order charges. `economic_impact_offset_days` can include modeled avoided loss and is not a cash-payback claim.

For internally verifiable options, `counterfactual_kpis` is the formal business view and `schedule.kpis` remains a route diagnostic. Outsourcing without supplier capacity evidence has `decision_status=EXTERNAL_CONDITIONAL`: `conditional_upper_bound_kpis` describes the best case under the stated external assumptions, but formal completion/SLA, `feasible`, cost impact and payback fields remain null. `outsource_cost_source` chooses either the decision cost policy or capacity policy; the same service fee is never added from both.
