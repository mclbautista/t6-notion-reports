"""Gated, sectioned briefs (#44), through the brief job with a fake Notion, LLM and gateway."""
import json
import re
import sqlite3
from fake_llm import AskLlm
from datetime import datetime, timezone

from fake_notion import CONTACTS, PROJECTS, FakeNotion, contact, project, relation, rich, select
from html_check import assert_valid_html
from t6_notion_reports.brief_job import run_brief
from t6_notion_reports.config import BriefSettings
from t6_notion_reports.snapshot_job import run_snapshot
from t6_notion_reports.snapshots import SCHEMA

BRIEF = BriefSettings("http://gw:8780", -1003, 7, "c-cyril", False, "s3cret")
SATURDAY = datetime(2026, 11, 14, 4, tzinfo=timezone.utc)  # 12:00 Manila
MONDAY = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
FRIDAY = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)

IN_PROGRESS, TVC, AIRING = "🚨 In-Progress", "🏎. In-Progress TVC", "Airing"
STANDBY, ARCHIVING, LEADS = "🏋️ Standby", "📦 Archiving", "🌱 Leads"


def tg(handle, user_id):
    return {"TelegramHandle": rich(handle), "TelegramUserID": {"type": "number", "number": user_id}}


def contacts():
    out = [contact("c-oto", "Oto"), contact("c-ida", "Ida"), contact("c-cyril", "Cyril")]
    for page, (h, i) in zip(out, (("oto_h", 111), ("ida_h", 222), ("cyril_h", 999))):
        page["properties"].update(tg(h, i))
    return out


def proj(page_id, code, status=IN_PROGRESS, coordinator="c-oto", stage="Offline", health="ON TRACK",
         edited="2026-11-13T09:00:00.000Z", **props):
    page = project(
        page_id, f"Show {code}", code, Status=select(status), Stage=select(stage),
        **{"Project Health": select(health), "T6 Post Coordinator": relation(coordinator)}, **props,
    )
    page["last_edited_time"] = edited
    return page


class Gateway:
    def __init__(self):
        self.sent = []

    def notify(self, to, text, key, topic=None):
        self.sent.append((to, topic, key, text))
        return len(self.sent)


class Llm(AskLlm):
    """Records the projects it is asked about and finds nothing to report."""

    def __init__(self):
        self.projects = []

    def ask(self, system, user, schema):
        self.projects.append(json.loads(user)["project"]["name"])
        return schema()


def make_db(before, after):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion(before, contacts()), PROJECTS, CONTACTS, "monday", MONDAY)
    run_snapshot(conn, FakeNotion(after, contacts()), PROJECTS, CONTACTS, "friday", FRIDAY)
    return conn


def brief(conn, llm=None, logs=None):
    gw = Gateway()
    run_brief(conn, FakeNotion([], contacts()), CONTACTS, BRIEF, SATURDAY, gateway=gw, llm=llm,
              log=(logs.append if logs is not None else lambda _: None))
    return gw


def dm(gw, to=111):
    return "\n".join(text for t, _, _, text in gw.sent if t == to)


def digest(gw):
    return "\n".join(text for t, _, _, text in gw.sent if t == 999)


def changed(pages_by_status):
    """Same projects Monday and Friday, with the Stage moved on so each one reports a change."""
    return (
        [proj(pid, code, status, stage="Offline", **kw) for pid, code, status, kw in pages_by_status],
        [proj(pid, code, status, stage="Online", **kw) for pid, code, status, kw in pages_by_status],
    )


def test_other_statuses_appear_nowhere_and_never_reach_the_llm():
    rows = [("p1", "KEEP", IN_PROGRESS, {}), ("p2", "DONE", "✅ Done", {}),
            ("p4", "ARCH", "📦 Archived", {}), ("p5", "LOST", "🧭 Lost", {})]
    llm, logs = Llm(), []
    gw = brief(make_db(*changed(rows)), llm=llm, logs=logs)
    text = dm(gw) + digest(gw)
    assert "KEEP" in text
    assert not any(code in text for code in ("DONE", "ARCH", "LOST"))
    assert llm.projects == ["Show KEEP"]


def test_every_incoming_status_is_gated_in_as_leads():
    """t6-core's rule: Leads, Lead, Negotiations and Proposal Made are all Incoming."""
    past = {"Playdate": {"type": "date", "date": {"start": "2026-11-01", "end": None}}}
    rows = [("p1", "LEA", "🌱 Leads", past), ("p2", "LED", "Lead", past), ("p3", "NEGO", "🔏 Negotiations", past),
            ("p4", "PROP", "Proposal Made", past)]
    llm = Llm()
    gw = brief(make_db(*changed(rows)), llm=llm)
    assert sorted(llm.projects) == ["Show LEA", "Show LED", "Show NEGO", "Show PROP"]
    assert all(code in digest(gw) for code in ("LEA", "LED", "NEGO", "PROP"))
    assert not any(code in dm(gw) for code in ("LEA", "LED", "NEGO", "PROP"))


def test_an_excluded_status_that_looks_like_in_progress_is_logged_by_name():
    rows = [("p1", "KEEP", IN_PROGRESS, {}), ("p2", "TYPO", "In Progress", {}), ("p3", "DONE", "✅ Done", {})]
    logs = []
    gw = brief(make_db(*changed(rows)), llm=Llm(), logs=logs)
    assert "TYPO" not in dm(gw)
    assert any("TYPO" in line and "In Progress" in line for line in logs)
    assert not any("DONE" in line for line in logs)


def test_dm_shows_on_hold_before_in_progress_with_airing_and_tvc_under_in_progress():
    rows = [("p1", "RUN", IN_PROGRESS, {}), ("p2", "TVC", TVC, {}), ("p3", "AIR", AIRING, {}),
            ("p4", "SBY", STANDBY, {}), ("p5", "ARC", ARCHIVING, {}), ("p6", "LED", LEADS, {})]
    text = dm(brief(make_db(*changed(rows))))
    hold, progress = text.index("<b>On-Hold</b>"), text.index("<b>In-Progress</b>")
    assert hold < progress
    assert hold < text.index("SBY") < progress and hold < text.index("ARC") < progress
    assert all(text.index(code) > progress for code in ("RUN", "TVC", "AIR"))
    assert "LED" not in text and "Leads" not in text


def test_digest_groups_by_coordinator_alphabetically_with_sections_and_no_empty_ones():
    rows = [("p1", "OTO1", IN_PROGRESS, {}), ("p2", "OTO2", STANDBY, {}),
            ("p3", "IDA1", LEADS, {"coordinator": "c-ida"}), ("p4", "IDA2", IN_PROGRESS, {"coordinator": "c-ida"})]
    text = digest(brief(make_db(*changed(rows))))
    assert text.index("<b>Ida</b>") < text.index("<b>Oto</b>")
    ida, oto = text[text.index("<b>Ida</b>"):text.index("<b>Oto</b>")], text[text.index("<b>Oto</b>"):]
    assert ida.index("<b>In-Progress</b>") < ida.index("<b>Leads</b>") and "On-Hold" not in ida
    assert oto.index("<b>On-Hold</b>") < oto.index("<b>In-Progress</b>") and "Leads" not in oto


def test_a_section_is_sorted_by_project_health_then_name_with_blank_last():
    rows = [("p1", "ZED", IN_PROGRESS, {"health": "CRITICAL"}), ("p2", "BLANK", IN_PROGRESS, {"health": None}),
            ("p3", "INC", IN_PROGRESS, {"health": "INCOMING"}), ("p4", "ONT", IN_PROGRESS, {"health": "ON TRACK"}),
            ("p5", "RISK", IN_PROGRESS, {"health": "AT RISK"}), ("p6", "ABE", IN_PROGRESS, {"health": "CRITICAL"})]
    text = dm(brief(make_db(*changed(rows))))
    order = [text.index(f"Show {c}") for c in ("ABE", "ZED", "RISK", "ONT", "INC", "BLANK")]
    assert order == sorted(order)
    assert text.count("missing health update") == 1
    assert text.index("missing health update") > text.index("Show BLANK")


def test_project_line_has_stage_and_weeks_but_neither_status_nor_health():
    rows = [("p1", "OLD", IN_PROGRESS, {"edited": "2026-10-21T09:00:00.000Z"}),
            ("p2", "NEW", IN_PROGRESS, {"edited": "2026-11-12T09:00:00.000Z"})]
    text = dm(brief(make_db(*changed(rows))))
    heading = {c: next(ln for ln in text.splitlines() if f"Show {c}" in ln) for c in ("OLD", "NEW")}
    assert "Online" in heading["OLD"] and "updated 3 wks ago" in heading["OLD"]
    assert "updated this week" in heading["NEW"]
    for ln in heading.values():
        assert "In-Progress" not in ln and "ON TRACK" not in ln and "href" not in ln


def test_a_long_message_splits_between_projects_and_repeats_the_heading_as_cont():
    notes = "As of Nov 12,\n" + "\n".join(f"  • {'long line of notes ' * 8}{i}" for i in range(2))
    rows = [(f"p{n}", f"P{n:02d}", IN_PROGRESS, {"PM Notes": rich(notes)}) for n in range(40)]
    before, after = changed([(a, b, c, {k: v for k, v in d.items() if k != "PM Notes"}) for a, b, c, d in rows])
    after = [proj(a, b, c, stage="Online", **d) for a, b, c, d in rows]
    gw = brief(make_db(before, after))
    for who in (111, 999):
        parts = [text for t, _, _, text in gw.sent if t == who]
        assert len(parts) > 1
        for text in parts:
            assert_valid_html(text)
            assert len(text) <= 4096
        assert "(cont.)" not in parts[0]
        assert all("<b>In-Progress (cont.)</b>" in text.splitlines()[:3] for text in parts[1:])
        # No project is cut in half or repeated: its name and its notes sit in the same single part.
        for n in range(40):
            holders = [i for i, text in enumerate(parts) if f"Show P{n:02d}" in text]
            assert len(holders) == 1
            assert parts[holders[0]].count("long line of notes") >= 1


def test_the_first_brief_has_the_banner_and_no_changes():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    run_snapshot(conn, FakeNotion([proj("p1", "ONE", stage="Online")], contacts()), PROJECTS, CONTACTS, "friday", FRIDAY)

    class ChangeLlm(AskLlm):
        def ask(self, system, user, schema):
            return schema(changes=[{"text": "Online started", "source": "snapshot"}])

    gw = brief(conn, llm=ChangeLlm())
    assert gw.sent
    for _, _, _, text in gw.sent:
        assert "First brief: no comparison yet" in text
        assert "Project Health:" not in text and "→" not in text and "No changes on any project" not in text
        assert "Online started" not in text  # no earlier snapshot: even the LLM's changes are left out
    assert "No Submission Deadline" in dm(gw)


def staffed(**props):
    """Every role filled and both dates set: nothing to flag, so an unchanged project stays quiet."""
    from fake_notion import date
    from t6_notion_reports.flags import ROLES
    return {**{r: relation("c-ida") for r in ROLES if r != "T6 Post Coordinator"},
            "Playdate": date("2026-12-01"), "Submission Deadline": date("2026-12-20"), **props}


def test_an_unchanged_staffed_project_with_blank_health_reaches_its_coordinator_with_one_note():
    pages = [proj("p1", "NOHEALTH", health=None, **staffed())]
    for llm in (None, Llm()):
        gw = brief(make_db(pages, pages), llm=llm)
        text = dm(gw)
        assert "Show NOHEALTH" in text
        assert text.count("missing health update") == 1
        assert digest(gw).count("missing health update") == 1
        assert "No changes: Oto" not in digest(gw)


def test_the_project_line_shows_the_current_dates_even_when_they_did_not_change():
    # Blank health keeps the project listed after consolidation finds no change.
    rows = [("p1", "DATED", IN_PROGRESS, staffed(health=None))]
    for llm in (None, Llm()):
        text = dm(brief(make_db(*changed(rows)), llm=llm))
        heading = next(ln for ln in text.splitlines() if "Show DATED" in ln)
        assert "Playdate Dec 1, 2026" in heading and "Submission Deadline Dec 20, 2026" in heading
