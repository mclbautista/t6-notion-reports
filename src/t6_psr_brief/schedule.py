"""When jobs fire, Asia/Manila: snapshots Monday 11:00 and Friday 18:00, briefing Sunday 16:00."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

MANILA = ZoneInfo("Asia/Manila")
# (weekday with Monday=0, local time, snapshot kind)
SLOTS = [(0, time(11, 0), "monday"), (4, time(18, 0), "friday")]
BRIEF_SLOT = (6, time(16, 0), "brief")


@dataclass(frozen=True)
class Every:
    """A slot that fires every ``minutes`` on the local clock (minute 0, 5, 10... for 5)."""

    minutes: int
    kind: str


# When it lands on the same minute as a weekly slot the weekly one wins, and this check runs five
# minutes later.
SEND_MOM_SLOT = Every(5, "send_mom")


def next_run(after: datetime, slots=SLOTS) -> tuple[datetime, str]:
    """The first slot strictly after ``after`` (an aware datetime), and its kind."""
    local = after.astimezone(MANILA)
    best: tuple[datetime, str] | None = None
    for day in range(8):
        date = (local + timedelta(days=day)).date()
        for weekday, at, kind in (s for s in slots if not isinstance(s, Every)):
            if date.weekday() != weekday:
                continue
            slot = datetime.combine(date, at, tzinfo=MANILA)
            if slot > after and (best is None or slot < best[0]):
                best = (slot, kind)
    for slot in slots:
        if isinstance(slot, Every):
            base = local.replace(second=0, microsecond=0)
            base += timedelta(minutes=slot.minutes - base.minute % slot.minutes)
            if best is None or base < best[0]:
                best = (base, slot.kind)
    assert best is not None
    return best


class Scheduler:
    """Runs ``job(kind)`` at each slot. Clock and sleep are injected so tests need no real time."""

    def __init__(self, job: Callable[[str], None], clock: Callable[[], datetime],
                 sleep: Callable[[float], None], log: Callable[[str], None] = print,
                 max_sleep: float = 3600, slots=SLOTS):
        self._slots = slots
        self._job, self._clock, self._sleep, self._log = job, clock, sleep, log
        self._max_sleep = max_sleep

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            when, kind = next_run(self._clock(), self._slots)
            self._log(f"scheduler next={when.isoformat()} kind={kind}")
            while not stop() and self._clock() < when:
                self._sleep(min(self._max_sleep, (when - self._clock()).total_seconds()))
            if stop():
                return
            try:
                self._job(kind)
            except Exception as exc:  # keep the loop alive; type only, messages may hold data
                self._log(f"job failed kind={kind} error={type(exc).__name__}")
            # A job that returns before the slot ends must not fire it again.
            while self._clock() <= when:
                self._sleep(1)
