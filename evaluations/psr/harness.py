"""Offline preparation and triage only. This module has no model client or network calls."""
import argparse
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import cast

from t6.llm.evals import (Attempt, Case, Grader, Provenance, RunRecord,
                          grade_kinds, grade_quotes, grade_schema, run_evals)
from t6.llm.transport import Provider

from t6_notion_reports.consolidate import (
    SYSTEM, Consolidation, Window, build_payload, validate,
)
from t6_notion_reports.diff import FieldChange

ROOT = Path(__file__).resolve().parent


def fixtures():
    corpus = json.loads((ROOT / 'cases.json').read_text())
    today = date.fromisoformat(corpus['today'])
    for case in corpus['cases']:
        windows = []
        if case.get('snapshot_end'):
            windows = [Window('since_last_brief', datetime.fromisoformat('2026-11-01T00:00:00+08:00'),
                              datetime.fromisoformat(case['snapshot_end']),
                              (FieldChange('Submission Deadline', '2026-11-19', '2026-11-21'),))]
        payload = build_payload(
            {'Name': 'Synthetic Film', 'Status': '🚨 In-Progress', 'Stage': 'Online',
             'Project Health': case['health'], 'PM Notes': case['notes']},
            today, today, windows, tuple(case.get('minutes', [])),
            {'page_date': '2026-11-14', 'last_edited': case.get('minutes_edited'), 'sent_at': None})
        yield case, payload, today


def export(directory):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'status': 'DRAFT: human review pending; no live evaluation performed',
                'system_sha256': hashlib.sha256(SYSTEM.encode()).hexdigest(), 'cases': []}
    (directory / 'system.txt').write_text(SYSTEM)
    (directory / 'schema.json').write_text(json.dumps(Consolidation.model_json_schema(), indent=2))
    for case, payload, _ in fixtures():
        (directory / f"{case['id']}.input.json").write_text(payload.json(indent=2))
        manifest['cases'].append({'id': case['id'], 'issue': case['issue'], 'expected': case['expected']})
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2))


def evaluate(directory, output):
    results = []
    for case, payload, today in fixtures():
        path = directory / f"{case['id']}.output.json"
        result = {'id': case['id'], 'human_review': 'PENDING', 'expected': case['expected']}
        if not path.exists():
            result['status'] = 'NOT RUN: missing structured output'
        else:
            try:
                raw = json.loads(path.read_text())
                reply = Consolidation.model_validate(raw)
                accepted = validate(reply, payload, today)
                graded = grade(next(c for c in load_cases() if c.id == case['id']), raw)
                result['shared_grader'] = {'passed': graded.passed, 'reason': graded.reason}
                kinds = {c.kind for c in reply.concerns if c.text.strip() in accepted.concerns}
                expected = case['expected']
                result.update(status='AUTOMATED TRIAGE ONLY', accepted=asdict(accepted),
                              missing_kinds=sorted(set(expected['kinds']) - kinds),
                              forbidden_kinds=sorted(set(expected.get('forbidden', [])) & kinds),
                              raw_item_counts={key: len(raw.get(key, [])) for key in ('changes', 'concerns')},
                              parsed_item_counts={'changes': len(reply.changes), 'concerns': len(reply.concerns)},
                              accepted_item_counts={'changes': len(accepted.changes), 'concerns': len(accepted.concerns)})
            except (ValueError, TypeError) as error:
                result.update(status='INVALID OUTPUT', error=type(error).__name__)
        results.append(result)
    output.write_text(json.dumps({'status': 'No acceptance decision: human review and live-run provenance required',
                                  'results': results}, indent=2, default=str))


def load_cases():
    """Adapt the app-owned synthetic corpus to the shared case contract."""
    cases = []
    for source, payload, _ in fixtures():
        expected = source['expected']
        properties = ([f"required_kind:{kind}" for kind in expected['kinds']]
                      + [f"forbidden_kind:{kind}" for kind in expected.get('forbidden', [])]
                      + [f"review:{expected['review']}"])
        for key in ('citation', 'newest', 'winning_value'):
            if key in expected:
                properties.append(f"{key}:{expected[key]}")
        provenance: Provenance = {'kind': 'synthetic'}
        grader: Grader = 'psr'
        cases.append(Case(id=source['id'], task='psr_consolidation', masked_input=payload.data,
                          expected_properties=properties, grader=grader, provenance=provenance))
    return cases


def grade(case, output):
    """Grade a reply using the shared graders and production acceptance rules."""
    schema = grade_schema(case, output, Consolidation.model_json_schema())
    if not schema.passed:
        return Attempt(False, schema.reason)
    reply = Consolidation.model_validate(output)
    payload, today = next((payload, today) for source, payload, today in fixtures()
                          if source['id'] == case.id)
    accepted = validate(reply, payload, today)
    kinds = [item.kind for item in reply.concerns if item.text.strip() in accepted.concerns]
    required = [p.removeprefix('required_kind:') for p in case.expected_properties if p.startswith('required_kind:')]
    forbidden = [p.removeprefix('forbidden_kind:') for p in case.expected_properties if p.startswith('forbidden_kind:')]
    kind_result = grade_kinds(case, kinds, required=required, forbidden=forbidden)
    quotes = [item.quote for group in (reply.changes, reply.concerns, reply.dates_from_notes)
              for item in group if item.quote is not None]
    quotes += [citation.quote for item in reply.concerns for citation in item.supporting_citations]
    quote_result = grade_quotes(case, quotes)
    results = (schema, kind_result, quote_result)
    citations = [p.removeprefix('citation:') for p in case.expected_properties if p.startswith('citation:')]
    citation_ok = all(any(citation in quote for quote in quotes) for citation in citations)
    return Attempt(all(result.passed for result in results) and citation_ok,
                   '; '.join(result.reason for result in results) + ('; expected citation missing' if not citation_ok else ''))


def offline_run(cases):
    outputs = json.loads((ROOT / 'offline_outputs.json').read_text())
    record = RunRecord(model_id='offline-fixtures', provider='offline', prompt_hash=hashlib.sha256(SYSTEM.encode()).hexdigest(),
                       schema_hash=hashlib.sha256(json.dumps(Consolidation.model_json_schema(), sort_keys=True).encode()).hexdigest(),
                       t6_version='0.4.0', date=date.today(), results=[])
    def no_live_call(case, provider):
        raise RuntimeError('offline run must not call a model')
    return run_evals(cases, record=record, evaluate=no_live_call, provider=cast(Provider, None), cost_cap=0,
                     offline=True, offline_evaluate=lambda case: grade(case, outputs[case.id]))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('offline')
    prepare = sub.add_parser('export')
    prepare.add_argument('directory', type=Path)
    score = sub.add_parser('evaluate')
    score.add_argument('directory', type=Path)
    score.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'offline':
        result = offline_run(load_cases())
        for item in result.results:
            print(f"{item.case_id}: {'PASS' if item.passed else 'FAIL'}: {item.reason}")
        raise SystemExit(0 if len(result.results) == len(load_cases()) and all(item.passed for item in result.results) else 1)
    if args.command == 'export':
        export(args.directory)
    else:
        evaluate(args.directory, args.output)
