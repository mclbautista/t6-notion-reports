import json
from datetime import date

import pytest
from pydantic import BaseModel

from html_check import assert_valid_html
from t6_notion_reports.briefing import OWED_UNAVAILABLE, Settings, build_briefing
from t6_notion_reports.contacts import Contact
from t6_notion_reports.llm import LlmError, OpenRouterClient
from t6_notion_reports.owed import MAX_ITEMS, OwedItem, OwedItems, OwedOutcome, find_owed

NOTES = "As of Nov 12,\n  • TLR piclock TBC\n  • Shoot today"
STATE = {"Name": "Show", "Project Code": "MAU", "Stage": "Preprod", "Status": "In-Progress",
         "Health": "On Track", "PM Notes": NOTES, "Deadline": None, "Editors": [],
         "Online Editor": [{"id": "x", "name": "Ben"}], "T6 Post Coordinator": [{"id": "c-pio", "name": "Pio"}]}


class FakeLlm:
    def __init__(self, items=(), error=False):
        self.items, self.error, self.calls = list(items), error, []

    def complete(self, system, user, schema):
        self.calls.append(user)
        if self.error:
            raise LlmError("down")
        return OwedItems(items=self.items)


def item(kind="open_item", text="Follow up", **cite):
    return OwedItem(kind=kind, text=text, **cite)


# --- LLM client ---

class Reply(BaseModel):
    ok: bool


def transport(status=200, content='{"ok": true}', sent=None):
    def send(url, headers, body):
        if sent is not None:
            sent.append((url, headers, body))
        return status, {"choices": [{"message": {"content": content}}]} if status == 200 else None
    return send


def test_every_request_carries_zdr_and_the_key():
    sent = []
    assert OpenRouterClient("or-key", "m", transport(sent=sent)).complete("sys", "usr", Reply) == Reply(ok=True)
    (_, headers, body), = sent
    assert body["provider"] == {"zdr": True} and body["model"] == "m"
    assert headers["Authorization"] == "Bearer or-key"


def test_failure_is_one_attempt_and_never_retried_without_zdr():
    sent = []
    with pytest.raises(LlmError) as exc:
        OpenRouterClient("or-key", "m", transport(404, sent=sent)).complete("s", "u", Reply)
    assert len(sent) == 1 and sent[0][2]["provider"] == {"zdr": True}
    assert "or-key" not in str(exc.value)


@pytest.mark.parametrize("content", ["not json", '{"ok": "maybe"}', "[]", None])
def test_answer_that_does_not_validate_is_an_error(content):
    with pytest.raises(LlmError):
        OpenRouterClient("k", "m", transport(content=content)).complete("s", "u", Reply)


def test_json_in_a_code_fence_is_accepted():
    client = OpenRouterClient("k", "m", transport(content='```json\n{"ok": true}\n```'))
    assert client.complete("s", "u", Reply).ok


def test_transport_exception_is_an_llm_error_without_its_message():
    def boom(*a):
        raise OSError("https://openrouter.ai secret-url")
    with pytest.raises(LlmError) as exc:
        OpenRouterClient("k", "m", boom).complete("s", "u", Reply)
    assert "secret-url" not in str(exc.value)


# --- validation in code ---

def test_valid_notes_citation_is_kept_ignoring_bullet_and_whitespace():
    out = find_owed(FakeLlm([item(notes_line="•  TLR piclock   TBC")]), STATE)
    assert [i.text for i in out.items] == ["Follow up"]


def test_item_citing_a_line_not_in_the_notes_is_dropped():
    out = find_owed(FakeLlm([item(notes_line="Pending approval from client")]), STATE)
    assert out.items == () and not out.failed


def test_partial_quote_of_a_line_is_dropped():
    assert find_owed(FakeLlm([item(notes_line="TLR piclock")]), STATE).items == ()


def test_property_citation_must_be_empty():
    llm = FakeLlm([
        item("missing_date", "No deadline", empty_property="Deadline"),
        item("missing_role", "No editor", empty_property="Editors"),
        item("missing_role", "No online", empty_property="Online Editor"),  # named: not empty
        item("missing_role", "Bogus", empty_property="Name"),                # not an input property
    ])
    assert [i.text for i in find_owed(llm, STATE).items] == ["No deadline", "No editor"]


def test_item_without_any_citation_is_dropped_and_bad_second_citation_too():
    llm = FakeLlm([item(), item(notes_line="Shoot today", empty_property="Online Editor")])
    assert find_owed(llm, STATE).items == ()


def test_cap_at_15_counts_the_rest():
    many = [item(text=f"item {n}", notes_line="Shoot today") for n in range(20)]
    out = find_owed(FakeLlm(many), STATE)
    assert len(out.items) == MAX_ITEMS == 15 and out.more == 5


def test_invalid_items_do_not_count_toward_the_cap():
    many = [item(text=f"item {n}", notes_line="Shoot today") for n in range(15)]
    many += [item(text="bad", notes_line="nope")] * 10
    out = find_owed(FakeLlm(many), STATE)
    assert len(out.items) == 15 and out.more == 0


def test_llm_failure_is_an_outcome_not_an_exception():
    assert find_owed(FakeLlm(error=True), STATE).failed


def test_prompt_carries_notes_and_properties_with_team_names():
    llm = FakeLlm()
    find_owed(llm, STATE)
    sent = json.loads(llm.calls[0])
    assert sent["pm_notes"] == NOTES and sent["Stage"] == "Preprod" and sent["Online Editor"] == ["Ben"]


# --- in the briefing ---

CONTACTS = {"c-pio": Contact("c-pio", "Pio", "pio_h", 111), "c-cyril": Contact("c-cyril", "Cyril", None, 999)}
SETTINGS = Settings(CONTACTS["c-cyril"], -1003, 7)


def brief(owed, failed=(), changes=None):
    from t6_notion_reports.diff import display_names, unchanged_project
    names = display_names({}, {"p1": STATE})
    shown = changes if changes is not None else [unchanged_project("p1", names, STATE, date(2026, 11, 13))]
    return build_briefing(shown, {"c-pio": "Pio"}, CONTACTS, SETTINGS, date(2026, 11, 15), owed, list(failed))


def test_project_without_changes_but_with_owed_items_appears():
    msgs = brief({"p1": OwedOutcome((item(text="Need <date> & more", notes_line="Shoot today"),))})
    dm = next(m for m in msgs if m.recipient == "Pio")
    assert "MAU" in dm.text and "What you owe" in dm.text and "Need &lt;date&gt; &amp; more" in dm.text
    assert "No changes on your projects" not in dm.text
    assert_valid_html(dm.text)


def test_unchanged_project_with_no_owed_items_stays_out():
    msgs = brief({"p1": OwedOutcome()}, changes=[])
    assert [m for m in msgs if m.recipient == "Pio"] == []


def test_more_than_fifteen_shows_plus_n_more():
    out = OwedOutcome(tuple(item(text=f"i{n}", notes_line="Shoot today") for n in range(15)), more=4)
    dm = next(m for m in brief({"p1": out}) if m.recipient == "Pio")
    assert dm.text.count("• Open: i") == 15 and "+4 more" in dm.text


def test_failure_shows_the_unavailable_line_and_digest_notes_it():
    msgs = brief({"p1": OwedOutcome(failed=True)}, failed=["MAU"], changes=[])
    assert [m for m in msgs if m.recipient == "Pio"] == []
    assert f"{OWED_UNAVAILABLE}: MAU" in next(m for m in msgs if m.recipient == "Cyril").text


def test_failed_project_without_changes_still_gets_the_unavailable_line():
    from t6_notion_reports.diff import display_names, unchanged_project
    names = display_names({}, {"p1": STATE})
    shown = [unchanged_project("p1", names, STATE, date(2026, 11, 13))]
    msgs = brief({"p1": OwedOutcome(failed=True)}, ["MAU"], shown)
    assert OWED_UNAVAILABLE in next(m for m in msgs if m.recipient == "Pio").text


def test_property_citation_only_for_date_and_role_kinds():
    out = find_owed(FakeLlm([item("open_item", "x", empty_property="Deadline")]), STATE)
    assert out.items == ()


def test_failure_on_a_changed_project_keeps_raw_changes_and_adds_the_line():
    from t6_notion_reports.diff import diff_snapshots
    changed = diff_snapshots({"p1": {**STATE, "Stage": "Offline"}}, {"p1": STATE}, date(2026, 11, 9), date(2026, 11, 13))
    msgs = brief({"p1": OwedOutcome(failed=True)}, ["MAU"], changed)
    dm = next(m for m in msgs if m.recipient == "Pio")
    assert "Stage: Offline → Preprod" in dm.text and OWED_UNAVAILABLE in dm.text


def test_no_llm_means_no_owed_section():
    dm = next(m for m in brief(None) if m.recipient == "Pio")
    assert "owe" not in dm.text and OWED_UNAVAILABLE not in dm.text
