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
- `overtime_base_cost_cents`: overtime base wage that is separate only in `PAID_SHIFT`; it is zero when occupied labor already includes those minutes.
- `overtime_premium_cost_cents`: overtime minutes multiplied by the technician rate and premium percentage.
- `cash_operating_cost_cents`: regular labor, overtime base, overtime premium, travel and outsourcing cash cost.
- `service_failure_loss_cents`: modeled SLA lateness and unserved-demand economic loss.
- `total_economic_impact_cents`: the sum of the previous two. It is not an accounting total.
- `additional_disruption_probability`: probability that absence, no-show, window failure, overtime breach or an emergency-caused degradation harms the published plan. A harmless emergency event is not counted as disruption.
- `emergency_event_probability`: probability that the simulated emergency capacity event occurs.
- `emergency_caused_failure_probability`: probability that the event causes a new window, overtime, unserved or SLA degradation outcome. `emergency_failure_given_event_probability` conditions that count on event occurrence.
- `baseline_unserved_orders`: work already unserved by the published plan.
- `expected_total_unserved_orders`: baseline unserved work plus simulated additional loss.

Decision figures are valid only for the recorded snapshot, complete schedule hash, travel model, policy, algorithm version, build SHA, execution watermark, and input fingerprint. The implemented scope is `EX_ANTE_FROZEN_PLAN`: it excludes actual execution and is not a remaining-work forecast.

The risk field `monte_carlo_mean_ci_low/high` is an interval for simulation mean error, not a confidence interval for uncertain real-world parameters. `full_day_total_late_minutes_p50/p90/p95` is the percentile of total late minutes across a simulated service day, not the percentile of individual work-order lateness. Keyed random draws let plans with the same snapshot, seed and risk policy share `simulation_scenario_set_hash` for paired comparison.

Capacity `option_applicable` says whether an input change can be constructed. `schedule_feasible` says whether the resulting complete schedule passes coverage, uniqueness, skill, time-window, travel-continuity, lock, fixed-assignment, real-depot-return, and overtime checks. The compatibility field `feasible` is true only when both are true. Invalid options expose only diagnostic metrics and evidence. `cost_unit_type`, `cost_units_per_day` and `affected_entity_ids` define per-shift and per-order charges. `economic_impact_offset_days` can include modeled avoided loss and is not a cash-payback claim.
