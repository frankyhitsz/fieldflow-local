from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class JobAuthority:
    job_id: str
    owner: str
    generation: int


CURRENT_JOB: ContextVar[JobAuthority | None] = ContextVar("fieldflow_job_authority", default=None)


@contextmanager
def job_authority(authority: JobAuthority):
    token = CURRENT_JOB.set(authority)
    try:
        yield
    finally:
        CURRENT_JOB.reset(token)
