# t6-notion-reports

Snapshots the Notion **Projects** database into SQLite and drafts the weekly PSR briefings
(per-coordinator diffs) and the meeting minutes. Sends nothing itself: delivery goes through the
Telegram gateway's `POST /notify`.

Status: snapshots run on a schedule (Mon 11:00 and Fri 18:00, Asia/Manila), and the Sunday 16:00
briefing (changes only, no LLM) sends through the gateway. The Monday run also drafts the minutes in Notion (raw PM Notes, no LLM). The briefing lists "What you owe" per project (LLM). Approved minutes are sent to the PSR group topic when Leizette ticks **Ready to send**. Not done: LLM condensing of the minutes.

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

    git clone https://github.com/mclbautista/t6-notion-reports && cd t6-notion-reports   # first time only
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
  is logged (project id only) and the digest names it. Without the key a real send
  treats every project as failed ("unavailable"); `--dry-print` without it leaves owed items out.
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
`src/t6_psr_brief/mom.py` (spec section 8); bullets are the raw new PM Notes blocks, condensed by the LLM when `OPENROUTER_API_KEY` is set (`condense.py`, spec 8.3): code rejects any output whose date, number, episode or capitalised word (names) is not in the project's PM Notes, or with more than one date from an older block (which must still be upcoming); a rejected or failed project keeps the raw bullets and is marked `(raw)`. Every project with notes has a "Raw source" toggle. In-Progress and
Archiving projects always appear; everything else without changes goes under "Unchanged" at the
bottom (Notion only). ⚫️ Others lists the lines of the Projects database description's "Upcoming
Holidays and Leaves" whose month-and-day (or range) falls in the next 14 days.

    docker compose run --rm psr-brief draft-mom --config /app/config.yaml --date 2026-11-17

`draft-mom` re-drafts by hand (default date: today, Manila). It trashes an earlier **unsent** page for
that date and makes a new one, and exits 1 without touching a page whose `Sent at` is set. The
`Sent at` (date) property must exist on Meeting Notes (spec 3.3); it is only read here.

## Sending the approved minutes

Leizette edits the draft in Notion and ticks `Ready to send`. Every 5 minutes (when the briefing keys
and `meeting_notes_data_source` are in `config.yaml`) the scheduler looks for `PSR MOM` pages with
`Ready to send` ticked and `Sent at` empty, reads each page as it is now, and sends it to the PSR group
topic: one message per group (header first, 🔴 🟠 🟡 🔵 🟢, ⚫️ Others last), split on bullet boundaries
past 4,096 characters. The "Unchanged" heading and everything after it, and any toggle, stay in Notion.
Keys are `psr-mom-<meeting date>-<part n>`. `Sent at` is set only once every part is accepted; on a
gateway failure it stays empty, the next check retries, and the gateway drops parts it already accepted.
`Ready to send` and `Sent at` must exist on Meeting Notes (spec 3.3).

    docker compose run --rm psr-brief send-ready-mom --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm psr-brief send-ready-mom --config /app/config.yaml               # check once now

`test_banner` adds the 🧪 TEST banner here too.

## JO shift reports

Three Job Orders reports go to one group topic every day (weekends and holidays included), Asia/Manila:
05:00 night end of shift, 12:00 day midshift, 18:00 day end of shift. At each slot the scheduler takes a
Job Orders snapshot (kind `0500`, `1200` or `1800`) and then sends that slot's report. On Friday 18:00 the
Projects snapshot runs first. Turn it on in `config.yaml`:

    jo_data_source: "…"
    gateway_url: "http://192.168.88.151:8780"
    shift_reports_enabled: true
    shift_chat_id: -1000000000000    # required
    shift_topic_id: 1                # optional
    ops_chat_id: 274983989           # optional; the ops alert (a later ticket) will use it
    test_banner: true                # 🧪 TEST on every part

`SHIFT_SECRET` goes in `.env`. The shift reports authenticate to the gateway as the `shift` module with that
secret, never `PSR_SECRET`, so the gateway needs `modules: shift: { secret_env: SHIFT_SECRET }`. A bad or
missing shift key stops boot with a message naming the key. With `shift_reports_enabled` false or absent,
nothing about the shift reports is scheduled and the other jobs behave as before.

Idempotency keys are `shift-<YYYY-MM-DD>-<slot>` for part 1, then `-2`, `-3`…, with the date in Asia/Manila,
so a rerun of the same slot and day queues nothing new. If the gateway fails after the client's retries, the
slot logs `job failed kind=shift<slot> error=GatewayError` and the scheduler carries on; a missed slot is not
made up. Each run logs one line with counts only (slot, snapshot, baseline, unfinished, received, processed,
accomplished, parts).

    docker compose run --rm psr-brief shift-report --slot 1800 --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm psr-brief shift-report --slot 1800 --config /app/config.yaml               # send one slot now

Do not send by hand before the dry-run go-ahead: it posts to `shift_chat_id`.
