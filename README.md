# t6-psr-brief

Snapshots the Notion **Projects** database into SQLite and drafts the weekly PSR briefings
(per-coordinator diffs) and the meeting minutes. Sends nothing itself: delivery goes through the
Telegram gateway's `POST /notify`.

Status: early scaffold. Done: snapshot store and diff core with tests. Not done: Notion fetcher,
briefing text, minutes draft, gateway client.

    pip install -e '.[dev]' && pytest
