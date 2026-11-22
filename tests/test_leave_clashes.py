"""Leave clash Concerns (#49), through the brief job with a fake Notion, LLM and gateway."""
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta, datetime, timezone

from fake_notion import FakeNotion, contact, date, relation, rich
from t6_notion_reports.brief_job import run_brief
from fake_llm_api import ok, refused
from t6_notion_reports.llm_api import ApiLlm
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA
from test_gated_briefs import BRIEF, FRIDAY, IN_PROGRESS, MONDAY, SATURDAY, STANDBY, Gateway, digest, dm, proj, tg

HEADING = "Upcoming Holidays and Leaves"
LEAVES = [
    "• Nia - leave Nov 20-22",
    "• Cal - leave Dec 20",
    "• Nov 30 - Bonifacio Day (holiday)",
    "• Zed - leave Nov 25",
    "• Nia - holiday Nov 21",
]


def contacts():
    people = [contact("c-oto", "Oto"), contact("c-owner", "Owner"), contact("c-nia", "Nia Varnel"),
              contact("c-cal", "Cal Pardo"), contact("c-uma", "Uma Belk")]
    for page, (h, i) in zip(people[:4], (("oto_h", 111), ("owner_h", 999), ("nia_h", 333), ("cal_h", 444))):
        page["properties"].update(tg(h, i))
    people[2]["properties"]["Aliases"] = rich("Nio")
    return people  # Uma has no TelegramUserID


class LeaveLlm:
    """A fake ``llm run`` API: answers the leave call from a script, and records every payload it is sent."""

    def __init__(self, entries=None, fail=False):
        self.entries, self.fail, self.payloads, self.inputs = entries, fail, [], []

    def llm_run(self, inputs):
        payload = json.loads(inputs.input_text)
        if inputs.output_schema.get("title") != "LeaveParse":
            return ok({})
        self.payloads.append(payload)
        self.inputs.append(inputs)
        if self.fail:
            return refused("masking")
        entries = self.entries(payload) if callable(self.entries) else (self.entries or [])
        return ok({"entries": entries})


def leave_adapter(api):
    return ApiLlm(api, "brief_leaves", "m", sleep=lambda _: None)


def entry(who, start, end, quote, kind="leave"):
    return {"who": who, "kind": kind, "start": start, "end": end, "quote": quote}


REAL_LEAVES = [
    "Nia - leave Nov 20-22",
    "Cal - leave Dec 20",
    "Nov 30 - Bonifacio Day (holiday)",
    "Zed - leave Nov 25",
    "Nia - holiday Nov 21",
]


STANDARD = [
    entry("Nia Varnel", "2026-11-20", "2026-11-22", "Nia - leave Nov 20-22"),
    entry("Cal Pardo", "2026-12-20", "2026-12-20", "Cal - leave Dec 20"),
    entry("ALL", "2026-11-30", "2026-11-30", "Nov 30 - Bonifacio Day (holiday)", "holiday"),
]


def build(projects, description="\n".join([HEADING, *LEAVES])):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(projects, contacts()), "monday", MONDAY)
    run_snapshot(conn, FakeNotion(projects, contacts()), "friday", FRIDAY)
    return conn, FakeNotion.live(conn, contacts(), description=description)


def team_project(status=IN_PROGRESS, **props):
    base = {"Submission Deadline": date("2026-11-30"), "Playdate": date("2026-11-28"), "Editors": relation("c-nia")}
    base.update(props)
    return proj("p1", "ONE", status, **base)


def brief(conn, notion, llm, brief_settings=BRIEF, **kw):
    gw, logs = Gateway(), []
    run_brief(conn, notion, brief_settings, SATURDAY, gateway=gw, llm=None, leaves_llm=leave_adapter(llm), log=logs.append, **kw)
    return gw, logs


def test_a_team_members_leave_near_a_project_date_is_one_clash_concern_in_the_dm_and_the_digest():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert digest(gw).count("Nia Varnel is on leave Nov 20–22") == 1  # shown once, in Raise at PSR (#142)
    assert "Nia Varnel is on leave" not in dm(gw)  # the Concern has no owner here, so it is in no DM
    assert "Playdate (Nov 28, 2026)" in digest(gw) and "last evidence Nov 14" in digest(gw)  # the nearest of the dates


def test_the_app_sends_the_real_lines_and_matches_the_restored_name_against_contacts():
    conn, notion = build([team_project()])
    def answer(payload):
        line = payload["lines"][0]
        assert line == "Nia - leave Nov 20-22"  # the API masks it; the app holds no mapping
        return [entry("Nia", "2026-11-20", "2026-11-22", line)]
    api = LeaveLlm(answer)
    gw, _ = brief(conn, notion, api)
    assert "Nia Varnel is on leave Nov 20–22" in digest(gw)
    sent = api.inputs[0]
    assert sent.task_name == "brief_leaves" and sent.require_zdr and sent.restore == "per_field" and sent.allow_projects == []


# ---- the parse itself, over a fake API ----

from datetime import date as _date  # noqa: E402

from fake_llm_api import FakeLlmApi, invalid, unreachable  # noqa: E402
from t6_notion_reports.leaves import Leave, parse_leaves  # noqa: E402

LINES = ["Nia - leave Nov 20-22", "Cal - leave Dec 20"]
GOOD = {"who": "Nia", "kind": "leave", "start": "2026-11-20", "end": "2026-11-22", "quote": "Nia - leave Nov 20-22"}


def parsed(*answers):
    return parse_leaves(leave_adapter(FakeLlmApi(*answers)), LINES, _date(2026, 11, 14))


def test_a_good_entry_is_kept():
    assert parsed(ok({"entries": [GOOD]})).entries == (Leave("Nia", "leave", _date(2026, 11, 20), _date(2026, 11, 22)),)


def test_a_quote_that_is_not_an_exact_line_drops_that_entry_only():
    result = parsed(ok({"entries": [{**GOOD, "quote": "Nia - leave Nov 20-23"}, {**GOOD, "who": "Cal", "quote": "Cal - leave Dec 20"}]}))
    assert [e.who for e in result.entries] == ["Cal"] and not result.failed


def test_a_refusal_an_invalid_answer_a_timeout_or_an_unreachable_api_is_failed():
    for answer in (refused("masking"), invalid(), refused("provider", "timeout"), unreachable()):
        assert parsed(answer).failed


def test_an_entry_with_an_unresolved_placeholder_is_dropped_alone():
    entries = [{**GOOD, "who": "[person 9]"}, {**GOOD, "who": "Cal", "quote": "Cal - leave Dec 20"}]
    result = parsed(ok({"entries": entries}, unresolved=["entries[0].who"]))
    assert [e.who for e in result.entries] == ["Cal"] and not result.failed


def test_when_every_entry_is_dropped_for_a_placeholder_the_parse_failed():
    result = parsed(ok({"entries": [{**GOOD, "who": "[person 9]"}]}, unresolved=["entries[0].who"]))
    assert result.entries == () and result.failed


def test_a_leave_by_someone_in_prod_coor_supe_is_a_clash_like_one_in_editors():
    conn, notion = build([team_project(Editors=relation(), **{"Prod Coor/Supe": relation("c-nia")})])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert digest(gw).count("Nia Varnel is on leave Nov 20–22") == 1  # shown once, in Raise at PSR (#142)
    assert "Nia Varnel is on leave" not in dm(gw)  # the Concern has no owner here, so it is in no DM


def test_a_leave_by_someone_not_on_the_project_produces_nothing():
    conn, notion = build([team_project(Editors=relation("c-cal"))])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "on leave" not in dm(gw) + digest(gw)


def test_a_leave_outside_the_window_of_every_project_date_produces_nothing():
    late = {"Playdate": date("2027-01-05"), "Submission Deadline": date("2027-01-04")}
    conn, notion = build([team_project(Editors=relation("c-cal"), **late)])  # Cal's leave is Dec 20: 15 days before Jan 4
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Cal" not in dm(gw)
    conn, notion = build([team_project(Editors=relation("c-cal"), **{**late, "Submission Deadline": date("2027-01-03")})])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Cal Pardo is on leave Dec 20" in digest(gw)  # 14 days after leave.end: inside


def test_a_leave_starting_after_every_project_date_produces_nothing():
    people = contacts() + [contact("c-ana", "Ana Cruz")]
    people[-1]["properties"].update(tg("ana_h", 888))
    show_a = proj(
        "p-show-a",
        "Show A",
        IN_PROGRESS,
        **{
            "Playdate": date("2026-11-28"),
            "Submission Deadline": date("2026-11-30"),
            "Editors": relation("c-ana"),
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion([show_a], people), "monday", MONDAY)
    run_snapshot(conn, FakeNotion([show_a], people), "friday", FRIDAY)
    leaves = [
        entry("Ana Cruz", "2026-12-01", "2026-12-02", "Ana Cruz - leave Dec 1-2"),
    ]
    notion = FakeNotion.live(conn, people, description=f"{HEADING}\n• Ana Cruz - leave Dec 1-2")
    gw, _ = brief(conn, notion, LeaveLlm(leaves))
    assert "Ana Cruz" not in dm(gw) + digest(gw)
    assert "on leave" not in dm(gw) + digest(gw)


def test_the_window_comes_from_config():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD), replace(BRIEF, leave_clash_days=5))
    assert "on leave" not in dm(gw)  # Nov 22 is 8 days before Nov 30


def test_holidays_and_office_closures_produce_no_clash():
    everyone = [entry("ALL", "2026-11-20", "2026-11-22", "Nia - holiday Nov 21", "holiday"),
                entry("Nia", "2026-11-21", "2026-11-21", "Nia - holiday Nov 21", "holiday")]
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(everyone))
    assert "on leave" not in dm(gw) + digest(gw)


def test_an_entry_whose_quote_is_not_an_exact_line_or_whose_dates_are_invalid_is_dropped():
    bad = [
        entry("Nia", "2026-11-20", "2026-11-22", "Nia is away around Nov 20"),  # not a line of the input
        entry("Nia", "2026-11-31", "2026-11-31", "Nia - leave Nov 20-22"),  # no such day
        entry("Nia", "2026-11-22", "2026-11-20", "Nia - leave Nov 20-22"),  # ends before it starts
        entry("Nia", "soon", "later", "Nia - leave Nov 20-22"),
    ]
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(bad))
    assert "on leave" not in dm(gw)
    assert "Unmatched" not in digest(gw) and "Leaves unavailable" not in digest(gw)
    good = entry("Nia", "2026-11-20", "2026-11-22", "Nia - leave Nov 20-22")
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm(bad + [good]))
    assert digest(gw).count("on leave") == 1  # one bad entry does not void the rest


def test_a_quote_may_carry_the_bullet_mark_but_not_altered_spacing_inside_the_line():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Nia", "2026-11-20", "2026-11-22", "•  Nia - leave Nov 20-22 ")]))
    assert "Nia Varnel is on leave" in digest(gw)
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Nia", "2026-11-20", "2026-11-22", "Nia -  leave Nov 20-22")]))
    assert "on leave" not in dm(gw)


def test_a_nickname_is_matched_through_the_contacts_alias():
    conn, notion = build([team_project()])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Nio", "2026-11-20", "2026-11-22", "Nia - leave Nov 20-22")]))
    assert "Nia Varnel is on leave" in digest(gw)


def test_all_the_leave_names_of_a_run_go_to_person_find_in_one_call_with_telegram_first():
    conn, notion = build([team_project()])
    brief(conn, notion, LeaveLlm(STANDARD))
    assert notion.api.find_calls == [(["Nia Varnel", "Cal Pardo"], True)]  # distinct, in order; no holiday, no ALL


def test_a_namesake_the_api_cannot_tell_apart_is_unmatched_never_a_guess():
    people = contacts() + [contact("c-nia2", "Nia Varnel")]
    people[-1]["properties"].update(tg("nia2_h", 555))
    conn, _ = build([team_project()])
    notion = FakeNotion.live(conn, people, description="\n".join([HEADING, *LEAVES]))
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "on leave" not in dm(gw)
    assert "Unmatched leave names (add an alias in Contacts): Nia Varnel" in digest(gw)


def test_a_match_the_api_marks_for_review_is_unmatched():
    conn, notion = build([team_project()])
    nia = {"page_id": "c-nia", "t6_id": None, "name": "Nia Varnel"}
    notion.find_overrides = {"Nia Varnel": {"match": nia, "needs_review": True, "method": "fuzzy", "score": 0.7,
                                            "reason": None, "candidates": []}}
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Nia Varnel is on leave" not in dm(gw)
    assert "Unmatched leave names (add an alias in Contacts): Nia Varnel" in digest(gw)


def test_a_name_with_no_onboarded_match_falls_through_to_the_rest_of_contacts():
    conn, notion = build([team_project(Editors=relation("c-uma"))])
    gw, _ = brief(conn, notion, LeaveLlm([entry("Uma Belk", "2026-11-20", "2026-11-22", "Nia - leave Nov 20-22")]))
    assert "Uma Belk is on leave" in digest(gw)


def test_a_team_relation_that_holds_the_t6_id_of_the_person_is_a_clash_too():
    conn, notion = build([team_project(Editors=relation("per_nia"))])  # a snapshot value that is a T6 ID
    nia = {"page_id": "c-nia", "t6_id": "per_nia", "name": "Nia Varnel"}
    notion.find_overrides = {"Nia Varnel": {"match": nia, "needs_review": False, "method": "exact", "score": 1.0,
                                            "reason": None, "candidates": []}}
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Nia Varnel is on leave Nov 20–22" in digest(gw)


def test_an_unavailable_api_gives_no_clash_notes_leaves_unavailable_and_still_sends():
    conn, notion = build([team_project()])
    llm = LeaveLlm(STANDARD)

    def down(names):
        from t6_notion_reports.t6_api import ApiUnavailable
        raise ApiUnavailable("t6 API unreachable")
    notion.find_people = down
    gw, logs = brief(conn, notion, llm)
    assert "on leave" not in dm(gw) and dm(gw)
    assert "⚠️ Leaves unavailable: no leave clash Concerns this run" in digest(gw)
    assert "brief leaves unavailable error=ApiUnavailable" in logs


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
    conn, notion = build([team_project(), proj("p2", "TWO", Editors=relation("c-nia"))])
    llm = LeaveLlm(STANDARD)
    brief(conn, notion, llm)
    assert len(llm.payloads) == 1
    payload = llm.payloads[0]
    assert set(payload) == {"today", "lines"} and payload["today"] == "2026-11-14"
    assert payload["lines"] == REAL_LEAVES
    blob = json.dumps(payload)
    assert "Show ONE" not in blob and "c-nia" not in blob and "ONE" not in blob


def test_there_is_no_leave_call_without_leave_lines_and_no_note():
    conn, notion = build([team_project()], description=HEADING + "\n\n")
    llm = LeaveLlm(STANDARD)
    gw, _ = brief(conn, notion, llm)
    assert llm.payloads == []
    assert "Leaves unavailable" not in digest(gw)


def test_without_the_heading_nothing_is_sent_and_the_digest_says_leaves_are_unavailable():
    conn, notion = build([team_project()], description="Notes about the db\n• Nia - leave Nov 20-22")
    llm = LeaveLlm(STANDARD)
    gw, _ = brief(conn, notion, llm)
    assert llm.payloads == []
    assert "Leaves unavailable: no Upcoming Holidays and Leaves heading" in digest(gw)
    assert "Leaves unavailable" not in dm(gw)


def test_only_the_leaves_section_is_sent_and_a_later_heading_ends_it():
    plain = "\n".join([HEADING, *LEAVES, "", "Project planning", "• ONE needs Nia Varnel on Dec 1 for the online"])
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
        assert llm.payloads[0]["lines"] == REAL_LEAVES, description


def test_a_clash_concern_is_a_leave_finding_that_ages_through_the_api_and_stays_when_the_leave_goes():
    conn, notion = build([team_project()])
    brief(conn, notion, LeaveLlm(STANDARD))
    assert {f.source for f in notion.api.concerns.observed[-1].findings} == {"leave"}
    first = brief(conn, notion, LeaveLlm(STANDARD))  # same day: no change
    assert digest(first[0]).count("last evidence Nov 14") == 1
    notion.description = HEADING
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "within 14 days of the Playdate (Nov 28, 2026) · last evidence Nov 14" in digest(gw)  # not found this run, so it stays
    notion.api.concerns.close("on leave", "2026-11-16")  # a person closes it
    next_week, logs = Gateway(), []
    run_brief(conn, notion, BRIEF, SATURDAY + timedelta(weeks=1), gateway=next_week, llm=None,
              leaves_llm=leave_adapter(LeaveLlm(STANDARD)), log=logs.append)
    assert "Resolved last week: Nia Varnel is on leave" in dm(next_week)


def test_a_standby_project_is_still_checked_for_its_dates():
    conn, notion = build([team_project(status=STANDBY)])
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD))
    assert "Nia Varnel is on leave" in digest(gw)


def test_show_llm_input_prints_the_leave_payload_and_makes_no_call():
    conn, notion = build([team_project()])
    llm, out = LeaveLlm(STANDARD), []
    run_brief(conn, notion, BRIEF, SATURDAY, llm=None, leaves_llm=leave_adapter(llm), log=lambda _: None, out=out.append,
              show_llm_input=True)
    assert llm.payloads == []
    assert any("llm input: leaves" in text and "Nia - leave Nov 20-22" in text for text in out)


def test_a_failed_leave_parse_sends_the_leave_source_as_unavailable_and_the_open_clash_concern_stays():
    conn, notion = build([team_project()])
    brief(conn, notion, LeaveLlm(STANDARD))
    gw, _ = brief(conn, notion, LeaveLlm(fail=True))
    assert ("p1", "leave") in {(u.project, u.source) for u in notion.api.concerns.observed[-1].unavailable}
    text = digest(gw)
    assert "Nia Varnel is on leave Nov 20–22" in text and "Resolved last week" not in text
    assert "Leaves unavailable" in digest(gw)


def test_unmatched_names_go_to_cyril_alone_when_the_digest_has_several_recipients():
    from fake_notion import contact as page_of
    people = contacts() + [page_of("c-kit", "Kit")]
    people[-1]["properties"].update(tg("kit_h", 777))
    conn, _ = build([team_project()])
    notion = FakeNotion.live(conn, people, description="\n".join([HEADING, *LEAVES]))
    settings = replace(BRIEF, digest_recipients=("c-owner", "c-kit"))
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD + [entry("Zed", "2026-11-25", "2026-11-25", "Zed - leave Nov 25")]), settings)
    by = lambda who: "\n".join(t for to, _, _, t in gw.sent if to == who)
    assert "Unmatched leave names (add an alias in Contacts): Zed" in by(999)
    assert by(777) and "Zed" not in by(777)
    assert "Zed" not in by(111)


def test_a_blank_line_or_a_name_with_a_colon_inside_the_list_does_not_end_it():
    description = "\n".join([HEADING, "• Nia - leave Nov 20-22", "", "• Cal on vacation next week", "Ida on leave:",
                             "• Nov 25"])
    conn, notion = build([team_project()], description=description)
    llm = LeaveLlm(STANDARD)
    brief(conn, notion, llm)
    assert llm.payloads[0]["lines"] == ["Nia - leave Nov 20-22", "Cal on vacation next week", "Ida on leave:", "Nov 25"]


# ---- section boundaries when the section is empty or the heading is long (#62 item 7) ---------

def leave_payload_for(description):
    conn, notion = build([team_project()], description=description)
    llm = LeaveLlm(STANDARD)
    brief(conn, notion, llm)
    return llm.payloads


def test_an_empty_leave_section_stops_at_a_markdown_heading_and_sends_nothing():
    description = HEADING + "\n\n# Project planning\n• ONE needs Nia Varnel on Dec 1"
    assert leave_payload_for(description) == []


def test_an_empty_leave_section_stops_at_a_rich_text_heading_and_an_end_marker():
    rich_text = [
        {"plain_text": HEADING + "\n\n", "annotations": {"bold": True}},
        {"plain_text": "Staffing", "annotations": {"bold": True}},
        {"plain_text": "\nONE needs Nia Varnel on Dec 1", "annotations": {"bold": False}},
    ]
    assert leave_payload_for(rich_text) == []
    assert leave_payload_for(HEADING + "\n---\n• ONE needs Nia Varnel on Dec 1") == []


def test_an_introductory_bold_note_does_not_end_an_empty_section():
    rich_text = [
        {"plain_text": HEADING + "\n\nDon't forget the onboarding projects", "annotations": {"bold": True}},
        {"plain_text": "\n" + "\n".join(LEAVES), "annotations": {"bold": False}},
    ]
    (payload,) = leave_payload_for(rich_text)
    assert payload["lines"] == REAL_LEAVES


def test_a_long_plain_heading_after_the_entries_ends_the_section():
    description = "\n".join([HEADING, *LEAVES, "", "Planning notes for the whole post team this quarter", "• ONE needs Nia Varnel on Dec 1"])
    (payload,) = leave_payload_for(description)
    assert payload["lines"] == REAL_LEAVES


# ---- private unmatched names need an explicitly identified owner (#62 item 8) ------------------

def _unmatched_run(recipients, cyril_id):
    from fake_notion import contact as page_of
    people = contacts() + [page_of("c-kit", "Kit")]
    people[-1]["properties"].update(tg("kit_h", 777))
    conn, _ = build([team_project()])
    notion = FakeNotion.live(conn, people, description="\n".join([HEADING, *LEAVES]))
    settings = replace(BRIEF, cyril_contact_id=cyril_id, digest_recipients=recipients)
    gw, _ = brief(conn, notion, LeaveLlm(STANDARD + [entry("Zed", "2026-11-25", "2026-11-25", "Zed - leave Nov 25")]), settings)
    return lambda who: "\n".join(t for to, _, _, t in gw.sent if to == who)


def test_unmatched_names_reach_only_the_explicit_cyril_whatever_the_recipient_order():
    for recipients in (("c-owner", "c-kit"), ("c-kit", "c-owner")):
        by = _unmatched_run(recipients, "c-owner")
        assert "Zed" in by(999) and "Zed" not in by(777), recipients


def test_without_an_explicit_cyril_unmatched_names_are_withheld_and_a_configuration_warning_is_shown():
    for recipients in (("c-kit", "c-owner"), ("c-owner", "c-kit"), ("c-kit",)):
        by = _unmatched_run(recipients, None)
        for who in (999, 777):
            assert "Zed" not in by(who), recipients
        shared = by(777) or by(999)
        assert "cyril_contact_id" in shared and "Unmatched leave names" not in shared, recipients


def test_cyril_outside_the_recipient_list_never_gets_a_private_digest_and_names_stay_withheld():
    by = _unmatched_run(("c-kit", "c-oto"), "c-owner")
    assert "Zed" not in by(777) and "Zed" not in by(111) and "cyril_contact_id" in by(777)
