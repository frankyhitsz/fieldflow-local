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

- `cash_operating_cost_cents`: planned labor, travel, overtime premium and outsourcing cash cost.
- `service_failure_loss_cents`: modeled SLA lateness and unserved-demand economic loss.
- `total_economic_impact_cents`: the sum of the previous two. It is not an accounting total.
- `additional_disruption_probability`: probability that random absence, no-show, emergency load, window violation or overtime-limit breach disrupts the published plan. Known baseline unserved work is excluded from this probability.
- `baseline_unserved_orders`: work already unserved by the published plan.
- `expected_total_unserved_orders`: baseline unserved work plus simulated additional loss.

Decision figures are valid only for the recorded snapshot, complete schedule hash, travel model, policy, algorithm version, build SHA, execution watermark, and input fingerprint. The implemented scope is `EX_ANTE_FROZEN_PLAN`: it excludes actual execution and is not a remaining-work forecast.

The risk field `monte_carlo_mean_ci_low/high` is an interval for simulation mean error, not a confidence interval for uncertain real-world parameters. `full_day_total_late_minutes_p50/p90/p95` is the percentile of total late minutes across a simulated service day, not the percentile of individual work-order lateness. Absence, customer no-show, time-window, overtime, and emergency-capacity disruption probabilities are reported separately.

Capacity `option_applicable` says whether an input change can be constructed. `schedule_feasible` says whether the resulting complete schedule passes coverage, uniqueness, skill, time-window, travel-continuity, lock, fixed-assignment, real-depot-return, and overtime checks. The compatibility field `feasible` is true only when both are true. One-time investment, daily operating delta, analysis-horizon impact, and break-even days use the returned cost cadence and must not be added without a common horizon.
