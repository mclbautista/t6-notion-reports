"""Comparison facts require individually cited evidence; other facts remain line-scoped."""
from datetime import date, datetime, timezone

import pytest

from t6_notion_reports.consolidate import Consolidation, Window, build_payload, validate
from t6_notion_reports.diff import FieldChange

TODAY = date(2026, 11, 16)


def payload():
    return build_payload(
        {'Name': 'Synthetic', 'Project Health': 'ON TRACK',
         'PM Notes': 'As of Nov 14, 2026\n• Delivery Nov 20, 2026\n• Unrelated zyxmarco'},
        TODAY, TODAY,
        [Window('since_psr', datetime(2026, 11, 9, tzinfo=timezone.utc),
                datetime(2026, 11, 14, tzinfo=timezone.utc),
                (FieldChange('Submission Deadline', '2026-11-19', '2026-11-21'),))],
        ('• Delivery Nov 22, 2026',), {'page_date': '2026-11-14', 'last_edited': None})


def citation(p, origin, contains):
    line_id, line = next((i, line) for i, line in p.lines.items()
                         if line.origin == origin and contains in line.text)
    return {'source': origin, 'line_id': line_id, 'quote': line.text}


def outcome(p, text, kind='source_conflict', supporting=None):
    item = {**citation(p, 'pm_notes', 'Delivery'), 'kind': kind, 'text': text,
            'supporting_citations': supporting or []}
    return validate(Consolidation(concerns=[item]), p, TODAY)


def test_comparison_accepts_dates_from_two_explicitly_cited_sources():
    p = payload()
    text = 'Delivery Nov 20, 2026 or Nov 22, 2026.'
    assert outcome(p, text).concerns == ()
    assert outcome(p, text, supporting=[citation(p, 'minutes', 'Delivery')]).concerns == (text,)


@pytest.mark.parametrize('corruption', ['quote', 'source', 'line_id'])
def test_one_invalid_supporting_citation_drops_the_comparison(corruption):
    p = payload()
    support = citation(p, 'minutes', 'Delivery')
    support[corruption] = {'quote': 'invented', 'source': 'snapshot', 'line_id': 999}[corruption]
    assert outcome(p, 'Delivery Nov 20, 2026 or Nov 22, 2026.', supporting=[support]).concerns == ()


def test_comparisons_cannot_borrow_an_uncited_fact_or_recombine_a_year():
    p = payload()
    support = [citation(p, 'minutes', 'Delivery')]
    assert outcome(p, 'Delivery zyxmarco Nov 22, 2026.', supporting=support).concerns == ()
    assert outcome(p, 'Delivery Nov 20, 2027.', supporting=support).concerns == ()
    # open_item never gains access to another line through the comparison extension.
    assert outcome(p, 'Delivery Nov 22, 2026.', kind='open_item', supporting=support).concerns == ()


def test_health_comparison_can_use_the_explicit_project_health_property():
    p = payload()
    text = 'ON TRACK but Delivery Nov 20, 2026.'
    assert outcome(p, text, kind='health_vs_evidence').concerns == (text,)
    assert outcome(p, text, kind='open_item').concerns == ()


def test_comparison_support_can_cite_snapshot_and_preserves_date_grounding():
    p = payload()
    text = 'Delivery Nov 20, 2026 or Nov 21, 2026.'
    assert outcome(p, text, supporting=[citation(p, 'snapshot', 'Submission')]).concerns == (text,)


def test_unresolved_nested_supporting_quote_drops_the_item():
    p = payload()
    support = citation(p, 'minutes', 'Delivery')
    support['quote'] += ' [person 999]'
    assert outcome(p, 'Delivery Nov 20, 2026.', supporting=[support]).concerns == ()
