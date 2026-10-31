"""U18 process-local infrastructure breaker; all deadlines use monotonic time."""
from __future__ import annotations

from threading import RLock
from time import monotonic
from typing import Callable, TypeVar
from weakref import WeakKeyDictionary

from sqlalchemy.exc import OperationalError

from agentbank.db import Database
from agentbank.errors import DomainError

T = TypeVar("T")
_states: WeakKeyDictionary[Database, dict[str, tuple[int, float]]] = WeakKeyDictionary()
_lock = RLock()


def snapshot(database: Database) -> dict[str, object]:
    with _lock:
        current = monotonic()
        observed = _states.get(database, {})
        return {'scope': 'process-local tool infrastructure failures',
                'tools': [{'tool': tool, 'consecutive_failures': failures,
                           'open': until > current, 'remaining_seconds': max(0, int(until - current))}
                          for tool, (failures, until) in sorted(observed.items())]}


def guarded_call(database: Database, tool: str, action: Callable[[], T], *, read_only: bool) -> T:
    if read_only:
        return action()
    with _lock:
        state = _states.setdefault(database, {})
        failures, until = state.get(tool, (0, 0.0))
        if until > monotonic():
            raise DomainError("TOOL_CIRCUIT_OPEN")
        if until:
            state[tool] = (0, 0.0)
    try:
        result = action()
    except (OSError, TimeoutError, ConnectionError, OperationalError):
        with _lock:
            failures = state.get(tool, (0, 0.0))[0] + 1
            state[tool] = (failures, monotonic() + 60 if failures >= 3 else 0.0)
        raise
    with _lock:
        state[tool] = (0, 0.0)
    return result
