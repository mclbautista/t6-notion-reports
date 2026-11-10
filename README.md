# t6-notion-reports

Snapshots the Notion **Projects** and **Job Orders** databases into SQLite, drafts the weekly PSR briefings
(per-coordinator diffs) and the meeting minutes, and builds the daily JO shift reports. Sends nothing itself:
delivery goes through the Telegram gateway's `POST /notify`.

Status: snapshots run on a schedule (Mon 11:00 and Fri 23:30, Asia/Manila), and the Saturday 12:00
briefing sends through the gateway. The Monday run also drafts the minutes in Notion: their bullets are LLM-condensed when `OPENROUTER_API_KEY` is set, and stay raw PM Notes otherwise. The briefing consolidates each project's week with an LLM when that key is set. Approved minutes are sent to the PSR group topic set in `config.yaml` when the minutes owner ticks **Ready to send**.

    pip install -e '.[dev]' && pytest

## This repository is public

Anyone can read this repo and its whole history. Never commit:

- secrets: `NOTION_TOKEN`, `OPENROUTER_API_KEY`, `PSR_SECRET`, `SHIFT_SECRET`, `TS_AUTHKEY` or any other token or key. They go in `.env` only.
- real config: `config.yaml` (real Notion data source ids, Telegram chat, topic and user ids, gateway address). Keep `config.example.yaml` to placeholders.
- data: `data/`, `*.db` and any other SQLite file, snapshots or exported Notion content.
- real names: no client, show, coordinator or staff names anywhere, including test fixtures, docs and comments. Tests use made-up names (Oto, Ida, Juno, Nia, Show QAZ and so on).

The **Public repo safety** workflow (`.github/workflows/public-repo-safety.yml`, shared from t6-core) runs on every
push and pull request. It fails on tracked forbidden paths, on gitleaks findings and on any name from the
`T6_NAMES_DENYLIST` Actions secret (real names, one per line; the list itself is never committed). If it
finds a real secret, rotate it first, then remove it. Reviewed false positives go in `.gitleaksignore`.

Run the same check before you push (needs gitleaks 8.21.2, or add `--skip-secrets`):

    curl -fsSL -o /tmp/public_repo_safety.py \
      https://raw.githubusercontent.com/mclbautista/t6-core/main/scripts/public_repo_safety.py
    python3 /tmp/public_repo_safety.py --names-file ~/t6-names.txt

## Commands

`python -m t6_notion_reports <command>`. In the container, run them with
`docker compose run --rm notion-reports <command> --config /app/config.yaml` (`diff` has no `--config`; its `--db` defaults to `data/psr.db`).

- `serve`: the scheduler. This is the container's default command.
- `snapshot --kind monday|friday|manual`: one Projects snapshot.
- `jo-snapshot --kind 0500|1130|1900|manual`: one Job Orders snapshot.
- `shift-report --slot 0500|1130|1900 [--dry-print]`: one shift report.
- `brief [--dry-print] [--show-llm-input]`: the Saturday briefing now.
- `draft-mom [--date YYYY-MM-DD]`: re-draft the minutes in Notion.
- `send-ready-mom [--dry-print]`: send the approved minutes once.
- `diff [--old ID --new ID]`: what changed between two snapshots (default: latest Monday against the latest Friday).

## Run on the Mac mini

notion-reports runs as its own container on the T6 Mac mini (Intel, Colima), next to the gateway, with its
own `compose.yaml`. An in-process scheduler takes a snapshot every Monday 11:00 and Friday 23:30
Asia/Manila time. It does not catch up missed slots: if the container is down at 11:00, take one by hand.

The scheduler fires on its own, so **do not start the container before Cyril's go-ahead**: the
first snapshot taken becomes the baseline. A bad config exits with code 2, and
`restart: unless-stopped` then restarts it in a loop; read `docker compose logs notion-reports`.

Config: non-secret values in `config.yaml` (see `config.example.yaml`), `NOTION_TOKEN` in `.env`
(see `.env.example`). A missing or invalid value fails boot with a message naming the key, never
its value. The SQLite file lives in `./data`, which the non-root container user (uid 1001) owns.

### Tailnet and gateway networking

The pinned Tailscale sidecar follows [t6-netops' sidecar pattern](https://github.com/mclbautista/t6-netops#tailscale-sidecar).
Set `TS_AUTHKEY` in `.env` to a **non-ephemeral** auth key authorized for `tag:t6-reports`;
the sidecar advertises that tag, identifies itself as `t6-reports`, and saves its node identity
in the Compose `tailscale_state` volume. Keep the volume and Compose project name across redeploys
and reboots. Do not use `docker compose down -v` or remove the volume: that loses the identity and
can create a duplicate device. Kernel networking requires `/dev/net/tun` and `NET_ADMIN` in Colima.
Reports shares the sidecar's network namespace and waits for its health check. Use the VPS's 100.x
tailnet IP, not an assumed MagicDNS name. Tailnet ACLs, not this Compose file, must allow the reports
tag to queue port 8790 and deny tap port 8791; see the t6-netops policy. This change adds connectivity,
not the Frame.io queue client.

**Existing external `sixto` network:** Before deploying, back up the untracked local
`compose.override.yaml`. It currently attaches `notion-reports` to the external `sixto` network;
that is incompatible with `network_mode: service:tailscale`. Move that service's network attachment,
including any needed aliases, to `tailscale` instead. Preserve unrelated override settings and the
existing gateway's attachment. See `compose.sixto.example.yaml` for the network and the
`notion-reports` alias; merge its relevant parts into the existing override, rather than replacing
it blindly. The example is not auto-loaded. The network must already exist. If the gateway is on
`sixto`, this preserves the shared Docker network path; keep
the configured gateway `/notify` URL unchanged. If it uses a different network, attach the sidecar
to that network instead. Validate the merged model with `docker compose config --quiet` without
printing the resolved secrets. The sidecar may need the default network too if another existing
attachment depends on it; explicitly retain all required networks when overriding.

After an approved deployment, check `docker compose ps` and `docker compose logs tailscale` (avoid
sharing logs containing sensitive information). Verify a controlled `/notify` through the existing
gateway address using the normal approved delivery procedure, and test TCP from the running reports
container with a VPS tailnet IP substituted below (never a real key):

```sh
docker compose exec notion-reports python -c 'import socket; socket.create_connection(("<VPS_TAILNET_IP>", 8790), 5).close(); print("queue reachable")'
docker compose exec notion-reports python -c 'import socket; socket.create_connection(("<VPS_TAILNET_IP>", 8791), 5).close(); print("WARNING: tap reachable")'
```

The first must succeed and the second fail. A failed connection alone does not prove the ACL:
confirm the tap service is listening and reachable from an authorized gateway node, and check the
applied policy. Record the sidecar's `Self.ID` from `docker compose exec tailscale tailscale status --json`
(keep that output private), and confirm it is unchanged after a sidecar restart and reboot. Compose-initiated sidecar restarts
restart reports, but an automatic sidecar crash/restart can leave reports in a stale namespace;
recover with `docker compose up -d --force-recreate notion-reports` and recheck connectivity.

### Deploy

    git clone https://github.com/mclbautista/t6-notion-reports && cd t6-notion-reports   # first time only
    cp config.example.yaml config.yaml && cp .env.example .env                 # fill both in
    mkdir -p data && sudo chown 1001:1001 data && chmod 0700 data
    git pull && docker compose up -d --build
    docker compose logs -f notion-reports      # expect: serving db=... / scheduler next=... kind=...

Logs carry one line per job run with counts only (`snapshot id=... kind=monday baseline=True
changed=... unchanged=... removed=...`). No secrets, no note text.

### Rollback

Each build is tagged `notion-reports:<NOTION_REPORTS_TAG>` (default `latest`). **Before every deploy**, tag the
running image; without that tag there is nothing to roll back to:

    docker tag notion-reports:latest notion-reports:previous      # before the new deploy
    NOTION_REPORTS_TAG=previous docker compose up -d    # roll back
    # or: git checkout <old-commit> && docker compose up -d --build

`./data` is never touched by a rollback. Snapshots are append-only.
The image-only rollback keeps the sidecar and its state. To roll back the **network topology** too,
stop the sidecar while the new configuration is still in place (`docker compose stop tailscale`),
then restore the pre-sidecar Compose revision and the backed-up override together (reattach
`notion-reports` to `sixto`), then recreate the service using that revision's Compose configuration.
Keep the `tailscale_state` volume even if the old revision does not use it; do not run `down -v`.
Check `/notify` again. This stops use of the reports tailnet node, so queue access is lost until the
sidecar topology is restored. Do not remove the device or volume as part of rollback.

### Manual snapshot

    docker compose run --rm notion-reports snapshot --config /app/config.yaml --kind manual

`--kind` is `monday`, `friday` or `manual`. The first snapshot ever taken is flagged as the
baseline (planned: Mon 2026-11-09 11:00, after Cyril's go-ahead).

## Saturday briefing

Every Saturday 12:00 Asia/Manila each T6 Post Coordinator gets a DM built from the latest Friday
snapshot against the Monday before it, and Cyril gets one digest of every project. Everything goes
through the gateway's `POST /notify` (`PSR_SECRET` bearer, `format: "html"`). It is on only when the
briefing keys are in `config.yaml` (see `config.example.yaml`); `PSR_SECRET` goes in `.env`.

- `coordinator_recipients` optionally limits Coordinator DM delivery to a list of Contacts page ids. Omit it
  to allow all coordinators; use `[]` to disable all Coordinator DMs and their group fallbacks. A nonempty
  list allows only those coordinators. Excluded coordinators get no DM or fallback and are not named in
  the delivery-specific quiet/fallback notices. Their projects still appear in the full Master PSR Digest
  and still pass through the normal LLM/status gate. This setting does not change Minutes or JO shift delivery.
- `digest_recipients` independently selects Master PSR Digest recipients by Contacts page id; it defaults
  to `cyril_contact_id` when omitted. Keep excluded entries commented out for a temporary rollout.
- An allowed coordinator with no `TelegramUserID` in Contacts gets their briefing in the PSR group topic,
  prefixed with their @handle, and the digest says so.
- Idempotency keys are `notion-reports-<date>-<contact id>` for each coordinator and `psr-digest-<date>` for the digest (`psr-digest-<date>-<contact id>` when there is more than one digest recipient), so a same-day rerun
  queues nothing new. A message that splits into parts adds `-2`, `-3`, ... to the later parts.
- **PSR message splitting policy (owner approved for #96):** normal messages split only between
  complete projects. A new part repeats the coordinator (in the digest) and section heading, marking
  an already-started section `(cont.)`. The approved exception to #40's project-only boundary rule
  is a single project that exceeds the message limit: preserve all content and continue it under its
  project identity and section, both marked `(cont.)`, plus its coordinator in the digest. Split at
  lines first; an oversized line splits at words, or characters for an unbroken word, preserving valid
  HTML and the same context on every fragment. Digest warnings and tails continue under the digest
  title `(cont.)`, never as untitled messages. Every part, including repeated context and any test
  banner, stays within Telegram's 4096-character limit.
- A gateway error is retried 3 times with backoff; then the run carries on with the other messages
  and exits non-zero.
- **LLM consolidation**: with `OPENROUTER_API_KEY` in `.env`, each project in the status gate gets one LLM call
  (OpenRouter, `llm_model` in config, ZDR on every request, temperature 0, never retried). The payload is that
  project alone: today's date, name, Stage, Status, Project Health, the three dates and which are blank and
  relevant, the numbered PM Notes lines of the last `pm_notes_weeks` (52) weeks plus older blocks with a future
  date, the property changes since the last brief and since the PSR, and the project's lines from the latest
  PSR minutes page (as it is in Notion now, with its date, last-edited time and Sent at).
  **Free-text privacy policy (owner approved for #94):** redact known Contacts names, aliases and handles,
  known contact/project page ids, Telegram identifiers/links, Notion identifiers/links, and known references
  to other snapshot projects before sending free text, including bracketed and punctuated values.
  Full contact names and aliases match in every casing even when they are ordinary words: a known
  name or alias `Will` also hides the verb `will`. This sacrifices some useful language to protect known
  identities. Partial name words and project codes that are ordinary words retain their existing
  capitalized/all-capitals matching rule, so ambiguous lowercase ordinary language can remain.
  The target project's own identity remains available. **Limitation:** unknown names absent from Contacts,
  unknown other-project names absent from the snapshots, and ambiguous lowercase partial-name words
  cannot reliably be identified and may remain; this is a known-values boundary, not universal name detection.
  Local numbered placeholders protect outbound text; grounding checks the exact protected source quotes,
  then accepted output restores names locally (identifiers stay hidden). The reply is structured: `last_update`, `changes` and
  `concerns`. Code checks each item on its own: the quote must be the cited line (ignoring bullet marks and
  whitespace) and facts must be grounded in that cited line; a failing item is dropped.
  A `source_conflict` or `health_vs_evidence` Concern may include `supporting_citations` with exact source,
  line id and quote for each additional evidence line (including snapshot lines). Only those explicitly
  cited lines may supply comparison facts; health comparisons may also use `project_health`. Other
  Concern kinds and changes remain scoped to their primary line. ISO dates may be paraphrased with month
  names while preserving their written year. These checks enforce evidence provenance, not the semantic
  correctness of a comparison; human-reviewed model evaluation remains necessary. The changes replace the raw changes list, `after_psr` ones say "(after PSR)", and Concerns go through
  the Concern store. A valid `last_update` (a real "As of" header, not in the future) sets the weeks since
  update; otherwise Notion's last-edited time does.
  If a call fails or its JSON is invalid, that project shows its flags and raw changes marked "(LLM unavailable)",
  the failure is logged (project id only) and the digest names it; its open Concerns carry over. Without the key
  a real send marks every project "(LLM unavailable)"; `--dry-print` without it leaves the LLM out.
- Leave clashes: once per run, one more LLM call parses the Projects database's "Upcoming Holidays and Leaves"
  lines (it gets those lines and today's date, nothing about projects) into entries with exact quotes; an entry
  with a quote that is not a line, or invalid dates, is dropped. Each leave's name is matched to Contacts
  (Aliases, then guarded fuzzy; onboarded Contacts first). A leave by someone in the project's team relations
  within `leave_clash_days` (default 14) of its Playdate, Submission Deadline or Archival Deadline becomes one
  Concern, aged through the Concern store. Holidays and office closures never clash. Names nobody matched are
  listed in Cyril's digest only. If the call fails there are no clash Concerns that run, and the digest says
  leaves were unavailable.
- `brief --dry-print --show-llm-input` prints every payload in full and calls no LLM, so you can read exactly
  what would leave the machine first.
- `test_banner: true` adds "🧪 TEST" to every message (for the Nov 13 dry run).

    docker compose run --rm notion-reports brief --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm notion-reports brief --config /app/config.yaml               # send now

`--dry-print` still reads Contacts from Notion but needs no `PSR_SECRET`. **Do not run `brief`
without `--dry-print` before Cyril's go-ahead:** it messages the real coordinators.

## Minutes draft

With `meeting_notes_data_source` in `config.yaml`, the Monday 11:00 job takes its snapshot and then
drafts a page named `PSR MOM <YYYY-MM-DD>` in Meeting Notes: `Meeting Date`, a `Project` relation to
every project on the page, and `Team Attendees` copied from the previous `PSR MOM` page. The diff is
that Monday's snapshot against the Friday one before it. Groups, order and rules are in
`src/t6_notion_reports/mom.py` (spec section 8); bullets are the raw new PM Notes blocks, condensed by the LLM when `OPENROUTER_API_KEY` is set (`condense.py`, spec 8.3): code rejects any output whose date, number, episode or capitalised word (names) is not in the project's PM Notes, or with more than one date from an older block (which must still be upcoming); a rejected or failed project keeps the raw bullets and is marked `(raw)`. Every project with notes has a "Raw source" toggle. In-Progress and
Archiving projects always appear; everything else without changes goes under "Unchanged" at the
bottom (Notion only). ⚫️ Others lists the lines of the Projects database description's "Upcoming
Holidays and Leaves" whose month-and-day (or range) falls in the next 14 days.

    docker compose run --rm notion-reports draft-mom --config /app/config.yaml --date 2026-11-17

`draft-mom` re-drafts by hand (default date: today, Manila). It trashes an earlier **unsent** page for
that date and makes a new one, and exits 1 without touching a page whose `Sent at` is set. The
`Sent at` (date) property must exist on Meeting Notes (spec 3.3); it is only read here.

## Sending the approved minutes

The minutes owner edits the draft in Notion and ticks `Ready to send`. Every 5 minutes (when the briefing keys
and `meeting_notes_data_source` are in `config.yaml`) the scheduler looks for `PSR MOM` pages with
`Ready to send` ticked and `Sent at` empty, reads each page as it is now, and sends it to the PSR group
topic: one message per group (header first, 🔴 🟠 🟡 🔵 🟢, ⚫️ Others last), split on bullet boundaries
past 4,096 characters. The "Unchanged" heading and everything after it, and any toggle, stay in Notion.
Keys are `psr-mom-<meeting date>-<part n>`. `Sent at` is set only once every part is accepted; on a
gateway failure it stays empty, the next check retries, and the gateway drops parts it already accepted.
`Ready to send` and `Sent at` must exist on Meeting Notes (spec 3.3).

    docker compose run --rm notion-reports send-ready-mom --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm notion-reports send-ready-mom --config /app/config.yaml               # check once now

`test_banner` adds the 🧪 TEST banner here too.

## JO shift reports

Three Job Orders reports go to their configured destinations every day (weekends and holidays included), Asia/Manila:
05:00 night end of shift, 11:30 day mid shift, 19:00 day end of shift. At each slot the scheduler takes a
Job Orders snapshot (kind `0500`, `1130` or `1900`) and then sends that slot's report. The Friday Projects snapshot (23:30) is a separate slot. Turn it on in `config.yaml`:

    jo_data_source: "…"
    gateway_url: "http://192.0.2.10:8780"
    shift_reports_enabled: true
    shift_destinations:              # at least one destination in total
      mid:   [{chat: -1000000000001, topic: 1}]   # a chat, with an optional topic
      day:   [100000001, 100000002]               # a bare positive number is a user id (a DM)
      night: [100000001, {chat: -1000000000001, topic: 2}]
    ops_chat_id: 100000001           # optional; the Notion-outage alert goes here (else to the first chat entry of that report's list)
    test_banner: true                # 🧪 TEST on every part

`shift_destinations` lists where each report goes: `mid` (the midshift report), `day` (end of day shift) and
`night` (end of night shift). An entry is an integer user id (a DM) or a mapping with an integer `chat` and an
optional integer `topic`. A group chat id (negative) must use the mapping form; a zero or negative bare number stops boot. All three names are optional and a missing or empty list means that report is not sent.
Unknown names, non-integer ids, extra mapping keys and a repeated destination in one list stop boot with a
message naming the key and entry number, never the id. `shift_chat_id` and `shift_topic_id` were replaced by
`shift_destinations` and stop boot if they are still present. Put real ids only in the deployed `config.yaml`.
Every part of a report goes to every destination of its list, in config order.

`SHIFT_SECRET` goes in `.env`. The shift reports authenticate to the gateway as the `shift` module with that
secret, never `PSR_SECRET`, so the gateway needs `modules: shift: { secret_env: SHIFT_SECRET }`. A bad or
missing shift key stops boot with a message naming the key. With `shift_reports_enabled` false or absent,
nothing about the shift reports is scheduled and the other jobs behave as before.

Idempotency keys are `shift-<YYYY-MM-DD>-<slot>-<dest>` for part 1, then `-2`, `-3`… (`<dest>` is `u<id>` for a DM,
`c<chat>` for a chat, `c<chat>t<topic>` for a chat topic), with the date in Asia/Manila,
so a rerun of the same slot and day dedupes per destination and queues nothing new. If the gateway fails after the client's retries, the
slot logs `job failed kind=shift<slot> error=GatewayError` and the scheduler carries on; a missed slot is not
made up. Each run logs one line with counts only (slot, snapshot, baseline, unfinished, received, processed,
accomplished, parts, destinations, undelivered).

After the last send the job checks delivery: every 15 seconds, for up to 5 minutes (earlier once all are final),
it asks the gateway about each queued message. A destination counts as undelivered if it was rejected at send,
or any part `failed`, is still `queued` at the end, or whose status could not be read (`unknown`). If any
destination is undelivered, one alert (key `shift-<date>-<slot>-undelivered`) is sent by the same rule as the
Notion-outage alert, naming the slot and each such destination by id with its state; it holds no report text.
No alert when everything was delivered. `undelivered=<n>` is in the run log line, without ids.

    docker compose run --rm notion-reports shift-report --slot 1900 --config /app/config.yaml --dry-print   # print, send nothing
    docker compose run --rm notion-reports shift-report --slot 1900 --config /app/config.yaml               # send one slot now

If Notion cannot be read at a slot, the job retries every minute for 30 minutes. If it stays down it queues one
alert (key `shift-<date>-<slot>-alert`, naming the slot and the error type, nothing from Notion) to `ops_chat_id`,
or to the first chat entry of that report's list, marked 🚨, when `ops_chat_id` is not set, and skips the slot: no report, no snapshot, no
catch-up. The next slot compares with the latest earlier snapshot and says so.

Do not send by hand before the dry-run go-ahead: it posts to every destination of the slot's list.
