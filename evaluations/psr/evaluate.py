"""Offline preparation and triage only. This module has no model client or network calls."""
import argparse
from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('export')
    prepare.add_argument('directory', type=Path)
    score = sub.add_parser('evaluate')
    score.add_argument('directory', type=Path)
    score.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        export(args.directory)
    else:
        evaluate(args.directory, args.output)
