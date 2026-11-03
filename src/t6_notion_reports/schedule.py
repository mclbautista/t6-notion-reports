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


@dataclass(frozen=True)
class Daily:
    """A slot that fires every day (weekends and holidays included) at a fixed Manila time."""

    at: time
    kind: str


# JO shift reports: snapshot Job Orders, then build and send that slot's report. Kind "shift<slot>".
# Declared after SLOTS, so on Friday 18:00 the Projects snapshot runs first.
SHIFT_SLOTS = [Daily(time(5, 0), "shift0500"), Daily(time(12, 0), "shift1200"), Daily(time(18, 0), "shift1800")]


def next_batch(after: datetime, slots=SLOTS) -> tuple[datetime, list[str]]:
    """The first minute strictly after ``after`` with a slot, and every kind due then.

    Kinds come in declared order. An ``Every`` slot yields to weekly and daily slots on the same
    minute, as it always has.
    """
    local = after.astimezone(MANILA)
    fixed: list[tuple[datetime, int, str]] = []
    for index, slot in enumerate(slots):
        if isinstance(slot, Every):
            continue
        for day in range(8):
            date = (local + timedelta(days=day)).date()
            if isinstance(slot, Daily):
                at, kind = slot.at, slot.kind
            else:
                weekday, at, kind = slot
                if date.weekday() != weekday:
                    continue
            when = datetime.combine(date, at, tzinfo=MANILA)
            if when > after:
                fixed.append((when, index, kind))
                break
    candidates = list(fixed)
    for index, slot in enumerate(slots):
        if isinstance(slot, Every):
            base = local.replace(second=0, microsecond=0)
            base += timedelta(minutes=slot.minutes - base.minute % slot.minutes)
            candidates.append((base, index, slot.kind))
    assert candidates
    when = min(c[0] for c in candidates)
    pool = fixed if any(c[0] == when for c in fixed) else candidates
    return when, [kind for w, _, kind in sorted(pool, key=lambda c: c[1]) if w == when]


def next_run(after: datetime, slots=SLOTS) -> tuple[datetime, str]:
    """The first slot strictly after ``after`` (an aware datetime), and its kind."""
    when, kinds = next_batch(after, slots)
    return when, kinds[0]


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
            when, kinds = next_batch(self._clock(), self._slots)
            self._log(f"scheduler next={when.isoformat()} kind={'+'.join(kinds)}")
            while not stop() and self._clock() < when:
                self._sleep(min(self._max_sleep, (when - self._clock()).total_seconds()))
            if stop():
                return
            for kind in kinds:  # a failure must not stop the next job at the same minute
                try:
                    self._job(kind)
                except Exception as exc:  # keep the loop alive; type only, messages may hold data
                    self._log(f"job failed kind={kind} error={type(exc).__name__}")
            # A job that returns before the slot ends must not fire it again.
            while self._clock() <= when:
                self._sleep(1)
