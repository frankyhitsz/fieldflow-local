# Data format

The public JSON schema is available from the running service at `/openapi.json`. The main aggregate is `ScheduleScenario` in `backend/models.py`.

Times are integer minutes from the start of the planning date. Values above 1440 represent the next day; for example, 1500 is next day 01:00. Coordinates use the closed range 0–100.

There is no supported bulk import command in version 0.2.0. Editing `fieldflow.db` by hand bypasses aggregate validation and is not supported.

Work-order `status` is read-only in the generic update API. Use the `/start` and `/complete` action routes shown in OpenAPI. Each request identifies the assigned technician, occurrence minute, expected scenario revision, and idempotency key. The resulting execution event can be read from `/api/scenarios/{id}/execution-events`.
