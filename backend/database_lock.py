"""Stable advisory locks shared by the API, workers and maintenance CLI."""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

APPLICATION_INSTANCE_LOCK = threading.Lock()
_exclusive_paths: ContextVar[frozenset[str]] = ContextVar("fieldflow_maintenance_paths", default=frozenset())
_coordinator_paths: ContextVar[frozenset[str]] = ContextVar("fieldflow_coordinator_paths", default=frozenset())


@contextmanager
def database_access(path: str | Path, *, exclusive: bool = False, coordinator: bool = False):
    canonical = str(Path(path).resolve())
    if coordinator and canonical in _coordinator_paths.get():
        yield
        return
    if canonical in _exclusive_paths.get() and not coordinator:
        yield
        return
    lock_path = Path(canonical + (".coordinator.lock" if coordinator else ".access.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive or coordinator else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"DATABASE_IN_USE: {canonical}；请停止使用该库的应用和后台任务后执行维护") from error
        token = _exclusive_paths.set(_exclusive_paths.get() | {canonical}) if exclusive and not coordinator else None
        coordinator_token = _coordinator_paths.set(_coordinator_paths.get() | {canonical}) if coordinator else None
        try:
            yield
        finally:
            if token is not None:
                _exclusive_paths.reset(token)
            if coordinator_token is not None:
                _coordinator_paths.reset(coordinator_token)
            fcntl.flock(handle, fcntl.LOCK_UN)
