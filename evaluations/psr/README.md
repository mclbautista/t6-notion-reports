# Synthetic PSR evaluation preparation (#97 and #98)

**Draft. Human review pending. Live model evaluation not run. Neither issue is accepted or complete.**

All 21 cases use invented project content. The offline harness uses the production payload builder,
current consolidation prompt/schema, and production validator. It does not import a model client,
read credentials, contact a service, or send a brief. It produces no evidence of model quality.

## Prepare locally

From the repository root, with project development dependencies installed:

```sh
PYTHONPATH=src python evaluations/psr/evaluate.py export /tmp/psr-eval-inputs
PYTHONPATH=src python evaluations/psr/evaluate.py evaluate /tmp/psr-eval-outputs /tmp/psr-eval-triage.json
```

The first command writes exact payloads, system prompt, schema, prompt hash and draft rubric manifest.
The second reads externally supplied `<case-id>.output.json` structured replies; missing files are
reported as NOT RUN. Empty outputs are not a successful evaluation. Automated triage checks required
and forbidden Concern kinds after production validation and reports raw/parsed/accepted counts.
It does not judge meaning, fluency, winner selection, or per-source semantic grounding.

## Human dataset review required before any live run

Read `cases.json` alongside exported payloads. Record reviewer, date, corpus revision, per-case
approval/correction and rationale in a separate review record. Verify each required Concern and
negative control. Expected kinds are minimum requirements unless explicitly forbidden; additional
Concerns require manual assessment. Review the Taglish sentence with a fluent speaker. Do not require
one exact sentence: require a complete fluent sentence preserving the source's language and meaning.
Confirm the same-day timestamp policy: a date-only PM Notes header does not establish ordering
against an intraday edit. Equal timestamps and absent timestamps must not yield an arbitrary winner.

Coverage includes all five action markers, Taglish, both positive-health/blocker variants, both
negative-health/stale-update variants, forgotten versus subsequently mentioned milestones, resolved
and healthy controls, and PM Notes/Minutes/snapshot alternately newer. Conflict cases include equal,
missing, and mixed-precision timestamps. The missing-time case deliberately omits the snapshot window
because the production Window requires a datetime; PM Notes and Minutes remain in disagreement.

## Pending live evaluation protocol

A human must approve the synthetic corpus and a non-production evaluation environment first.
No live execution is provided or authorized by these preparation commands. Once separately authorized,
run the actual configured model/version with the exported production prompt, schema and input. Retain:

- Exact model identifier and provider-reported resolved version, date, approved environment, generation
  settings (production uses temperature 0 and zero-data-retention routing), repository/corpus revision.
- Exact prompts and schema, each raw structured output, request metadata excluding secrets, errors,
  automated triage, and human judgments. Never retain tokens, authorization headers or real projects.
- Per-case detection, unsupported additions, exact quote/source/line correctness, fluent-sentence judgment,
  semantic grounding, and production validation drops. A raw Concern dropped by validation is a failure.
- For unambiguous recency, require a submission-deadline change using the winning value and exact winning line;
  reject stale values asserted as current and reject source_conflict. For ambiguity, require a grounded
  source_conflict from PM Notes or Minutes and reject an unsupported definitive winner.

The automated validator requires a PM Notes/Minutes primary citation for Concerns. Comparison Concerns
may explicitly cite additional PM Notes, Minutes or snapshot lines through `supporting_citations`; every
supporting source, line id and exact quote must validate. A snapshot winner should be cited in a change. The snapshot field uses ISO dates while other lines use month names;
review equivalent dates semantically. Source timestamps, not source category or sent_at, decide recency.

For every failure, retain the failed output and a limitation or remediation note. Re-run a versioned
corpus after changes and retain prior results. Acceptance requires every expected Concern, no unsupported
Concern, fluent grounded accepted sentences, and correct source selection on the human-reviewed set.
Do not substitute fixture tests, prompt inspection, or this harness's automated triage for that evidence.
