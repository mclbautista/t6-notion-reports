# Issue tracker: GitHub

Issues and specs live in https://github.com/mclbautista/t6-psr-brief/issues.
Use the `gh` CLI from this clone; it infers the repository from `origin`.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
- Read: `gh issue view <number> --comments`; fetch labels with
  `gh issue view <number> --json labels`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
  Apply label and state filters as needed.
- Comment: `gh issue comment <number> --body-file <file>`.
- Label: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.
- “Publish to the issue tracker” means create a GitHub issue.
- “Fetch the relevant ticket” means read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares issue and PR numbers. Resolve ambiguous references with
`gh pr view <number>`, falling back to `gh issue view <number>`.

## Wayfinding operations

- Map: a single issue labeled `wayfinder:map`, with Notes,
  Decisions-so-far, and Fog sections.
- Children: link tickets as native GitHub sub-issues. If unavailable,
  use a task list in the map and `Part of #<map>` in each child.
  Use `wayfinder:<type>` labels: research, prototype, grilling, task.
- Blocking: use native issue dependencies:
  `gh api --method POST repos/mclbautista/t6-psr-brief/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`.
  Obtain the numeric database ID with
  `gh api repos/mclbautista/t6-psr-brief/issues/<number> --jq .id`.
  If unavailable, record `Blocked by: #<number>` in the child.
- Frontier: open children in map order with no open blockers or assignee.
  Native `issue_dependencies_summary.blocked_by` counts open blockers.
- Claim: `gh issue edit <number> --add-assignee @me`.
- Resolve: comment with the result, close the ticket, and add a context
  pointer to the map's Decisions-so-far.
