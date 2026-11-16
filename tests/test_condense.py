from datetime import date

from t6_notion_reports.condense import (Condensed, check_grounding, condense_bullets, extract_facts)
from t6_notion_reports.llm_api import ApiLlm
from fake_llm_api import FakeLlmApi, api_error, invalid, ok, refused, unreachable

MEET = date(2026, 10, 26)
OLD = "As of Oct 5,\n  • Fest submission on Nov 6\n  • Ang tagal ng grading"
NEW = ("As of Oct 26,\n  • Ep 3 locked, Ep 4 pa rin sa offline\n  • Oto will confirm 12 shooting days\n"
       "  • Lalabas na ang DCP")
NOTES = OLD + "\n" + NEW
SOURCE = ["Ep 3 locked, Ep 4 pa rin sa offline", "Oto will confirm 12 shooting days", "Lalabas na ang DCP"]


def adapter(api):
    return ApiLlm(api, "minutes_condense", "m", sleep=lambda _: None)


def run(bullets=None, error=False, notes=NOTES, source=SOURCE):
    api = FakeLlmApi(refused("provider", "api_error") if error else ok({"bullets": bullets}))
    return condense_bullets(adapter(api), "QAZ", notes, source, MEET), api


def test_facts_cover_dates_numbers_episodes_names():
    f = extract_facts("Episodes status: Ep 3 (locked), Ep 4; Oto on Nov 6 / 12 days")
    assert (11, 6) in f.dates and {"12", "3", "4", "6"} <= f.numbers
    assert f.episodes == {3, 4} and "oto" in f.names


def test_valid_output_replaces_raw():
    out, _ = run(["Episodes status: Ep 3 locked, Ep 4 sa offline", "Oto will confirm 12 shooting days"])
    assert out.bullets == ["Episodes status: Ep 3 locked, Ep 4 sa offline", "Oto will confirm 12 shooting days"]
    assert not out.raw


def test_invented_number_date_episode_or_name_is_rejected():
    for bad in ("Oto will confirm 13 shooting days", "Oto will confirm 12 shooting days on Nov 8",
                "Ep 5 locked", "Dario will confirm 12 shooting days", "Ep 3 locked on 11/9"):
        out, _ = run([bad])
        assert out.raw and out.bullets == SOURCE, bad


def test_one_upcoming_date_from_an_older_block_is_allowed():
    out, _ = run(["Ep 3 locked", "Fest submission on Nov 6"])
    assert not out.raw and out.bullets[1] == "Fest submission on Nov 6"


def test_two_older_dates_are_rejected():
    notes = NOTES.replace("Ang tagal", "Premiere on Nov 20\n  • Ang tagal")
    out, _ = run(["Fest submission on Nov 6", "Premiere on Nov 20"], notes=notes)
    assert out.raw


def test_older_date_that_is_past_is_rejected():
    notes = NOTES.replace("Nov 6", "Oct 10")
    out, _ = run(["Ep 3 locked", "Fest submission on Oct 10"], notes=notes)
    assert out.raw


def test_date_from_the_new_block_is_not_counted_as_older():
    notes = NOTES.replace("Lalabas na ang DCP", "DCP on Oct 30 and Nov 2")
    out, _ = run(["DCP on Oct 30 and Nov 2", "Fest submission on Nov 6"], notes=notes,
                 source=["DCP on Oct 30 and Nov 2"])
    assert not out.raw


def test_a_refusal_an_invalid_answer_a_timeout_or_an_unreachable_api_fall_back_raw():
    for answer in (refused("masking"), invalid(), refused("provider", "timeout"), unreachable()):
        out = condense_bullets(adapter(FakeLlmApi(answer)), "QAZ", NOTES, SOURCE, MEET)
        assert out == Condensed(SOURCE, True)


def test_a_bullet_with_an_unresolved_placeholder_is_dropped_alone():
    api = FakeLlmApi(ok({"bullets": ["Ep 3 locked", "[person 9] confirms", "Ep 4 sa offline"]}, unresolved=["bullets[1]"]))
    out = condense_bullets(adapter(api), "QAZ", NOTES, SOURCE, MEET)
    assert out == Condensed(["Ep 3 locked", "Ep 4 sa offline"], False)


def test_failed_call_or_empty_output_falls_back_raw():
    assert run(error=True)[0] == Condensed(SOURCE, True)
    assert run([])[0].raw and run(["  "])[0].raw


def test_the_real_notes_and_new_blocks_go_to_the_api_unmasked():
    _, api = run(["Ep 3 locked"])
    sent = api.sent[0]
    assert "Fest" in sent.input_text and "Lalabas" in sent.input_text and "Oto will confirm" in sent.input_text
    assert sent.task_name == "minutes_condense" and sent.require_zdr and sent.allow_projects == []


def test_check_grounding_reports_reason():
    assert check_grounding(["Ep 3 locked"], NOTES, SOURCE, MEET) is None
    assert "Dario" in check_grounding(["Dario is here"], NOTES, SOURCE, MEET)


def test_check_grounding_rejects_an_invented_lowercase_name_and_a_recombined_year():
    assert "zyxmarco" in check_grounding(["Ep 3 locked, ask zyxmarco"], NOTES, SOURCE, MEET)
    notes = "QC on 2026-12-01, festival in Q2 2027"
    assert check_grounding(["QC on 2026-12-01"], notes, ["QC on 2026-12-01"], MEET) is None
    assert "2027-12-01" in check_grounding(["QC on 2027-12-01"], notes, ["QC on 2026-12-01"], MEET)


# ---- in the minutes draft: Oct 26 fixtures (synthetic, shaped like the real QAZ, LSB and Wedding notes) ----
import sqlite3
from datetime import datetime, timezone

from fake_notion import CONTACTS, MEETING, PROJECTS, FakeNotion, project, rich
from t6_notion_reports.mom_job import run_draft_mom
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA

FRI = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
MON = datetime(2026, 10, 26, 3, tzinfo=timezone.utc)
OLD_QAZ = "As of Oct 12,\n  • Shoot wraps Nov 3\n  • TBC pa ang grading"
NEW_QAZ = OLD_QAZ + ("\nAs of Oct 26,\n  • Ep 3 locked na\n  • Ep 4 sa offline pa rin\n"
                     "  • Si Oto ang magco-confirm ng 12 shooting days\n  • Kain tayo sa Friday")
NEW_LSB = "As of Oct 26,\n  • Mixing delayed ng 2 days\n  • VBM will haggle"
NEW_WED = "As of Oct 26,\n  • Online on Nov 9\n  • Colorist: Pax"
OLD_ALL = {"QAZ": OLD_QAZ, "LSB": "", "WED": ""}
NEW_ALL = {"QAZ": NEW_QAZ, "LSB": NEW_LSB, "WED": NEW_WED}


def snap(which):
    return [project(c.lower(), c, c, **{"PM Notes": rich(t)}) for c, t in which.items()]


def drafted(answers):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(snap(OLD_ALL), []), PROJECTS, CONTACTS, "friday", FRI)
    run_snapshot(conn, FakeNotion(snap(NEW_ALL), []), PROJECTS, CONTACTS, "monday", MON)
    notion = FakeNotion(snap(NEW_ALL), [])

    def answer(inputs):
        code = json.loads(inputs.input_text)["project"].split()[0]
        return answers[code] if isinstance(answers[code], Exception) else ok({"bullets": answers[code]})
    run_draft_mom(conn, notion, PROJECTS, CONTACTS, MEETING, date(2026, 10, 26), log=lambda _: None,
                  llm=adapter(FakeLlmApi(answer)))
    return notion.created[0]["children"] + [c for _, cs in notion.appended for c in cs]


def project_block(blocks, code):
    return next(b for b in blocks if b["type"] == "bulleted_list_item"
                and "".join(t["text"]["content"] for t in b[b["type"]]["rich_text"]).startswith(code))


def flat(block):
    body = block[block["type"]]
    return [("".join(t["text"]["content"] for t in body["rich_text"]), block["type"]),
            *[x for c in body.get("children", []) for x in flat(c)]]


import json  # noqa: E402


def test_oct_26_projects_condense_with_a_fake_llm_and_toggles_hold_the_raw_block():
    blocks = drafted({
        "QAZ": ["Episodes status: Ep 3 locked, Ep 4 sa offline", "Oto magco-confirm ng 12 shooting days",
                "Shoot wraps Nov 3"],
        "LSB": ["Mixing delayed ng 2 days; VBM will haggle"],
        "WED": ["Online on Nov 9; Colorist: Pax"],
    })
    for code in ("QAZ", "LSB", "WED"):
        lines = flat(project_block(blocks, code))
        assert "(raw)" not in lines[0][0]
        toggle = [i for i, (_, kind) in enumerate(lines) if kind == "toggle"]
        assert len(toggle) == 1 and lines[toggle[0]][0] == "Raw source"
    qaz = [t for t, _ in flat(project_block(blocks, "QAZ"))]
    assert qaz[1] == "Episodes status: Ep 3 locked, Ep 4 sa offline"
    assert qaz[-4:] == ["Ep 3 locked na", "Ep 4 sa offline pa rin",
                        "Si Oto ang magco-confirm ng 12 shooting days", "Kain tayo sa Friday"][-4:]


def test_ungrounded_or_failed_projects_keep_raw_bullets_marked_raw():
    blocks = drafted({
        "QAZ": ["Ep 3 locked, Ep 5 delivered"],           # invented episode
        "LSB": api_error(403),                        # call failed
        "WED": ["Online on Nov 9; Colorist: Pax"],
    })
    qaz, lsb, wed = (flat(project_block(blocks, c)) for c in ("QAZ", "LSB", "WED"))
    assert qaz[0][0].endswith("(raw)") and qaz[1][0] == "Ep 3 locked na"
    assert lsb[0][0].endswith("(raw)") and lsb[1][0] == "Mixing delayed ng 2 days"
    assert not wed[0][0].endswith("(raw)") and wed[1][0] == "Online on Nov 9; Colorist: Pax"


def test_echoing_the_meeting_date_does_not_use_up_the_older_date_allowance():
    out, _ = run(["Ep 3 locked Oct 26", "Fest submission on Nov 6"])
    assert not out.raw


def test_minutes_job_sends_the_real_text_and_checks_the_restored_bullets():
    from fake_notion import contact
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    people = [contact("person-page", "Dario Varnel")]
    before = [project("own", "OWN", "Own Show"), project("other", "OTH", "Other Show")]
    after = [project("own", "OWN", "Own Show", **{"PM Notes": rich(
        "As of Oct 26\n• Dario confirmed Other Show")}), before[1]]
    run_snapshot(conn, FakeNotion(before, people), PROJECTS, CONTACTS, "friday", FRI)
    run_snapshot(conn, FakeNotion(after, people), PROJECTS, CONTACTS, "monday", MON)
    notion = FakeNotion(after, people)
    api = FakeLlmApi(lambda inputs: ok({"bullets": json.loads(inputs.input_text)["new_blocks"]}))
    run_draft_mom(conn, notion, PROJECTS, CONTACTS, MEETING, MEET, log=lambda _: None, llm=adapter(api))
    assert "Dario confirmed Other Show" in api.sent[0].input_text  # the API masks it
    lines = [text for text, _ in flat(project_block(notion.created[0]["children"], "OWN"))]
    assert "Dario confirmed Other Show" in lines and "(raw)" not in lines[0]


def test_dry_print_condenses_prints_the_bullets_and_writes_nothing():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(snap(OLD_ALL), []), PROJECTS, CONTACTS, "friday", FRI)
    run_snapshot(conn, FakeNotion(snap(NEW_ALL), []), PROJECTS, CONTACTS, "monday", MON)
    notion = FakeNotion(snap(NEW_ALL), [])
    api = FakeLlmApi(lambda inputs: ok({"bullets": ["Ep 9 locked"]}) if inputs.input_text.startswith('{"project": "QAZ"')
                     else ok({"bullets": ["Online on Nov 9; Colorist: Pax"]}))
    out, log = [], []
    result = run_draft_mom(conn, notion, PROJECTS, CONTACTS, MEETING, date(2026, 10, 26), log=log.append,
                           llm=adapter(api), dry_print=True, out=out.append)
    assert result.page_id is not None and not notion.created and not notion.appended
    assert "## QAZ (raw)" in out and "- Ep 3 locked na" in out and "## WED" in out and "- Online on Nov 9; Colorist: Pax" in out
    assert log and "condensed=" in log[0] and "raw=" in log[0]
