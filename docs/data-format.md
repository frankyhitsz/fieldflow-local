# Data format

The public JSON schema is available from the running service at `/openapi.json`. The main aggregate is `ScheduleScenario` in `backend/models.py`.

Times are integer minutes from the start of the planning date. Values above 1440 represent the next day; for example, 1500 is next day 01:00. Coordinates use the closed range 0–100.

There is no supported bulk import command. Editing `fieldflow.db` by hand bypasses aggregate validation and is not supported.

Technician labor cost is serialized as `cost_per_minute_cents`, an integer number of CNY cents. Schema v10 converts legacy `cost_per_minute` floating values in current and historical snapshots before they are read by the application.

Work-order `status` is read-only in the generic update API. Use the `/start` and `/complete` action routes shown in OpenAPI. Each request identifies the assigned technician, occurrence minute, expected scenario revision, and idempotency key. The resulting execution event can be read from `/api/scenarios/{id}/execution-events`.
