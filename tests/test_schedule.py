from datetime import time
from datetime import datetime, timedelta, timezone

from t6_notion_reports.schedule import MANILA, Scheduler, next_run


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


def test_briefing_fires_sunday_16_manila_when_enabled():
    from t6_notion_reports.schedule import BRIEF_SLOT, SLOTS
    fake = FakeClock(manila(2026, 11, 8, 0, 0))
    fired = []
    end = manila(2026, 11, 16, 12, 0)
    Scheduler(lambda k: fired.append((fake.now, k)), fake.clock, fake.sleep, log=lambda _: None,
              slots=SLOTS + [BRIEF_SLOT]).run(stop=lambda: fake.now >= end)
    assert fired == [
        (manila(2026, 11, 8, 16), "brief"),
        (manila(2026, 11, 9, 11), "monday"),
        (manila(2026, 11, 13, 18), "friday"),
        (manila(2026, 11, 15, 16), "brief"),
        (manila(2026, 11, 16, 11), "monday"),
    ]


def test_briefing_is_off_by_default():
    assert "brief" not in [k for _, k in run_weeks()]


def test_every_five_minutes_slot_fires_on_the_clock_and_yields_to_weekly_slots():
    from t6_notion_reports.schedule import SEND_MOM_SLOT, SLOTS
    slots = SLOTS + [SEND_MOM_SLOT]
    assert next_run(manila(2026, 11, 10, 9, 3), slots) == (manila(2026, 11, 10, 9, 5), "send_mom")
    assert next_run(manila(2026, 11, 10, 9, 5), slots) == (manila(2026, 11, 10, 9, 10), "send_mom")
    assert next_run(manila(2026, 11, 10, 9, 59, 59), slots) == (manila(2026, 11, 10, 10), "send_mom")
    assert next_run(manila(2026, 11, 9, 10, 57), slots) == (manila(2026, 11, 9, 11), "monday")  # tie: weekly wins
    assert next_run(manila(2026, 11, 9, 11), slots) == (manila(2026, 11, 9, 11, 5), "send_mom")


def run_slots(slots, start, end, job=None):
    fake = FakeClock(start)
    fired = []

    def default(kind):
        fired.append((fake.now, kind))

    Scheduler(job(fired, fake) if job else default, fake.clock, fake.sleep, log=lambda _: None,
              slots=slots).run(stop=lambda: fake.now >= end)
    return fired


def test_daily_slot_fires_every_day_including_weekend():
    from t6_notion_reports.schedule import Daily
    fired = run_slots([Daily(time(12, 0), "noon")], manila(2026, 11, 9), manila(2026, 11, 16))
    assert fired == [(manila(2026, 11, d, 12), "noon") for d in range(9, 16)]
    assert {t.weekday() for t, _ in fired} == set(range(7))


def test_daily_slot_rolls_to_next_day_and_is_strictly_after():
    from t6_notion_reports.schedule import Daily
    slots = [Daily(time(5, 0), "early")]
    assert next_run(manila(2026, 11, 14, 5), slots) == (manila(2026, 11, 15, 5), "early")
    assert next_run(manila(2026, 11, 14, 4, 59), slots) == (manila(2026, 11, 14, 5), "early")


def test_two_slots_on_same_minute_both_run_in_declared_order_once():
    from t6_notion_reports.schedule import SLOTS, Daily
    slots = SLOTS + [Daily(time(18, 0), "shift18")]
    fired = run_slots(slots, manila(2026, 11, 13, 17), manila(2026, 11, 14, 17))
    assert fired == [(manila(2026, 11, 13, 18), "friday"), (manila(2026, 11, 13, 18), "shift18")]


def test_declared_order_wins_over_other_order():
    from t6_notion_reports.schedule import SLOTS, Daily
    slots = [Daily(time(18, 0), "shift18")] + SLOTS
    fired = run_slots(slots, manila(2026, 11, 13, 17), manila(2026, 11, 14, 17))
    assert [k for _, k in fired] == ["shift18", "friday"]


def test_failing_first_job_does_not_stop_second_at_shared_minute():
    from t6_notion_reports.schedule import SLOTS, Daily
    slots = SLOTS + [Daily(time(18, 0), "shift18")]
    ran = []

    def job(fired, fake):
        def run(kind):
            ran.append(kind)
            if kind == "friday":
                raise RuntimeError("x")
        return run

    run_slots(slots, manila(2026, 11, 13, 17), manila(2026, 11, 13, 19), job)
    assert ran == ["friday", "shift18"]
