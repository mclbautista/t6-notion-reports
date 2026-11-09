"""Offline harness integrity, never model-quality or human-review evidence."""
import json
from pathlib import Path
import runpy

HARNESS = Path(__file__).resolve().parents[1] / 'evaluations' / 'psr' / 'evaluate.py'


def test_export_and_triage_distinguish_missing_dropped_and_accepted_replies(tmp_path):
    harness = runpy.run_path(str(HARNESS))
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    harness['export'](inputs)
    manifest = json.loads((inputs / 'manifest.json').read_text())
    assert len(manifest['cases']) == 21
    assert {c['issue'] for c in manifest['cases']} == {97, 98}
    outputs.mkdir()
    payload = json.loads((inputs / 'open-pending.input.json').read_text())
    line = payload['pm_notes'][1]
    reply = {'concerns': [{'kind': 'open_item', 'source': 'pm_notes', 'line_id': line['line_id'],
                           'quote': line['text'], 'text': 'Pending client approval.'}]}
    (outputs / 'open-pending.output.json').write_text(json.dumps(reply))
    # An unsupported quote is discarded even if the model gave the expected kind.
    reply['concerns'][0]['quote'] = 'invented'
    (outputs / 'open-tbc.output.json').write_text(json.dumps(reply))
    report = tmp_path / 'report.json'
    harness['evaluate'](outputs, report)
    results = {r['id']: r for r in json.loads(report.read_text())['results']}
    assert results['open-pending']['missing_kinds'] == []
    assert results['open-pending']['accepted_item_counts']['concerns'] == 1
    assert results['open-tbc']['missing_kinds'] == ['open_item']
    assert results['open-tbc']['accepted_item_counts']['concerns'] == 0
    assert results['open-waiting']['status'].startswith('NOT RUN')
    assert all(r['human_review'] == 'PENDING' for r in results.values())
