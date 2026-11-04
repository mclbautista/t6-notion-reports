"""Leave clash Concerns (#49), through the brief job with a fake Notion, LLM and gateway."""
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from fake_notion import CONTACTS, PROJECTS, FakeNotion, contact, date, relation, rich
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.llm import LlmError
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA
from test_gated_briefs import BRIEF, FRIDAY, IN_PROGRESS, MONDAY, SATURDAY, STANDBY, Gateway, digest, dm, proj, tg

HEADING = "Upcoming Holidays and Leaves"
LEAVES = [
    "• Lea - leave Nov 20-22",
    "• Ben - leave Dec 20",
    "• Nov 30 - Bonifacio Day (holiday)",
    "• Zed - leave Nov 25",
    "• Lea - holiday Nov 21",
]


def contacts():
    people = [contact("c-pio", "Pio"), contact("c-cyril", "Cyril"), contact("c-lea", "Lea Santos"),
              contact("c-ben", "Ben Reyes"), contact("c-ann", "Ann Cruz")]
    for page, (h, i) in zip(people[:4], (("pio_h", 111), ("cyril_h", 999), ("lea_h", 333), ("ben_h", 444))):
        page["properties"].update(tg(h, i))
    people[2]["properties"]["Aliases"] = rich("Lei")
    return people  # Ann has no TelegramUserID


class LeaveLlm:
    """Answers the leave call from a script, and records every payload it is sent."""

    def __init__(self, entries=None, fail=False):
        self.entries, self.fail, self.payloads = entries, fail, []

    def complete(self, system, user, schema):
        payload = json.loads(user)
        if schema.__name__ != "LeaveParse":
            return schema()
        self.payloads.append(payload)
        if self.fail:
            raise LlmError("down")
        entries = self.entries(payload) if callable(self.entries) else (self.entries or [])
        return schema(entries=entries)


def entry(who, start, end, quote, kind="leave"):
    return {"who": who, "kind": kind, "start": start, "end": end, "quote": quote}


STANDARD = [
    entry("Lea Santos", "2026-11-20", "2026-11-22", "Lea - leave Nov 20-22"),
    entry("Ben Reyes", "2026-12-20", "2026-12-20", "Ben - leave Dec 20"),
    entry("ALL", "2026-11-30", "2026-11-30", "Nov 30 - Bonifacio Day (holiday)", "holiday"),
]


def build(projects, description="\n".join([HEADING, *LEAVES])):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(projects, contacts()), PROJECTS, CONTACTS, "monday", MONDAY)
    run_snapshot(conn, FakeNotion(projects, contacts()), PROJECTS, CONTACTS, "friday", FRIDAY)
    return conn, FakeNotion([], contacts(), description=description)


def team_project(status=IN_PROGRESS, **props):
    base = {"Submission Deadline": date("2026-11-30"), "Playdate": date("2026-11-28"), "Editors": relation("c-lea")}
    base.update(props)
    return proj("p1", "ONE", status, **base)


def brief(conn, notion, llm, brief_settings=BRIEF, **kw):
    gw, logs = Gateway(), []
    run_brief(conn, notion, CONTACTS, brief_settings, SATURDAY, gateway=gw, llm=llm, log=logs.append,
              projects_ds=PROJECTS, **kw)
    return gw, logs


def test_a_team_members_leave_near_a_project_date_is_one_clash_concern_in_the_dm_and_the_digest():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    for text in (dm(gw), digest(gw)):
        assert text.count("Lea Santos is on leave Nov 20–22") == 1
        assert "Playdate (Nov 28, 2026)" in text and "since Nov 14" in text  # the nearest of the dates


def test_a_leave_by_someone_not_on_the_project_produces_nothing():
    conn, notion = build([team_project(Editors=relation("c-ben"))])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "on leave" not in dm(gw) + digest(gw)


def test_a_leave_outside_the_window_of_every_project_date_produces_nothing():
    late = {"Playdate": date("2026-12-01"), "Submission Deadline": date("2026-12-05")}
    conn, notion = build([team_project(Editors=relation("c-ben"), **late)])  # Ben's leave is Dec 20: 15 days after Dec 5
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Ben" not in dm(gw)
    conn, notion = build([team_project(Editors=relation("c-ben"), **{**late, "Submission Deadline": date("2026-12-06")})])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Ben Reyes is on leave Dec 20" in dm(gw)  # 14 days: inside


def test_the_window_comes_from_config():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD), replace(BRIEF, leave_clash_days=5))
    assert "on leave" not in dm(gw)  # Nov 22 is 8 days before Nov 30


def test_holidays_and_office_closures_produce_no_clash():
    everyone = [entry("ALL", "2026-11-20", "2026-11-22", "Lea - holiday Nov 21", "holiday"),
                entry("Lea", "2026-11-21", "2026-11-21", "Lea - holiday Nov 21", "holiday")]
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(everyone))
    assert "on leave" not in dm(gw) + digest(gw)


def test_an_entry_whose_quote_is_not_an_exact_line_or_whose_dates_are_invalid_is_dropped():
    bad = [
        entry("Lea", "2026-11-20", "2026-11-22", "Lea is away around Nov 20"),  # not a line of the input
        entry("Lea", "2026-11-31", "2026-11-31", "Lea - leave Nov 20-22"),  # no such day
        entry("Lea", "2026-11-22", "2026-11-20", "Lea - leave Nov 20-22"),  # ends before it starts
        entry("Lea", "soon", "later", "Lea - leave Nov 20-22"),
    ]
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(bad))
    assert "on leave" not in dm(gw)
    assert "Unmatched" not in digest(gw) and "Leaves unavailable" not in digest(gw)
    good = entry("Lea", "2026-11-20", "2026-11-22", "Lea - leave Nov 20-22")
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(bad + [good]))
    assert dm(gw).count("on leave") == 1  # one bad entry does not void the rest


def test_a_quote_may_carry_the_bullet_mark_but_not_altered_spacing_inside_the_line():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Lea", "2026-11-20", "2026-11-22", "•  Lea - leave Nov 20-22 ")]))
    assert "Lea Santos is on leave" in dm(gw)
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Lea", "2026-11-20", "2026-11-22", "Lea -  leave Nov 20-22")]))
    assert "on leave" not in dm(gw)


def test_a_nickname_is_matched_through_the_contacts_alias():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Lei", "2026-11-20", "2026-11-22", "Lea - leave Nov 20-22")]))
    assert "Lea Santos is on leave" in dm(gw)


def test_an_onboarded_contact_wins_over_a_namesake_without_a_telegram_id():
    people = contacts() + [contact("c-lea2", "Lea Santos")]
    conn, _ = build([team_project(Editors=relation("c-lea2"))])
    notion = FakeNotion([], people, description="\n".join([HEADING, *LEAVES]))
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "on leave" not in dm(gw)  # the match is the onboarded Lea, who is not on this project
    conn, _ = build([team_project(Editors=relation("c-lea"))])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Lea Santos is on leave" in dm(gw)


def test_a_name_with_no_onboarded_match_falls_through_to_the_rest_of_contacts():
    conn, notion = build([team_project(Editors=relation("c-ann"))])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Ann Cruz", "2026-11-20", "2026-11-22", "Lea - leave Nov 20-22")]))
    assert "Ann Cruz is on leave" in dm(gw)


def test_unmatched_names_appear_only_in_the_digest():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD + [entry("Zed", "2026-11-25", "2026-11-25", "Zed - leave Nov 25")]))
    assert "Unmatched leave names (add an alias in Contacts): Zed" in digest(gw)
    assert "Zed" not in dm(gw)
    assert "Zed" not in "\n".join(t for to, _, _, t in gw.sent if to not in (999,))


def test_a_failed_parse_blocks_nothing_and_the_digest_says_so():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(fail=True))
    assert "on leave" not in dm(gw)
    assert "⚠️ Leaves unavailable: no leave clash Concerns this run" in digest(gw)
    assert "Leaves unavailable" not in dm(gw)
    assert dm(gw)  # the brief still reached the coordinator


def test_leaves_are_parsed_once_per_run_and_the_payload_holds_only_the_leave_lines_and_the_date():
    conn, notion = build([team_project(), proj("p2", "TWO", Editors=relation("c-lea"))])
    llm = LeaveLlm(STANDARD)
    brief(conn, notion, llm)
    assert len(llm.payloads) == 1
    payload = llm.payloads[0]
    assert set(payload) == {"today", "lines"} and payload["today"] == "2026-11-14"
    assert payload["lines"] == [ln.lstrip("• ") for ln in LEAVES]
    blob = json.dumps(payload)
    assert "Show ONE" not in blob and "c-lea" not in blob and "ONE" not in blob


def test_there_is_no_leave_call_without_leave_lines_and_no_note():
    conn, notion = build([team_project()], description=HEADING + "\n\n")
    llm = LeaveLlm(STANDARD)
    gw, _ = brief(conn, notion, llm)
    assert llm.payloads == []
    assert "Leaves unavailable" not in digest(gw)


def test_without_the_heading_nothing_is_sent_and_the_digest_says_leaves_are_unavailable():
    conn, notion = build([team_project()], description="Notes about the db\n• Lea - leave Nov 20-22")
    llm = LeaveLlm(STANDARD)
    gw, _ = brief(conn, notion, llm)
    assert llm.payloads == []
    assert "Leaves unavailable: no Upcoming Holidays and Leaves heading" in digest(gw)
    assert "Leaves unavailable" not in dm(gw)


def test_only_the_leaves_section_is_sent_and_a_later_heading_ends_it():
    plain = "\n".join([HEADING, *LEAVES, "", "Project planning", "• ONE needs Lea Santos on Dec 1 for the online"])
    rich_text = [
        {"plain_text": HEADING + "\n\nDon't forget the onboarding projects", "annotations": {"bold": True}},
        {"plain_text": "\n" + "\n".join(LEAVES) + "\n", "annotations": {"bold": False}},
        {"plain_text": "Staffing", "annotations": {"bold": True}},
        {"plain_text": "\nONE is short of an editor on Dec 3", "annotations": {"bold": False}},
    ]
    for description in (plain, rich_text, "\n".join([HEADING, *LEAVES, "Planning:", "• ONE on Dec 1"])):
        conn, notion = build([team_project()], description=description)
        llm = LeaveLlm(STANDARD)
        brief(conn, notion, llm)
        assert llm.payloads[0]["lines"] == [ln.lstrip("• ") for ln in LEAVES], description


def test_a_clash_concern_ages_through_the_store_and_resolves_when_the_leave_goes():
    conn, notion = build([team_project()])
    brief(conn, notion, LeaveLlm(STANDARD))
    first = brief(conn, notion, LeaveLlm(STANDARD))  # same day: no change
    assert dm(first[0]).count("since Nov 14") == 1
    notion.description = HEADING
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Resolved last week: Lea Santos is on leave" in dm(gw)


def test_a_standby_project_is_still_checked_for_its_dates():
    conn, notion = build([team_project(status=STANDBY)])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Lea Santos is on leave" in dm(gw)


def test_show_llm_input_prints_the_leave_payload_and_makes_no_call():
    conn, notion = build([team_project()])
    llm, out = LeaveLlm(STANDARD), []
    run_brief(conn, notion, CONTACTS, BRIEF, SATURDAY, llm=llm, log=lambda _: None, out=out.append,
              projects_ds=PROJECTS, show_llm_input=True)
    assert llm.payloads == []
    assert any("llm input: leaves" in text and "Lea - leave Nov 20-22" in text for text in out)


def test_a_failed_leave_parse_keeps_the_open_clash_concern_instead_of_resolving_it():
    conn, notion = build([team_project()])
    brief(conn, notion, LeaveLlm(STANDARD))
    gw, _ = brief(conn, notion, LeaveLlm(fail=True))
    text = dm(gw)
    assert "Lea Santos is on leave Nov 20–22" in text and "Resolved last week" not in text
    assert "Leaves unavailable" in digest(gw)


def test_unmatched_names_go_to_cyril_alone_when_the_digest_has_several_recipients():
    from fake_notion import contact as page_of
    people = contacts() + [page_of("c-jam", "Jam")]
    people[-1]["properties"].update(tg("jam_h", 777))
    conn, _ = build([team_project()])
    notion = FakeNotion([], people, description="\n".join([HEADING, *LEAVES]))
    settings = replace(BRIEF, digest_recipients=("c-cyril", "c-jam"))
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD + [entry("Zed", "2026-11-25", "2026-11-25", "Zed - leave Nov 25")]), settings)
    by = lambda who: "\n".join(t for to, _, _, t in gw.sent if to == who)
    assert "Unmatched leave names (add an alias in Contacts): Zed" in by(999)
    assert by(777) and "Zed" not in by(777)
    assert "Zed" not in by(111)
