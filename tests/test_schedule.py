from datetime import datetime, timedelta, timezone

from t6_psr_brief.schedule import MANILA, Scheduler, next_run


class FakeClock:
    def __init__(self, start, step=None):
        self.now, self.step = start, step

    def clock(self):
        return self.now

    def sleep(self, seconds):
        # step caps each sleep, mimicking a process that wakes early
        self.now += timedelta(seconds=min(seconds, self.step) if self.step else seconds)


def manila(*a):
    return datetime(*a, tzinfo=MANILA)


def run_weeks(step=None):
    fake = FakeClock(manila(2026, 11, 8, 0, 0), step)
    fired = []
    end = manila(2026, 11, 21, 0, 0)
    Scheduler(lambda k: fired.append((fake.now, k)), fake.clock, fake.sleep, log=lambda _: None,
              max_sleep=3600).run(stop=lambda: fake.now >= end)
    return fired


def test_fires_monday_11_and_friday_18_manila_and_nowhere_else():
    assert run_weeks() == [
        (manila(2026, 11, 9, 11), "monday"),
        (manila(2026, 11, 13, 18), "friday"),
        (manila(2026, 11, 16, 11), "monday"),
        (manila(2026, 11, 20, 18), "friday"),
    ]


def test_early_wakeups_do_not_fire_early_or_twice():
    assert [k for _, k in run_weeks(step=600)] == ["monday", "friday", "monday", "friday"]


def test_next_run_is_strictly_after_and_works_from_utc():
    mon = manila(2026, 11, 9, 11)
    assert next_run(mon) == (manila(2026, 11, 13, 18), "friday")
    assert next_run(mon - timedelta(seconds=1)) == (mon, "monday")
    # Sunday 20:00 UTC is Monday 04:00 in Manila
    assert next_run(datetime(2026, 11, 8, 20, tzinfo=timezone.utc))[1] == "monday"


def test_failed_job_is_logged_without_message_and_loop_continues():
    fake = FakeClock(manila(2026, 11, 8))
    logs = []

    def boom(kind):
        raise RuntimeError("secret note text")

    end = manila(2026, 11, 10)
    Scheduler(boom, fake.clock, fake.sleep, log=logs.append).run(stop=lambda: fake.now >= end)
    assert any("failed kind=monday error=RuntimeError" in line for line in logs)
    assert not any("secret" in line for line in logs)
