# Domain docs

## Layout and discovery

This is a single-context repository:
- `CONTEXT.md` at the root holds domain vocabulary.
- `docs/adr/` holds architecture decisions.

Before exploring, read `CONTEXT.md` and ADRs relevant to the work.
If `CONTEXT-MAP.md` exists, follow it to relevant per-context documents,
including context-specific ADR directories.

If these documents do not exist, proceed silently. Do not flag their
absence or suggest creating them upfront. The domain-modeling skill
creates them lazily as terms and decisions are resolved.

## Vocabulary

Use the glossary's terms in issues, proposals, hypotheses, and tests.
Do not substitute synonyms the glossary explicitly avoids.
If a concept is missing, reconsider whether it belongs, or note the gap
for domain modeling.

## ADR conflicts

Explicitly identify any proposal that contradicts an existing ADR,
with the ADR reference and why the decision should be reconsidered.
