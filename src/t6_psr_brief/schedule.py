"""When snapshots fire: Monday 11:00 and Friday 18:00, Asia/Manila."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MANILA = ZoneInfo("Asia/Manila")
# (weekday with Monday=0, local time, snapshot kind)
SLOTS = [(0, time(11, 0), "monday"), (4, time(18, 0), "friday")]


def next_run(after: datetime) -> tuple[datetime, str]:
    """The first slot strictly after ``after`` (an aware datetime), and its snapshot kind."""
    local = after.astimezone(MANILA)
    best: tuple[datetime, str] | None = None
    for day in range(8):
        date = (local + timedelta(days=day)).date()
        for weekday, at, kind in SLOTS:
            if date.weekday() != weekday:
                continue
            slot = datetime.combine(date, at, tzinfo=MANILA)
            if slot > after and (best is None or slot < best[0]):
                best = (slot, kind)
    assert best is not None
    return best


class Scheduler:
    """Runs ``job(kind)`` at each slot. Clock and sleep are injected so tests need no real time."""

    def __init__(self, job: Callable[[str], None], clock: Callable[[], datetime],
                 sleep: Callable[[float], None], log: Callable[[str], None] = print,
                 max_sleep: float = 3600):
        self._job, self._clock, self._sleep, self._log = job, clock, sleep, log
        self._max_sleep = max_sleep

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            when, kind = next_run(self._clock())
            self._log(f"scheduler next={when.isoformat()} kind={kind}")
            while not stop() and self._clock() < when:
                self._sleep(min(self._max_sleep, (when - self._clock()).total_seconds()))
            if stop():
                return
            try:
                self._job(kind)
            except Exception as exc:  # keep the loop alive; type only, messages may hold data
                self._log(f"snapshot failed kind={kind} error={type(exc).__name__}")
            # A job that returns before the slot ends must not fire it again.
            while self._clock() <= when:
                self._sleep(1)
