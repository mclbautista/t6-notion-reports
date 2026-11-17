"""``concern record-message`` after the brief is sent (#132): the Concern IDs of each message, the poll for the Telegram
id, and a step that never changes what the brief does."""
import pytest

from fake_api import FakeConcerns, down, refused
from fake_notion import FakeNotion, date
from t6_notion_reports.brief_job import RECORD_CAP, RECORD_TRIES, RECORD_WAIT, run_brief
from t6_notion_reports.config import load_config
from t6_notion_reports.gateway import GatewayClient, GatewayError
from test_flags import staffed
from test_gated_briefs import BRIEF, IN_PROGRESS, SATURDAY, Gateway, contacts, make_db, proj
from test_gateway import FakeTransport, client
from test_scan_and_raise import T6, quiet, world


class IdGateway(Gateway):
    """Sends like the fake gateway; ``ids`` gives the Telegram id of a message_ref (a missing one is not sent yet)."""

    def __init__(self, ids=None, fail_for=()):
        super().__init__()
        self.ids, self.polls, self.fail_for = ids, [], set(fail_for)

    def notify(self, to, text, key, topic=None):
        if to in self.fail_for:
            raise GatewayError("down")
        return super().notify(to, text, key, topic)

    def sent_message_id(self, ref):
        self.polls.append(ref)
        return 9000 + ref if self.ids is None else self.ids.get(ref)


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


SLIP = {"Playdate": date("2026-12-10"), "Submission Deadline": date("2026-12-01")}  # a date-sanity Concern


def unstaffed(*codes):
    """Projects with a Concern each (a submission before the playdate). Two coordinators, so a DM each."""
    pages = [proj(f"p{n}", code, IN_PROGRESS, coordinator="c-oto" if n == 1 else "c-ida", **staffed(**SLIP))
             for n, code in enumerate(codes, 1)]
    return world(*pages)


def go(conn, notion, gw, clock=None, brief=BRIEF, **kw):
    clock = clock or Clock()
    logs = []
    res = run_brief(conn, notion, brief, SATURDAY, gateway=gw, log=logs.append, monotonic=clock.monotonic, sleep=clock.sleep, **kw)
    return res, logs, clock


def recorded(notion):
    return notion.api.concerns.recorded


def last(logs):
    return next(ln for ln in reversed(logs) if ln.startswith("brief record-message ok="))


# ---- the gateway read ----

def test_sent_message_id_is_the_telegram_id_of_a_sent_message():
    t = FakeTransport((200, {"state": "sent", "telegram_message_id": 4412}))
    assert client(t).sent_message_id(5531) == 4412
    url, headers, body = t.calls[0]
    assert url == "http://gw:8780/notify/5531" and headers["Authorization"] == "Bearer s3cret" and body is None


@pytest.mark.parametrize("reply", [
    (200, {"state": "queued"}), (200, {"state": "sent"}), (200, {"state": "sent", "telegram_message_id": "x"}),
    (200, {"state": "sent", "telegram_message_id": True}), (404, None), (500, {"state": "sent", "telegram_message_id": 1}),
    (200, "text"), ConnectionError("down"), ValueError("bad"),
])
def test_sent_message_id_is_none_and_never_raises_on_any_problem(reply):
    t = FakeTransport(reply)
    assert client(t).sent_message_id(1) is None and len(t.calls) == 1


# ---- the step ----

def test_each_concern_is_recorded_once_with_the_chat_the_telegram_id_the_date_and_the_key():
    conn, notion = unstaffed("ONE", "TWO")
    gw = IdGateway()
    res, logs, _ = go(conn, notion, gw)
    assert res.sent == len(gw.sent) == 3
    rows = recorded(notion)
    assert len(rows) == 4 and len({r[4] for r in rows}) == 4  # each Concern once for each message that carried it (DM and digest)
    by_key = {key: (to, 9000 + n) for n, (to, _, key, _) in enumerate(gw.sent, 1)}
    for concern, chat, message_id, brief_date, key in rows:
        message_key = next(k for k in by_key if key.startswith(f"psr-brief-2026-11-14-{k}-"))
        assert key == f"psr-brief-2026-11-14-{message_key}-{concern}"
        assert (int(chat), int(message_id)) == by_key[message_key] and brief_date == "2026-11-14"
    assert last(logs) == f"brief record-message ok={len(rows)} missing_id=0 refused=0"


def test_a_message_with_concerns_of_two_projects_records_each_concern_once():
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS, **staffed(**SLIP)), proj("p2", "TWO", IN_PROGRESS, **staffed(**SLIP)))  # one coordinator, two projects
    gw = IdGateway()
    go(conn, notion, gw)
    owned = [r for r in recorded(notion) if r[4].startswith("psr-brief-2026-11-14-notion-reports-2026-11-14-c-oto")]
    projects = {c["project"] for c in notion.api.concerns.items}
    assert projects == {"t6_proj_one", "t6_proj_two"}
    assert len(owned) == len(notion.api.concerns.items) and len({r[0] for r in owned}) == len(owned)


def test_a_raise_at_psr_concern_is_recorded_with_the_message_that_shows_it():
    store = FakeConcerns()
    conn, notion = world(proj("p1", "ONE", IN_PROGRESS), store=store)
    quiet(store)
    gw = IdGateway()
    go(conn, notion, gw)
    concern = store.items[0]["id"]
    carriers = {r[1:3] for r in store.recorded if r[0] == concern}
    shown = {(str(to), str(9000 + n)) for n, (to, _, _, text) in enumerate(gw.sent, 1) if "Raise at PSR" in text and "Vendor quote" in text}
    assert carriers == shown and carriers


def test_the_message_id_is_polled_until_the_first_id_with_ten_seconds_between_polls():
    conn, notion = unstaffed("ONE")
    seen = []

    class Late(IdGateway):
        def sent_message_id(self, ref):
            seen.append(ref)
            return None if len(seen) < 3 else 7

    res, logs, clock = go(conn, notion, Late())
    assert seen[:3] == [seen[0]] * 3 and clock.sleeps[:2] == [RECORD_WAIT, RECORD_WAIT]
    assert RECORD_TRIES == 6 and RECORD_WAIT == 10 and RECORD_CAP == 120
    assert recorded(notion) and "missing_id=0" in last(logs)


def test_an_id_that_never_comes_is_counted_after_six_tries_and_nothing_is_recorded():
    conn, notion = unstaffed("ONE")
    gw = IdGateway(ids={})
    res, logs, clock = go(conn, notion, gw)
    assert recorded(notion) == [] and res.sent == 2 and res.failed == 0
    assert gw.polls.count(gw.polls[0]) == 6 and len(clock.sleeps) >= 5
    ok, missing, refused_n = (int(x.split("=")[1]) for x in last(logs).split()[2:])
    assert ok == 0 and missing > 0 and refused_n == 0


def test_a_refused_call_is_counted_with_its_rule_name_only():
    conn, notion = unstaffed("ONE")
    notion.api.fail["concern record-message"] = refused("no_such_concern")
    res, logs, _ = go(conn, notion, IdGateway())
    assert recorded(notion) == [] and res.sent == 2
    assert "ok=0 missing_id=0 refused=" in last(logs) and "refused=0" not in last(logs)
    assert any("brief record-message failed error=concern record-message refused rule=no_such_concern" == ln for ln in logs)


def test_an_unreachable_api_is_counted_as_refused_and_the_brief_still_succeeds():
    conn, notion = unstaffed("ONE")
    notion.api.fail["concern record-message"] = down()
    res, logs, _ = go(conn, notion, IdGateway())
    assert res.sent == 2 and res.failed == 0 and "refused=0" not in last(logs)


def test_the_total_time_cap_stops_the_step():
    conn, notion = unstaffed("ONE", "TWO")
    gw = IdGateway(ids={})  # polls forever: the clock runs 10 s a poll
    res, logs, clock = go(conn, notion, gw)
    assert clock.now <= RECORD_CAP + RECORD_WAIT
    assert len(gw.polls) < 3 * RECORD_TRIES  # the cap stopped it before every message had its six tries
    assert res.sent == 3


def test_a_dry_run_and_no_gateway_call_nothing():
    conn, notion = unstaffed("ONE")
    gw = IdGateway()
    res, logs, _ = go(conn, notion, gw, dry_print=True, out=lambda _: None)
    assert gw.polls == [] and "concern record-message" not in notion.api.calls and not any("record-message" in ln for ln in logs)
    conn2, notion2 = unstaffed("ONE")
    go(conn2, notion2, None)
    assert "concern record-message" not in notion2.api.calls


def test_nothing_in_the_step_changes_the_result_raises_or_sends_twice():
    conn, notion = unstaffed("ONE", "TWO")
    plain, plain_logs, _ = go(conn, notion, Gateway())  # a gateway that cannot answer the poll at all: the step is skipped
    conn2, notion2 = unstaffed("ONE", "TWO")

    class Broken(IdGateway):
        def sent_message_id(self, ref):
            raise RuntimeError("boom")

    gw = Broken()
    res, logs, _ = go(conn2, notion2, gw)
    assert (res.sent, res.failed, len(res.messages)) == (plain.sent, plain.failed, len(plain.messages))
    assert len(gw.sent) == len({k for _, _, k, _ in gw.sent}) == res.sent  # no second notify
    assert any(ln.startswith("brief record-message failed error=RuntimeError") for ln in logs)


def test_the_send_loop_and_the_sent_counts_are_what_they_were():
    conn, notion = unstaffed("ONE", "TWO")
    gw = IdGateway(fail_for={222})
    with pytest.raises(Exception) as err:
        go(conn, notion, gw)
    assert "1 of 3 messages were not queued" in str(err.value)
    assert [(to, key) for to, _, key, _ in gw.sent] == [(111, "notion-reports-2026-11-14-c-oto"), (999, "psr-digest-2026-11-14")]
    assert all(r[1] in ("111", "999") for r in recorded(notion))  # the failed message has no ref: nothing to record


def test_the_messages_carry_the_concern_ids_they_show():
    conn, notion = unstaffed("ONE", "TWO")
    res, _, _ = go(conn, notion, IdGateway())
    ids = {i for m in res.messages for i in m.concern_ids}
    assert ids == {c["id"] for c in notion.api.concerns.items}
    assert all(m.concern_ids for m in res.messages)


def test_when_a_brief_splits_each_message_carries_only_the_ids_of_its_own_concern_lines():
    from datetime import date
    from t6_notion_reports.briefing import Settings, build_briefing
    from t6_notion_reports.contacts import Contact
    from t6_notion_reports.diff import Person, ProjectChange

    people = [Person("c-oto", "Oto")]
    changes = [
        ProjectChange(f"p{n}", f"P{n:02d}", "unchanged", [], people, group="in_progress",
                      concerns=tuple(f"Concern {n}-{k} " + "x" * 300 for k in range(3)),
                      concern_ids=tuple(f"t6c_{n}_{k}" for k in range(3)))
        for n in range(1, 13)
    ]
    msgs = build_briefing(changes, {"c-oto": "Oto"}, {"c-oto": Contact("c-oto", "Oto", None, 111)}, Settings(None, -1, None), date(2026, 11, 14))
    assert len(msgs) > 1
    for m in msgs:
        shown = {f"t6c_{n}_{k}" for n in range(1, 13) for k in range(3) if f"Concern {n}-{k} " in m.text}
        assert set(m.concern_ids) == shown
    assert sorted(i for m in msgs for i in m.concern_ids) == sorted(f"t6c_{n}_{k}" for n in range(1, 13) for k in range(3))
