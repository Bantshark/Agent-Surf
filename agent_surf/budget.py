"""Fix 30: wall-clock budgets and sleep detection for long-running commands.

A command runs inside ``limit(kind)``; every wait, scroll and poll loop calls
``check()``. It raises BudgetExceeded when the budget is spent, or
MachineSlept when the wall clock advanced more than SLEEP_GAP_S beyond the
monotonic clock between two checks (the machine slept; on POSIX the monotonic
clock stops in suspend). With no active budget ``check()`` does nothing, so
library calls and tests are unaffected unless they opt in.

Budgets (seconds): run/inbox 900, learn/learn-action 1200, publish 600;
override with AGENT_SURF_MAX_RUN_S, AGENT_SURF_MAX_LEARN_S,
AGENT_SURF_MAX_PUBLISH_S.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping

log = logging.getLogger("agent_surf.budget")

DEFAULTS = {"run": 900.0, "learn": 1200.0, "publish": 600.0}
ENV = {"run": "AGENT_SURF_MAX_RUN_S", "learn": "AGENT_SURF_MAX_LEARN_S",
       "publish": "AGENT_SURF_MAX_PUBLISH_S"}
SLEEP_GAP_S = 60.0


class BudgetExceeded(RuntimeError):
    """The command's wall-clock budget is spent. ``reason``: "time_budget"
    or "machine_slept"."""

    reason = "time_budget"


class MachineSlept(BudgetExceeded):
    reason = "machine_slept"


def seconds_for(kind: str, env: Mapping[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    raw = env.get(ENV[kind], "")
    try:
        value = float(raw) if raw.strip() else DEFAULTS[kind]
    except ValueError:
        log.warning("%s is not a number; using %.0f s", ENV[kind], DEFAULTS[kind])
        value = DEFAULTS[kind]
    return value if value > 0 else DEFAULTS[kind]


class Budget:
    def __init__(self, kind: str, seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.kind, self.seconds = kind, seconds
        self.clock, self.wall = clock, wall
        self.deadline = clock() + seconds
        self.last = (wall(), clock())
        self.suspended = 0

    def check(self) -> None:
        if self.suspended:
            return
        w, m = self.wall(), self.clock()
        w0, m0 = self.last
        self.last = (w, m)
        if (w - w0) - (m - m0) > SLEEP_GAP_S:
            log.warning("the machine slept during the run (%.0f s unaccounted); stopping",
                        (w - w0) - (m - m0))
            raise MachineSlept(f"the machine slept during the {self.kind}; stopped")
        if m >= self.deadline:
            raise BudgetExceeded(f"the {self.kind} time budget of {self.seconds:.0f} s was spent "
                                 f"(raise {ENV[self.kind]} to allow more)")


_stack: list[Budget] = []


def check() -> None:
    """Raise if the active budget is spent or the machine slept; else nothing."""
    if _stack:
        _stack[-1].check()


def active() -> Budget | None:
    return _stack[-1] if _stack else None


@contextmanager
def limit(kind: str, seconds: float | None = None, **clocks: Callable[[], float]) -> Iterator[Budget]:
    b = Budget(kind, seconds_for(kind) if seconds is None else seconds, **clocks)
    _stack.append(b)
    try:
        yield b
    finally:
        _stack.remove(b)


def suspend_rest() -> None:
    """No more budget checks for the active budget (after a submit click the
    outcome must be resolved; its waits have their own timeouts)."""
    b = active()
    if b is not None:
        b.suspended += 1


@contextmanager
def suspended() -> Iterator[None]:
    """No budget checks inside (e.g. after a submit: the outcome must be
    resolved, with its own timeouts)."""
    b = active()
    if b is not None:
        b.suspended += 1
    try:
        yield
    finally:
        if b is not None:
            b.suspended -= 1
