# t6-psr-brief

Snapshots the Notion **Projects** database into SQLite and drafts the weekly PSR briefings
(per-coordinator diffs) and the meeting minutes. Sends nothing itself: delivery goes through the
Telegram gateway's `POST /notify`.

Status: snapshots run on a schedule (Mon 11:00 and Fri 18:00, Asia/Manila), and the Sunday 16:00
briefing (changes only, no LLM) sends through the gateway. The Monday run also drafts the minutes in Notion (raw PM Notes, no LLM). The briefing lists "What you owe" per project (LLM). Not done: LLM condensing of the minutes, sending the minutes.

    pip install -e '.[dev]' && pytest

## Run on the Mac mini

psr-brief runs as its own container on the T6 Mac mini (Intel, Colima), next to the gateway, with its
own `compose.yaml`. An in-process scheduler takes a snapshot every Monday 11:00 and Friday 18:00
Asia/Manila time. It does not catch up missed slots: if the container is down at 11:00, take one by hand.

The scheduler fires on its own, so **do not start the container before Cyril's go-ahead**: the
first snapshot taken becomes the baseline. A bad config exits with code 2, and
`restart: unless-stopped` then restarts it in a loop; read `docker compose logs psr-brief`.

Config: non-secret values in `config.yaml` (see `config.example.yaml`), `NOTION_TOKEN` in `.env`
(see `.env.example`). A missing or invalid value fails boot with a message naming the key, never
its value. The SQLite file lives in `./data`, which the non-root container user (uid 1001) owns.

### Deploy

    git clone https://github.com/mclbautista/t6-psr-brief && cd t6-psr-brief   # first time only
    cp config.example.yaml config.yaml && cp .env.example .env                 # fill both in
    mkdir -p data && sudo chown 1001:1001 data && chmod 0700 data
    git pull && docker compose up -d --build
    docker compose logs -f psr-brief      # expect: serving db=... / scheduler next=... kind=...

Logs carry one line per job run with counts only (`snapshot id=... kind=monday baseline=True
changed=... unchanged=... removed=...`). No secrets, no note text.

### Rollback

Each build is tagged `psr-brief:<PSR_TAG>` (default `latest`). **Before every deploy**, tag the
running image; without that tag there is nothing to roll back to:

    docker tag psr-brief:latest psr-brief:previous      # before the new deploy
    PSR_TAG=previous docker compose up -d               # roll back
    # or: git checkout <old-commit> && docker compose up -d --build

`./data` is never touched by a rollback. Snapshots are append-only.

### Manual snapshot

    docker compose run --rm psr-brief snapshot --config /app/config.yaml --kind manual

`--kind` is `monday`, `friday` or `manual`. The first snapshot ever taken is flagged as the
baseline (planned: Mon 2026-11-09 11:00, after Cyril's go-ahead).

## Sunday briefing

Every Sunday 16:00 Asia/Manila each T6 Post Coordinator gets a DM built from the latest Friday
snapshot against the Monday before it, and Cyril gets one digest of every project. Everything goes
through the gateway's `POST /notify` (`PSR_SECRET` bearer, `format: "html"`). It is on only when the
briefing keys are in `config.yaml` (see `config.example.yaml`); `PSR_SECRET` goes in `.env`.

- A coordinator with no `TelegramUserID` in Contacts gets their briefing in the PSR group topic,
  prefixed with their @handle, and the digest says so.
- Idempotency keys are `psr-brief-<date>-<contact id>` and `psr-digest-<date>`, so a same-day rerun
  queues nothing new. A message that splits into parts adds `-2`, `-3`, ... to the later parts.
- A gateway error is retried 3 times with backoff; then the run carries on with the other messages
  and exits non-zero.
- **What you owe**: with `OPENROUTER_API_KEY` in `.env`, an LLM (OpenRouter, `llm_model` in config,
  default Claude Haiku) reads each project's PM Notes, Stage, Status, Health, Deadline and team and
  returns up to 15 items (`missing_date`, `missing_role`, `open_item`, `stale_field`). Every request
  sends `provider.zdr: true` and is never retried without it. An item must cite an exact PM Notes line
  or an empty property; code drops the rest. A project with no changes but owed items still appears.
  If the call fails for a project, its briefing says "Owed items unavailable this week", the failure
  is logged (project id only) and the digest names it. `--dry-print` without the key leaves owed
  items out; a real send needs it.
- `test_banner: true` adds "🧪 TEST" to every message (for the Nov 13 dry run).

    docker compose run --rm psr-brief brief --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm psr-brief brief --config /app/config.yaml               # send now

`--dry-print` still reads Contacts from Notion but needs no `PSR_SECRET`. **Do not run `brief`
without `--dry-print` before Cyril's go-ahead:** it messages the real coordinators.

## Minutes draft

With `meeting_notes_data_source` in `config.yaml`, the Monday 11:00 job takes its snapshot and then
drafts a page named `PSR MOM <YYYY-MM-DD>` in Meeting Notes: `Meeting Date`, a `Project` relation to
every project on the page, and `Team Attendees` copied from the previous `PSR MOM` page. The diff is
that Monday's snapshot against the Friday one before it. Groups, order and rules are in
`src/t6_psr_brief/mom.py` (spec section 8); bullets are the raw new PM Notes blocks. In-Progress and
Archiving projects always appear; everything else without changes goes under "Unchanged" at the
bottom (Notion only). ⚫️ Others lists the lines of the Projects database description's "Upcoming
Holidays and Leaves" whose month-and-day (or range) falls in the next 14 days.

    docker compose run --rm psr-brief draft-mom --config /app/config.yaml --date 2026-11-17

`draft-mom` re-drafts by hand (default date: today, Manila). It trashes an earlier **unsent** page for
that date and makes a new one, and exits 1 without touching a page whose `Sent at` is set. The
`Sent at` (date) property must exist on Meeting Notes (spec 3.3); it is only read here.
