# t6-psr-brief

Snapshots the Notion **Projects** database into SQLite and drafts the weekly PSR briefings
(per-coordinator diffs) and the meeting minutes. Sends nothing itself: delivery goes through the
Telegram gateway's `POST /notify`.

Status: early. Done: Notion fetcher, snapshot store (kinds, baseline, removed, content hash), diff core,
`snapshot` command. Not done: PM Notes parsing, briefing text, minutes draft, gateway client.

    NOTION_TOKEN=... python -m t6_psr_brief snapshot --kind manual --projects-ds <id> --contacts-ds <id>

    pip install -e '.[dev]' && pytest
