from datetime import date, datetime, timezone

import pytest

from html_check import assert_valid_html
from t6_notion_reports.brief_job import coordinators_in
from t6_notion_reports.briefing import NO_CHANGES, Settings, build_briefing
from t6_notion_reports.contacts import Contact
from t6_notion_reports.diff import diff_snapshots
from t6_notion_reports.snapshots import ProjectRecord, connect, load_snapshot, take_snapshot

TODAY = date(2026, 11, 15)
PIO, ANA, ANGEL, CYRIL = "c-pio", "c-ana", "c-angel", "c-cyril"
CONTACTS = {
    PIO: Contact(PIO, "Pio", "pio_h", 111),
    ANA: Contact(ANA, "Ana", None, 222),
    ANGEL: Contact(ANGEL, "Angel Gaas", "heygelica", None),
    CYRIL: Contact(CYRIL, "Cyril", None, 999),
}
SETTINGS = Settings(CONTACTS[CYRIL], -1003, 7)


def person(cid):
    return {"id": cid, "name": CONTACTS[cid].name}


def rec(page_id, code, coordinators=("c-pio",), **fields):
    base = {"Name": f"Show {code}", "Project Code": code, "Stage": "Offline", "Status": "In-Progress",
            "Health": "On Track", "PM Notes": "", "T6 Post Coordinator": [person(c) for c in coordinators]}
    base.update(fields)
    return ProjectRecord(page_id, base)


def run(before, after, settings=SETTINGS, contacts=CONTACTS):
    conn = connect()
    a = take_snapshot(conn, before, datetime(2026, 11, 9, 3, tzinfo=timezone.utc), "monday").snapshot_id
    b = take_snapshot(conn, after, datetime(2026, 11, 13, 10, tzinfo=timezone.utc), "friday").snapshot_id
    old, new = load_snapshot(conn, a), load_snapshot(conn, b)
    changes = diff_snapshots(old, new, date(2026, 11, 9), date(2026, 11, 13))
    return build_briefing(changes, coordinators_in(old, new), contacts, settings, TODAY)


def by_recipient(messages):
    out = {}
    for m in messages:
        out.setdefault(m.recipient, []).append(m)
    return out


BEFORE = [
    rec("p1", "PAS", ("c-pio",)),
    rec("p2", "RIA", ("c-ana",)),
    rec("p3", "DW", ("c-angel",)),
    rec("p4", "ORF", ()),
]
AFTER = [
    rec("p1", "PAS", ("c-pio",), Stage="Online", Health="Critical",
        **{"PM Notes": "As of Nov 12,\n  • Delay in <Mixing> & QC"}),
    rec("p2", "RIA", ("c-ana",)),
    rec("p3", "DW", ("c-angel",), Status="Standby"),
    rec("p4", "ORF", (), Stage="Online"),
]


def test_each_coordinator_gets_one_dm_with_only_their_changed_projects():
    msgs = by_recipient(run(BEFORE, AFTER))
    [pio] = msgs["Pio"]
    assert (pio.to, pio.topic, pio.key) == (111, None, "notion-reports-2026-11-15-c-pio")
    assert "PAS · Show PAS" in pio.text and "RIA" not in pio.text and "DW" not in pio.text
    assert "Stage: Offline → Online" in pio.text and "🔴 Critical" in pio.text
    assert "https://www.notion.so/p1" in pio.text
    assert_valid_html(pio.text)


def test_new_notes_are_raw_escaped_and_in_an_expandable_quote():
    [pio] = by_recipient(run(BEFORE, AFTER))["Pio"]
    assert "<blockquote expandable>" in pio.text
    assert "Delay in &lt;Mixing&gt; &amp; QC" in pio.text
    assert_valid_html(pio.text)


def test_coordinator_with_nothing_changed_gets_the_one_line():
    [ana] = by_recipient(run(BEFORE, AFTER))["Ana"]
    assert (ana.to, ana.text) == (222, NO_CHANGES)


def test_digest_covers_everything_grouped_including_no_coordinator():
    [digest] = by_recipient(run(BEFORE, AFTER))["Cyril"]
    assert (digest.to, digest.topic, digest.key) == (999, None, "psr-digest-2026-11-15")
    text = digest.text
    assert text.index("<b>Pio</b>") < text.index("No coordinator")
    assert "PAS" in text and "ORF" in text and "DW" in text
    assert "No changes: Ana" in text
    assert_valid_html(text)


def test_project_without_coordinator_only_reaches_the_digest():
    msgs = run(BEFORE, AFTER)
    assert [m.recipient for m in msgs if "ORF" in m.text] == ["Cyril"]


def test_no_telegram_id_falls_back_to_group_topic_with_mention_and_digest_notes_it():
    msgs = by_recipient(run(BEFORE, AFTER))
    [angel] = msgs["Angel Gaas"]
    assert (angel.to, angel.topic, angel.key) == (-1003, 7, "notion-reports-2026-11-15-c-angel")
    assert angel.text.startswith("@heygelica:\n") and "DW" in angel.text
    assert "Angel Gaas has no Telegram id" in msgs["Cyril"][0].text


def test_fallback_without_handle_uses_the_name():
    contacts = {**CONTACTS, ANGEL: Contact(ANGEL, "Angel Gaas")}
    [angel] = by_recipient(run(BEFORE, AFTER, contacts=contacts))["Angel Gaas"]
    assert angel.text.startswith("Angel Gaas:\n")


def test_unknown_contact_falls_back_to_group_topic():
    contacts = {k: v for k, v in CONTACTS.items() if k != ANA}
    [ana] = by_recipient(run(BEFORE, AFTER, contacts=contacts))["Ana"]
    assert (ana.to, ana.topic) == (-1003, 7)


def test_test_banner_on_every_message_and_part():
    settings = Settings(CONTACTS[CYRIL], -1003, 7, test_banner=True)
    msgs = run(BEFORE, AFTER, settings=settings)
    assert msgs and all(m.text.startswith("🧪 TEST\n") for m in msgs)


def test_rerun_produces_identical_keys():
    assert [m.key for m in run(BEFORE, AFTER)] == [m.key for m in run(BEFORE, AFTER)]


def test_long_briefing_splits_with_unique_keys_and_valid_parts():
    many = [rec(f"p{i}", f"C{i:03d}") for i in range(80)]
    notes = "As of Nov 12,\n" + "\n".join(f"  • item {n} " + "w" * 90 for n in range(12))
    after = [rec(f"p{i}", f"C{i:03d}", **{"PM Notes": notes}) for i in range(80)]
    msgs = run(many, after)
    pio = by_recipient(msgs)["Pio"]
    assert len(pio) > 1
    assert [m.key for m in pio] == ["notion-reports-2026-11-15-c-pio"] + [f"notion-reports-2026-11-15-c-pio-{n}" for n in range(2, len(pio) + 1)]
    for m in msgs:
        assert len(m.text) <= 4096
        assert_valid_html(m.text)


def test_without_cyrils_telegram_id_the_dms_still_go_and_the_digest_is_left_out():
    msgs = run(BEFORE, AFTER, settings=Settings(Contact(CYRIL, "Cyril"), -1003, 7))
    assert {m.recipient for m in msgs} == {"Pio", "Ana", "Angel Gaas"}
