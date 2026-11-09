from datetime import date, datetime, timezone

import pytest

from html_check import assert_valid_html
from t6_notion_reports.brief_job import active_coordinators_in, coordinators_in
from t6_notion_reports.briefing import Settings, build_briefing
from t6_notion_reports.contacts import Contact
from t6_notion_reports.diff import diff_snapshots
from t6_notion_reports.snapshots import ProjectRecord, connect, load_snapshot, take_snapshot

TODAY = date(2026, 11, 15)
OTO, IDA, JUNO, CYRIL = "c-oto", "c-ida", "c-juno", "c-cyril"
CONTACTS = {
    OTO: Contact(OTO, "Oto", "oto_h", 111),
    IDA: Contact(IDA, "Ida", None, 222),
    JUNO: Contact(JUNO, "Juno Vex", "heyjunebug", None),
    CYRIL: Contact(CYRIL, "Cyril", None, 999),
}
SETTINGS = Settings(CONTACTS[CYRIL], -1003, 7)


def person(cid):
    return {"id": cid, "name": (CONTACTS.get(cid) or Contact(cid, cid.removeprefix("c-").title())).name}


def rec(page_id, code, coordinators=("c-oto",), **fields):
    base = {"Name": f"Show {code}", "Project Code": code, "Stage": "Offline", "Status": "🚨 In-Progress",
            "Project Health": "On Track", "PM Notes": "", "T6 Post Coordinator": [person(c) for c in coordinators]}
    base.update(fields)
    return ProjectRecord(page_id, base)


def run(before, after, settings=SETTINGS, contacts=CONTACTS, filter_active=False):
    conn = connect()
    a = take_snapshot(conn, before, datetime(2026, 11, 9, 3, tzinfo=timezone.utc), "monday").snapshot_id
    b = take_snapshot(conn, after, datetime(2026, 11, 13, 10, tzinfo=timezone.utc), "friday").snapshot_id
    old, new = load_snapshot(conn, a), load_snapshot(conn, b)
    changes = diff_snapshots(old, new, date(2026, 11, 9), date(2026, 11, 13))
    active = active_coordinators_in(new) if filter_active else None
    return build_briefing(changes, coordinators_in(old, new), contacts, settings, TODAY, active=active)


def by_recipient(messages):
    out = {}
    for m in messages:
        out.setdefault(m.recipient, []).append(m)
    return out


BEFORE = [
    rec("p1", "QAZ", ("c-oto",)),
    rec("p2", "NIM", ("c-ida",)),
    rec("p3", "DW", ("c-juno",)),
    rec("p4", "ORF", ()),
]
AFTER = [
    rec("p1", "QAZ", ("c-oto",), Stage="Online", **{"Project Health": "Critical"},
        **{"PM Notes": "As of Nov 12,\n  • Delay in <Mixing> & QC"}),
    rec("p2", "NIM", ("c-ida",)),
    rec("p3", "DW", ("c-juno",), Status="🏋️ Standby"),
    rec("p4", "ORF", (), Stage="Online"),
]


def test_each_coordinator_gets_one_dm_with_only_their_changed_projects():
    msgs = by_recipient(run(BEFORE, AFTER))
    [oto] = msgs["Oto"]
    assert (oto.to, oto.topic, oto.key) == (111, None, "notion-reports-2026-11-15-c-oto")
    assert "QAZ · Show QAZ" in oto.text and "NIM" not in oto.text and "DW" not in oto.text
    assert "Stage: Offline → Online" in oto.text and "Project Health: On Track → Critical" in oto.text
    assert "href" not in oto.text and "notion.so" not in oto.text
    assert_valid_html(oto.text)


def test_new_notes_are_raw_escaped_and_in_an_expandable_quote():
    [oto] = by_recipient(run(BEFORE, AFTER))["Oto"]
    assert "<blockquote expandable>" in oto.text
    assert "Delay in &lt;Mixing&gt; &amp; QC" in oto.text
    assert_valid_html(oto.text)


def test_coordinator_with_nothing_changed_gets_no_message():
    assert "Ida" not in by_recipient(run(BEFORE, AFTER))


def test_quiet_coordinator_without_telegram_id_triggers_no_group_fallback():
    contacts = {**CONTACTS, "c-zed": Contact("c-zed", "Zed", "zed_h", None)}
    quiet = rec("p5", "QQ", ("c-zed",))
    msgs = run(BEFORE + [quiet], AFTER + [quiet], contacts=contacts)
    assert "Zed" not in by_recipient(msgs)


def test_digest_no_changes_line_lists_only_active_coordinators():
    before = BEFORE + [rec("p5", "SBY", ("c-juno",), Status="🏋️ Standby"), rec("p6", "ARC", ("c-cyril",), Status="📦 Archiving")]
    after = AFTER + [rec("p5", "SBY", ("c-juno",), Status="🏋️ Standby"), rec("p6", "ARC", ("c-cyril",), Status="📦 Archiving")]
    msgs = run(before, after, filter_active=True)
    [digest] = by_recipient(msgs)["Cyril"]
    quiet_line = next(l for l in digest.text.splitlines() if l.startswith("No changes:"))
    assert quiet_line == "No changes: Cyril, Ida"  # Juno's only project is Standby


def test_digest_covers_everything_grouped_including_no_coordinator():
    [digest] = by_recipient(run(BEFORE, AFTER))["Cyril"]
    assert (digest.to, digest.topic, digest.key) == (999, None, "psr-digest-2026-11-15")
    text = digest.text
    assert text.index("<b>Oto</b>") < text.index("No coordinator")
    assert "QAZ" in text and "ORF" in text and "DW" in text
    assert "No changes: Ida" in text
    assert_valid_html(text)


def test_project_without_coordinator_only_reaches_the_digest():
    msgs = run(BEFORE, AFTER)
    assert [m.recipient for m in msgs if "ORF" in m.text] == ["Cyril"]


def test_no_telegram_id_falls_back_to_group_topic_with_mention_and_digest_notes_it():
    msgs = by_recipient(run(BEFORE, AFTER))
    [juno] = msgs["Juno Vex"]
    assert (juno.to, juno.topic, juno.key) == (-1003, 7, "notion-reports-2026-11-15-c-juno")
    assert juno.text.startswith("@heyjunebug:\n") and "DW" in juno.text
    assert "Juno Vex has no Telegram id" in msgs["Cyril"][0].text


def test_fallback_without_handle_uses_the_name():
    contacts = {**CONTACTS, JUNO: Contact(JUNO, "Juno Vex")}
    [juno] = by_recipient(run(BEFORE, AFTER, contacts=contacts))["Juno Vex"]
    assert juno.text.startswith("Juno Vex:\n")


def test_unknown_contact_falls_back_to_group_topic():
    contacts = {k: v for k, v in CONTACTS.items() if k != OTO}
    [oto] = by_recipient(run(BEFORE, AFTER, contacts=contacts))["Oto"]
    assert (oto.to, oto.topic) == (-1003, 7)


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
    oto = by_recipient(msgs)["Oto"]
    assert len(oto) > 1
    assert [m.key for m in oto] == ["notion-reports-2026-11-15-c-oto"] + [f"notion-reports-2026-11-15-c-oto-{n}" for n in range(2, len(oto) + 1)]
    for m in msgs:
        assert len(m.text) <= 4096
        assert_valid_html(m.text)


def test_without_cyrils_telegram_id_the_dms_still_go_and_the_digest_is_left_out():
    msgs = run(BEFORE, AFTER, settings=Settings(Contact(CYRIL, "Cyril"), -1003, 7))
    assert {m.recipient for m in msgs} == {"Oto", "Juno Vex"}


def test_active_coordinators_are_those_with_in_progress_or_archiving_projects():
    state = {
        "a": {"Status": "🚨  In-Progress ", "T6 Post Coordinator": [person(OTO)]},
        "b": {"Status": "📦 Archiving", "T6 Post Coordinator": [person(IDA)]},
        "c": {"Status": "🏋️ Standby", "T6 Post Coordinator": [person(JUNO)]},
        "d": {"Status": None, "T6 Post Coordinator": []},
    }
    assert active_coordinators_in(state) == {OTO, IDA}


# ---- splitting a project too long for one message (#60) -------------------------------------

def _plain(text):
    import re
    return re.sub(r"<[^>]+>", "", text)


def test_a_project_too_long_for_one_message_goes_on_under_its_own_name_marked_cont():
    notes = "As of Nov 12,\n" + "\n".join(f"  • note line {i} " + "x" * 60 for i in range(120))
    after = [rec("p1", "BIG", **{"PM Notes": notes}), rec("p2", "SMALL", Stage="Online")]
    msgs = [m for m in run([rec("p1", "BIG"), rec("p2", "SMALL")], after) if m.recipient == "Oto"]
    assert len(msgs) > 2
    for m in msgs:
        assert_valid_html(m.text) and len(m.text) <= 4096
    for m in msgs[1:]:
        head = _plain(m.text).splitlines()[:2]
        if "SMALL" in m.text and "note line" not in m.text:
            continue
        assert head == ["In-Progress (cont.)", "BIG · Show BIG (cont.)"], head
    body = "\n".join(_plain(m.text) for m in msgs)
    assert all(f"note line {i} " in body for i in range(120))  # nothing dropped
    assert body.count("SMALL · Show SMALL") == 1


def test_a_digest_tail_that_does_not_fit_starts_a_part_with_the_digest_title():
    from t6_notion_reports.briefing import build_briefing as build
    notes = [f"⚠️ warning {i} " + "y" * 80 for i in range(60)]
    conn_changes = []
    msgs = build(conn_changes, {}, CONTACTS, SETTINGS, TODAY, notes=notes)
    digest = [m for m in msgs if m.recipient == "Cyril"]
    assert len(digest) > 1
    for m in digest[1:]:
        assert _plain(m.text).splitlines()[0] == "PSR digest · Sun Nov 15 (cont.)"


# ---- a single oversized line keeps its context on every part (#62 item 1) -------------------

def _words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def _all_words(texts, prefix):
    return {w for t in texts for w in _plain(t).split() if w.startswith(prefix)}


def test_a_single_oversized_pm_notes_line_repeats_section_and_project_on_every_part():
    notes = "As of Nov 12,\n  • " + _words(2500)
    after = [rec("p1", "BIG", **{"PM Notes": notes}), rec("p2", "SMALL", Stage="Online")]
    msgs = [m for m in run([rec("p1", "BIG"), rec("p2", "SMALL")], after) if m.recipient == "Oto"]
    carrying = [m for m in msgs if any(w.startswith("w") and w[1:].isdigit() for w in _plain(m.text).split())]
    assert len(carrying) >= 3
    for m in msgs:
        assert_valid_html(m.text)
        assert len(m.text) <= 4096
    for m in carrying[1:]:
        assert _plain(m.text).splitlines()[:2] == ["In-Progress (cont.)", "BIG · Show BIG (cont.)"]
    assert _all_words(msgs and [m.text for m in msgs], "w") >= {f"w{i}" for i in range(2500)}


def test_a_single_oversized_summary_line_keeps_project_context():
    from dataclasses import replace
    conn = connect()
    a = take_snapshot(conn, [rec("p1", "BIG")], datetime(2026, 11, 9, 3, tzinfo=timezone.utc), "monday").snapshot_id
    b = take_snapshot(conn, [rec("p1", "BIG", Stage="Online")], datetime(2026, 11, 13, 10, tzinfo=timezone.utc), "friday").snapshot_id
    changes = diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b), date(2026, 11, 9), date(2026, 11, 13))
    changes = [replace(ch, summary=[_words(2500, "s")], consolidated=True) for ch in changes]
    msgs = [m for m in build_briefing(changes, {"c-oto": "Oto"}, CONTACTS, SETTINGS, TODAY) if m.recipient == "Oto"]
    assert len(msgs) >= 3
    for m in msgs:
        assert_valid_html(m.text)
        assert len(m.text) <= 4096
    for m in msgs[1:]:
        assert _plain(m.text).splitlines()[:2] == ["In-Progress (cont.)", "BIG · Show BIG (cont.)"]
    assert _all_words([m.text for m in msgs], "s") >= {f"s{i}" for i in range(2500)}


def test_a_single_oversized_digest_warning_keeps_the_digest_title():
    msgs = build_briefing([], {}, CONTACTS, SETTINGS, TODAY, notes=["⚠️ " + _words(2500, "z")])
    digest = [m for m in msgs if m.recipient == "Cyril"]
    assert len(digest) >= 3
    for m in digest:
        assert_valid_html(m.text)
        assert len(m.text) <= 4096
    for m in digest[1:]:
        assert _plain(m.text).splitlines()[0] == "PSR digest · Sun Nov 15 (cont.)"
    assert _all_words([m.text for m in digest], "z") >= {f"z{i}" for i in range(2500)}


@pytest.mark.parametrize("recipient", ["Oto", "Cyril"])
def test_normal_project_boundaries_preserve_whole_projects_and_section_context(recipient):
    before = [rec(f"p{i}", f"C{i:03d}") for i in range(20)]
    after = [rec(f"p{i}", f"C{i:03d}",
                 **{"PM Notes": "As of Nov 12,\n  • " + " ".join(f"project{i}_item{n}" for n in range(40))})
             for i in range(20)]
    messages = [m for m in run(before, after) if m.recipient == recipient]
    assert len(messages) > 1
    for message in messages:
        assert_valid_html(message.text)
        assert len(message.text) <= 4096
        assert any(f"C{i:03d} · Show C{i:03d}" in _plain(message.text) for i in range(20))
    for message in messages[1:]:
        expected = ["Oto", "In-Progress (cont.)"] if recipient == "Cyril" else ["In-Progress (cont.)"]
        assert _plain(message.text).splitlines()[:len(expected)] == expected
    for i in range(20):
        title = f"C{i:03d} · Show C{i:03d}"
        carrying = [m for m in messages if title in _plain(m.text)]
        assert len(carrying) == 1
        for n in range(40):
            marker = f"project{i}_item{n}"
            assert marker in _plain(carrying[0].text)
            assert sum(marker in _plain(m.text) for m in messages) == 1


@pytest.mark.parametrize("single_line", [False, True])
def test_oversized_digest_project_repeats_coordinator_section_and_identity(single_line):
    if single_line:
        body = _words(2500, "atom")
    else:
        body = "\n  • ".join(f"atom{i} " + "x" * 100 for i in range(120))
    messages = [m for m in run([rec("p1", "BIG")],
                [rec("p1", "BIG", **{"PM Notes": "As of Nov 12,\n  • " + body})])
                if m.recipient == "Cyril"]
    assert len(messages) > 2
    for i, message in enumerate(messages):
        assert_valid_html(message.text)
        assert len(message.text) <= 4096
        assert "BIG · Show BIG" in _plain(message.text)
        if i:
            assert _plain(message.text).splitlines()[:3] == [
                "Oto", "In-Progress (cont.)", "BIG · Show BIG (cont.)"]
    assert _all_words([m.text for m in messages], "atom") >= {
        f"atom{i}" for i in range(2500 if single_line else 120)}



def test_oversized_tail_after_projects_keeps_title_banner_and_every_warning_atom():
    from dataclasses import replace
    settings = replace(SETTINGS, test_banner=True)
    # Include a project so the tail must leave the final project part and resume independently.
    conn = connect()
    a = take_snapshot(conn, [rec("p1", "ONE")], datetime(2026, 11, 9, 3, tzinfo=timezone.utc), "monday").snapshot_id
    b = take_snapshot(conn, [rec("p1", "ONE", Stage="Online")], datetime(2026, 11, 13, 10, tzinfo=timezone.utc), "friday").snapshot_id
    changes = diff_snapshots(load_snapshot(conn, a), load_snapshot(conn, b), date(2026, 11, 9), date(2026, 11, 13))
    warnings = ["⚠️ " + _words(2500, "warning"), "⚠️ final warning"]
    messages = [m for m in build_briefing(changes, {OTO: "Oto"}, CONTACTS, settings, TODAY, notes=warnings)
                if m.recipient == "Cyril"]
    assert len(messages) >= 3
    assert "ONE · Show ONE" in messages[0].text
    for i, message in enumerate(messages):
        assert_valid_html(message.text)
        assert len(message.text) <= 4096
        assert message.text.startswith("🧪 TEST\n")
        if i:
            assert _plain(message.text).splitlines()[1] == "PSR digest · Sun Nov 15 (cont.)"
    assert _all_words([m.text for m in messages], "warning") >= {f"warning{i}" for i in range(2500)}
    assert sum("final warning" in m.text for m in messages) == 1
