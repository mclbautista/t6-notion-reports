"""Dates from PM Notes (#48): a blank date property filled from a quoted line, precise or fuzzy."""
from fake_notion import date, relation, rich
from t6_notion_reports.flags import ROLES, SEPARATE_ROLES
from test_consolidation import Llm, line_id, make_db, reply_with, run
from test_gated_briefs import AIRING, IN_PROGRESS, LEADS, STANDBY, digest, dm, proj

# Today is Sat 2026-11-14 in Manila.
NOTES = "As of Nov 9\n• Submission is Dec 1, waiting on the client\n• Playdate is Q2 2027"
SUBMIT_QUOTE = "• Submission is Dec 1, waiting on the client"
PLAY_QUOTE = "• Playdate is Q2 2027"


def staffed(**props):
    base = {r: relation("c-ana") for r in (*ROLES, *SEPARATE_ROLES) if r != "T6 Post Coordinator"}
    base.update(props)
    return base


def page(status=IN_PROGRESS, notes=NOTES, **props):
    return proj("p1", "ONE", status, **{"PM Notes": rich(notes)}, **staffed(**props))


def brief_for(status=IN_PROGRESS, notes=NOTES, **props):
    p = page(status, notes, **props)
    return make_db([p], [p], [p])


def submission(iso="2026-12-01", quote=SUBMIT_QUOTE, prop="submission_deadline", line="waiting on the client"):
    return lambda p: [{"property": prop, "iso_date": iso, "line_id": line_id(p, line), "quote": quote}]


def fuzzy(text="Q2 2027", quote=PLAY_QUOTE, prop="playdate"):
    return lambda p: [{"property": prop, "fuzzy_text": text, "line_id": line_id(p, "Q2 2027"), "quote": quote}]


def text_of(status=IN_PROGRESS, reply=None, to_digest=False, **props):
    gw, _ = run(brief_for(status, **props), Llm(reply_with(**reply)))
    return digest(gw) if to_digest else dm(gw)


def test_a_precise_date_shows_with_a_not_in_property_hint_and_the_missing_flag_goes():
    text = text_of(reply={"dates_from_notes": submission()}, Playdate=date("2026-12-20"))
    assert "Submission Deadline (from PM Notes): Dec 1, 2026 · not in property" in text
    assert "No Submission Deadline" not in text


def test_the_flag_stays_when_nothing_is_found_in_the_notes():
    assert "No Submission Deadline" in text_of(reply={}, Playdate=date("2026-12-20"))


def test_a_precise_date_from_notes_that_has_passed_is_overdue_for_in_progress():
    text = text_of(reply={"dates_from_notes": submission("2026-11-10", quote="• Submission was Nov 10, waiting on the client")},
                   Playdate=date("2026-12-20"), notes="As of Nov 9\n• Submission was Nov 10, waiting on the client")
    assert "Submission Deadline overdue by 4 days" in text


def test_a_precise_date_from_notes_that_has_passed_is_not_overdue_for_airing():
    text = text_of(AIRING, {"dates_from_notes": submission("2026-11-10", quote="• Submission was Nov 10, waiting on the client")},
                   Playdate=date("2026-12-20"), notes="As of Nov 9\n• Submission was Nov 10, waiting on the client")
    assert "overdue" not in text and "No Submission Deadline" not in text
    assert "Submission Deadline (from PM Notes): Nov 10, 2026 · not in property" in text


def test_a_fuzzy_value_renders_as_stated_and_raises_neither_flag():
    text = text_of(reply={"dates_from_notes": fuzzy()}, **{"Submission Deadline": date("2026-12-20")})
    assert "Playdate (from PM Notes): Q2 2027" in text
    assert "not in property" not in text
    assert "No Playdate" not in text and "overdue" not in text


def test_an_item_for_a_property_that_is_not_blank_is_dropped():
    text = text_of(reply={"dates_from_notes": submission()}, Playdate=date("2026-12-20"),
                   **{"Submission Deadline": date("2026-12-25")})
    assert "from PM Notes" not in text


def test_an_item_for_a_property_that_is_not_relevant_is_dropped():
    text = text_of(STANDBY, {"dates_from_notes": submission()})
    assert "from PM Notes" not in text
    lead = text_of(LEADS, {"dates_from_notes": submission()}, to_digest=True)
    assert "from PM Notes" not in lead


def test_an_unknown_property_is_dropped():
    text = text_of(reply={"dates_from_notes": submission(prop="deadline")}, Playdate=date("2026-12-20"))
    assert "from PM Notes" not in text and "No Submission Deadline" in text


def test_an_invalid_quote_is_dropped_and_the_missing_flag_stays():
    for quote in ("Submission is on Dec 1", "• Playdate is Q2 2027"):
        text = text_of(reply={"dates_from_notes": submission(quote=quote)}, Playdate=date("2026-12-20"))
        assert "from PM Notes" not in text and "No Submission Deadline" in text


def test_a_date_the_quoted_line_does_not_hold_is_dropped():
    text = text_of(reply={"dates_from_notes": submission("2026-12-09")}, Playdate=date("2026-12-20"))
    assert "from PM Notes" not in text and "No Submission Deadline" in text


def test_a_fuzzy_text_the_quoted_line_does_not_hold_is_dropped():
    text = text_of(reply={"dates_from_notes": fuzzy("Q3 2027")}, **{"Submission Deadline": date("2026-12-20")})
    assert "from PM Notes" not in text and "No Playdate" in text


def test_a_failed_call_leaves_the_missing_flag_in_place():
    p = page(Playdate=date("2026-12-20"))
    gw, _ = run(make_db([p], [p], [p]), Llm(fail_for=("Show ONE",)))
    assert "No Submission Deadline" in dm(gw)


def test_a_lead_uses_a_precise_playdate_from_notes_for_its_warning():
    notes = "As of Nov 9\n• Playdate is Nov 30"
    reply = {"dates_from_notes": lambda p: [{"property": "playdate", "iso_date": "2026-11-30",
                                               "line_id": line_id(p, "Playdate is Nov 30"), "quote": "• Playdate is Nov 30"}]}
    text = text_of(LEADS, reply, notes=notes, to_digest=True)
    assert "Playdate (from PM Notes): Nov 30, 2026 · not in property" in text
    assert "Playdate in 16 days: check with the client" in text


def test_a_year_that_the_line_or_the_calendar_contradicts_drops_the_item():
    notes = "As of Nov 9\n• Submission is Dec 1, 2026, waiting on the client"
    quote = "• Submission is Dec 1, 2026, waiting on the client"
    for iso in ("2025-12-01", "2027-12-01"):
        text = text_of(reply={"dates_from_notes": submission(iso, quote)}, notes=notes, Playdate=date("2026-12-20"))
        assert "from PM Notes" not in text and "No Submission Deadline" in text
    far = "As of Nov 9\n• Submission is Dec 1, waiting on the client"
    text = text_of(reply={"dates_from_notes": submission("2028-12-01")}, notes=far, Playdate=date("2026-12-20"))
    assert "from PM Notes" not in text


def test_a_past_fuzzy_value_is_never_overdue():
    notes = "As of Nov 9\n• Playdate was Q1 2025"
    reply = {"dates_from_notes": fuzzy("Q1 2025", "• Playdate was Q1 2025")}
    reply["dates_from_notes"] = lambda p: [{"property": "playdate", "fuzzy_text": "Q1 2025",
                                              "line_id": line_id(p, "Q1 2025"), "quote": "• Playdate was Q1 2025"}]
    text = text_of(reply=reply, notes=notes, **{"Submission Deadline": date("2026-12-20")})
    assert "Playdate (from PM Notes): Q1 2025" in text and "overdue" not in text and "No Playdate" not in text


def test_an_iso_date_or_a_far_date_with_its_year_written_is_accepted():
    for notes, iso in (("As of Nov 9\n• Submission 2026-12-01, waiting on the client", "2026-12-01"),
                       ("As of Nov 9\n• Submission Dec 1, 2028, waiting on the client", "2028-12-01"),
                       ("As of Nov 9\n• Submission 12/1/2026, waiting on the client", "2026-12-01")):
        quote = notes.splitlines()[1]
        text = text_of(reply={"dates_from_notes": submission(iso, quote)}, notes=notes, Playdate=date("2026-12-20"))
        assert "Submission Deadline (from PM Notes)" in text and "not in property" in text, notes
        assert "No Submission Deadline" not in text


def test_a_placeholder_is_not_a_fuzzy_date_and_the_missing_flag_stays():
    for wording in ("Playdate TBC", "TBA", "pending client", "Playdate"):
        notes = f"As of Nov 9\n• {wording} for now, Q2 2027 maybe"
        reply = {"dates_from_notes": lambda p, w=wording, n=notes: [
            {"property": "playdate", "fuzzy_text": w, "line_id": line_id(p, "for now"), "quote": n.splitlines()[1]}]}
        text = text_of(reply=reply, notes=notes, **{"Submission Deadline": date("2026-12-20")})
        assert "from PM Notes" not in text and "No Playdate" in text, wording
    for wording in ("Q2 2027", "early 2027", "mid-December", "end of March", "H1 2027"):
        notes = f"As of Nov 9\n• Playdate {wording}"
        reply = {"dates_from_notes": lambda p, w=wording, n=notes: [
            {"property": "playdate", "fuzzy_text": w, "line_id": line_id(p, "Playdate"), "quote": n.splitlines()[1]}]}
        text = text_of(reply=reply, notes=notes, **{"Submission Deadline": date("2026-12-20")})
        assert f"Playdate (from PM Notes): {wording}" in text, wording


def test_a_precise_date_from_notes_is_checked_for_order_and_a_fuzzy_one_is_not():
    # The notes give a Submission Deadline (Dec 1) before the property's Playdate (Dec 20).
    text = text_of(reply={"dates_from_notes": submission()}, Playdate=date("2026-12-20"))
    concerns = [ln for ln in text.splitlines() if "is before" in ln]
    assert len(concerns) == 1 and "Submission Deadline (Dec 1, 2026) is before Playdate (Dec 20, 2026)" in concerns[0]
    text = text_of(reply={"dates_from_notes": fuzzy()}, **{"Submission Deadline": date("2026-12-20")})
    assert "is before" not in text
