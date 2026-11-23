"""A fake t6 API client that serves the same data as a FakeNotion, in the shape the verbs return."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from t6.client import Unavailable
from t6.verbs import Done, Preview, Refused

MANILA = timezone(timedelta(hours=8))

# The API's rules for Projects and Job Orders, as a test double: the app calls the verbs and applies none of this.
_DATED = {"in-progress", "in-progress tvc"}
_IN_PROGRESS = _DATED | {"airing"}
_ON_HOLD = {"standby", "archiving"}
_INCOMING = {"leads", "lead", "negotiations", "proposal made"}
_ROLES = ("editor_ids", "assistant_editor_ids", "online_editor_ids", "colorist_ids", "sound_tech_ids",
          "post_coordinator_ids", "director_ids", "cinematographer_ids")
_HEALTH_NAMES = ("Closed", "Blocked", "Delayed", "Urgent", "Advanced", "On Track")


def health_name(health):
    """The JO Health formula read by its words, ignoring emoji and case (core #637); None when blank or unknown."""
    words = " ".join(re.sub(r"[^\w\s]", "", str(health or "")).split()).casefold()
    return next((n for n in _HEALTH_NAMES if n.casefold() == words), None)


def status_key(status):
    return " ".join(re.sub(r"[^\w\s-]", "", str(status or "")).split()).lower()


def status_group(status):
    key = status_key(status)
    return "in_progress" if key in _IN_PROGRESS else "on_hold" if key in _ON_HOLD else "incoming" if key in _INCOMING else None


def project_rules(status, roles_set=(), health=None):
    """The rule results ``project list`` adds to a Project. ``roles_set`` holds the role attributes that have someone."""
    key = status_key(status)
    return {
        "status_key": key, "status_group": status_group(status), "snapshotted": key not in {"archived", "lost"},
        "missing_roles": [r for r in _ROLES if r not in roles_set] if key in _IN_PROGRESS else [],
        "relevant_dates": (["playdate", "submission_deadline"] if key in _IN_PROGRESS else
                           ["archival_deadline"] if key == "archiving" else ["playdate"] if key in _INCOMING else []),
        **health_rules(health),
    }


_HEALTH_ORDER = {"critical": 0, "at risk": 1, "on track": 2, "incoming": 3}


def health_rules(health):
    """``health_missing`` and ``health_rank`` of the select "Project Health": Critical 0, At Risk 1, On Track 2, Incoming 3,
    any other value 4, blank 5."""
    key = status_key(health)
    return {"health_missing": not key, "health_rank": _HEALTH_ORDER.get(key, 4) if key else 5}


def _day(value):
    if not value:
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (stamp.astimezone(MANILA) if stamp.tzinfo else stamp).date()


def _text(prop):
    return "".join(t.get("plain_text", "") for t in (prop or {}).get(prop.get("type"), [])) if prop else ""


def _body(blocks):
    out = []
    for b in blocks:
        content = b.get(b["type"]) or {}
        spans = [{"text": r["plain_text"], "bold": bool((r.get("annotations") or {}).get("bold")),
                  "code": bool((r.get("annotations") or {}).get("code")), "url": r.get("href")}
                 for r in content.get("rich_text") or []]
        out.append({"kind": b["type"], "text": "".join(s["text"] for s in spans), "spans": spans,
                    "checked": content.get("checked") if b["type"] == "to_do" else None,
                    "children": _body(b.get("_children", []))})
    return out


_ROLE_PROPERTY = {
    "editor_ids": "Editors", "assistant_editor_ids": "Assistant Editor", "online_editor_ids": "Online Editor",
    "colorist_ids": "Colorist", "sound_tech_ids": "Sound Tech", "post_coordinator_ids": "T6 Post Coordinator",
    "director_ids": "Director", "cinematographer_ids": "Cinematographer",
}

ROLES = tuple(_ROLE_PROPERTY.values())  # the required roles, as Project property names


class FakeConcerns:
    """The Concern store of the API, with the meaning of ``concern observe``: a finding is new or known; an open Concern
    the findings lack is "not seen" and stays open (the weekly run closes nothing). A person closes with ``close``.
    Give one store to several FakeNotion instances to keep the history across runs."""

    def __init__(self):
        self.items: list[dict] = []
        self.runs: list[str] = []
        self.requests: dict[str, tuple[str, dict]] = {}
        self.observed: list[object] = []  # the inputs of each call, dry runs included
        self.scans: list[object] = []  # the inputs of each ``concern scan``
        self.recorded: list[tuple[str, str, str, str | None, str]] = []  # (concern, chat, message_id, brief_date, key), once done

    def close(self, text_part, on):
        """A person closes the open Concern whose text holds ``text_part``, on the ISO date ``on``."""
        for item in self.open(text_part):
            item["state"], item["closed_on"] = "closed", on

    def snooze(self, text_part):
        """A human operator snoozes the open Concern whose text holds ``text_part``."""
        for item in self.open(text_part):
            item["state"] = "snoozed"

    def open(self, text_part=""):
        return [i for i in self.items if i["state"] != "closed" and text_part in i["text"]]

    def observe(self, inputs, ref):
        self.observed.append(inputs)
        raw = inputs.model_dump(exclude={"dry_run"})
        if not inputs.dry_run and inputs.idempotency_key in self.requests:
            fingerprint, result = self.requests[inputs.idempotency_key]
            if fingerprint == repr(raw):
                return Done(result=result)
            return Refused(rule="idempotency_key", reason="Idempotency key was used for a different request")
        items = [dict(i) for i in self.items] if inputs.dry_run else self.items
        seen, counts = set(), {"new": 0, "known": 0, "not_seen": 0, "unavailable": 0, "skipped": 0, "projects_with_both": 0, "closed": 0}
        for f in inputs.findings:
            project = ref(f.project)
            key = " ".join(f.text.split()).casefold()
            found = next((i for i in items if ref(i["project"]) == project and i["key"] == key and i["state"] != "closed"), None)
            if found is None:
                found = {"id": f"t6c_{len(items) + 1}", "project": project, "key": key, "text": f.text, "source": f.source,
                         "opened_on": inputs.run_date, "state": "open", "closed_on": None, "owner": f.owner}
                items.append(found)
                counts["new"] += 1
            else:
                found["state"] = "snoozed" if found["state"] == "snoozed" else "open"
                counts["known"] += 1
            found["evidence_at"] = datetime.fromisoformat(inputs.run_date).replace(tzinfo=ZoneInfo("Asia/Manila")).timestamp()
            seen.add(found["id"])
        missing = {(ref(u.project), u.source) for u in inputs.unavailable}
        for i in items:
            if i["state"] != "closed" and i["id"] not in seen and i["source"] in ("flag_rule", "pm_notes_read", "leave"):
                counts["unavailable" if (ref(i["project"]), i["source"]) in missing else "not_seen"] += 1
        today = date.fromisoformat(inputs.run_date)
        before = max([r for r in self.runs if r < inputs.run_date], default="0000-00-00")
        projects: dict[str, dict] = {}
        for i in sorted(items, key=lambda i: (i["opened_on"], i["id"])):
            entry = projects.setdefault(ref(i["project"]), {"project_id": ref(i["project"]), "open": [], "closed_since_last_run": []})
            if i["state"] != "closed":
                age = (today - date.fromisoformat(i["opened_on"])).days
                entry["open"].append({"id": i["id"], "text": i["text"], "source": i["source"], "opened_on": i["opened_on"],
                                      "age_days": age, "weeks": age // 7, "chronic": age > 28, "state": i["state"],
                                      "seen_this_run": i["id"] in seen})
            elif i["closed_on"] >= before:
                entry["closed_since_last_run"].append({"id": i["id"], "text": i["text"]})
        result = {"run_date": inputs.run_date, "projects": sorted(projects.values(), key=lambda p: p["project_id"]), "counts": counts}
        if not inputs.dry_run:
            self.runs.append(inputs.run_date)
            self.requests[inputs.idempotency_key] = (repr(raw), result)
        return Done(result=result)


class FakeApi:
    """Every call returns Unavailable, like an unreachable API, while ``down`` or ``notion.down`` is true.

    The meeting-notes writes land in the wrapped FakeNotion's meetings, so a later read sees them. ``fail`` maps a
    verb ("create", "append", "trash", "mark-sent") to an outcome returned instead of doing it, and ``writes`` lists
    the (verb, input model) pairs in call order.
    """

    def __init__(self, notion, down=False, fail=None):
        self.notion, self._down = notion, down
        self.concerns = getattr(notion, "concerns", None) or FakeConcerns()
        self.calls: list[str] = []
        self.fail = dict(fail or {})
        self.writes: list[tuple[str, object]] = []

    @property
    def down(self):
        return self._down or getattr(self.notion, "down", False)

    def _ref(self, page_id):
        """The id the API gives for a page: its T6 ID when ``notion.t6_ids`` binds one, else the page id."""
        return getattr(self.notion, "t6_ids", {}).get(page_id, page_id)

    def _done(self, name, result):
        self.calls.append(name)
        return Unavailable("connection refused") if self.down else Done(result=result)

    def person_list(self, inputs):
        people = []
        for c in self.notion.contacts:
            p = c["properties"]
            uid = p.get("TelegramUserID")
            number = (uid or {}).get("number") if (uid or {}).get("type") == "number" else _text(uid).strip()
            people.append({
                "page_id": c["id"], "t6_id": self._ref(c["id"]) if self._ref(c["id"]) != c["id"] else None, "name": _text(p.get("Name")),
                "telegram_handle": _text(p.get("TelegramHandle")) or None,
                "telegram_user_id": int(number) if number not in (None, "") and str(number).isdigit() else None,
                "aliases": [a.strip() for a in _text(p.get("Aliases")).split(",") if a.strip()],
            })
        return self._done("person list", {"people": people})

    def person_find(self, inputs):
        """The API's rule is not in this repo. The fake: a name matches a Contact by its name, an alias or its first word;
        with ``prefer_telegram`` the Contacts that have a Telegram user ID are searched first; more than one is a
        tie (no match, needs review). ``notion.find_overrides[name]`` gives a scripted result for a name."""
        people = self.person_list(None).result["people"] if not self.down else []
        self.find_calls = getattr(self, "find_calls", []) + [(list(inputs.mentions or []), inputs.prefer_telegram)]
        overrides = getattr(self.notion, "find_overrides", {})
        results = []
        for mention in inputs.mentions or []:
            if mention in overrides:
                results.append(overrides[mention])
                continue
            key = mention.casefold().strip()
            tiers = [[p for p in people if p["telegram_user_id"] is not None], [p for p in people if p["telegram_user_id"] is None]]
            if not inputs.prefer_telegram:
                tiers = [people]
            hits = []
            for tier in tiers:
                hits = [p for p in tier if key in {p["name"].casefold(), p["name"].casefold().split(" ")[0],
                                                   *(a.casefold() for a in p["aliases"])}]
                if hits:
                    break
            one = hits[0] if len(hits) == 1 else None
            results.append({"match": one, "method": "fake", "score": 1.0 if one else 0.0,
                            "needs_review": len(hits) > 1, "reason": None, "candidates": []})
        return self._done("person find", {"results": results})

    def studio_notices(self, inputs):
        d = self.notion.description
        items = d if isinstance(d, list) else [{"plain_text": d}]
        runs = [{"text": i["plain_text"], "bold": bool((i.get("annotations") or {}).get("bold"))} for i in items]
        return self._done("studio notices", {"description": "".join(r["text"] for r in runs), "runs": runs})

    def meeting_notes_list(self, inputs):
        notes = []
        for page in self.notion.meetings:
            p = page["properties"]
            name = _text(p["Name"])
            sent = (p["Sent at"].get("date") or {}).get("start")
            ready = bool(p["Ready to send"].get("checkbox"))
            if page.get("in_trash"):
                continue
            if inputs.title is not None and name != inputs.title:
                continue
            if inputs.title_prefix is not None and not name.startswith(inputs.title_prefix):
                continue
            if inputs.ready_to_send is not None and (ready and sent is None) != inputs.ready_to_send:
                continue
            people = p["Team Attendees"]["people"]
            day = (p["Meeting Date"].get("date") or {}).get("start")
            if inputs.before is not None and not (day and day[:10] < inputs.before):
                continue
            notes.append({
                "page_id": page["id"], "name": name, "last_edited": page.get("last_edited_time"),
                "meeting_date": (p["Meeting Date"].get("date") or {}).get("start"), "sent_at": sent,
                "ready_to_send": ready, "attendee_ids": [u["id"] for u in people],
                "attendee_names": [u["name"] for u in people],
                "body": _body(self.notion.page_blocks.get(page["id"], [])) if inputs.include_body else [],
            })
        if inputs.latest:
            notes = sorted((n for n in notes if n["meeting_date"]), key=lambda n: n["meeting_date"])[-1:]
        return self._done("meeting-notes list", {"meeting_notes": notes})

    def _write(self, verb, inputs, do):
        self.writes.append((verb, inputs))
        if verb in self.fail:
            return self.fail[verb]
        return Done(result={"meeting_note": do()})

    def _page(self, page_id):
        return next(p for p in self.notion.meetings if p["id"] == page_id)

    def meeting_notes_create(self, inputs):
        from fake_notion import meeting_page, people

        def do():
            page_id = f"new-{sum(1 for v, _ in self.writes if v == 'create')}"
            page = meeting_page(page_id, inputs.title, inputs.meeting_date)
            page["properties"]["Team Attendees"] = people(*((i, "") for i in inputs.attendee_ids))
            self.notion.meetings.append(page)
            return {"page_id": page_id, "name": inputs.title}
        return self._write("create", inputs, do)

    def meeting_notes_append(self, inputs):
        return self._write("append", inputs, lambda: {"page_id": inputs.note})

    def meeting_notes_trash(self, inputs):
        def do():
            self._page(inputs.note)["in_trash"] = True
            return {"page_id": inputs.note}
        return self._write("trash", inputs, do)

    def meeting_notes_mark_sent(self, inputs):
        def do():
            prop = self._page(inputs.note)["properties"]["Sent at"]
            if not (prop.get("date") or {}).get("start"):  # a note already sent keeps its first date
                prop["date"] = {"start": inputs.at.isoformat(), "end": None}
            return {"page_id": inputs.note}
        return self._write("mark-sent", inputs, do)

    def verbs(self, *names):
        return [i for v, i in self.writes if v in names]

    def project_list(self, inputs):
        return self._done("project list", {"projects": self._projects()})

    def _projects(self):
        def ids(p, name):
            return [self._ref(r["id"]) for r in (p.get(name) or {}).get("relation", [])]
        projects = []
        for page in self.notion.projects:
            if page.get("in_trash"):
                continue
            p = page["properties"]
            def date_value(name, key="start"):
                d = (p.get(name) or {}).get("date")
                return d[key] if d else None
            roles = {n for n in _ROLES if ids(p, _ROLE_PROPERTY[n])}
            projects.append({
                **project_rules(((p["Status"]["select"] or {}).get("name")), roles, (p["Project Health"]["select"] or {}).get("name")),
                "page_id": page["id"], "t6_id": self._ref(page["id"]) if self._ref(page["id"]) != page["id"] else None,
                "name": _text(p["Name"]),
                "project_code": (p["Project Code"]["select"] or {}).get("name"),
                "notion_project_code": (p["Project Code"]["select"] or {}).get("name"),
                "stage": (p["Stage"]["select"] or {}).get("name"), "status": (p["Status"]["select"] or {}).get("name"),
                "project_health": (p["Project Health"]["select"] or {}).get("name"),
                "pm_notes": _text(p["PM Notes"]) or None, "playdate": date_value("Playdate"),
                "submission_deadline": date_value("Submission Deadline"),
                "archival_deadline": date_value("Archival Deadline"),
                "playdate_end": date_value("Playdate", "end"),
                "submission_deadline_end": date_value("Submission Deadline", "end"),
                "archival_deadline_end": date_value("Archival Deadline", "end"),
                "post_coordinator_ids": ids(p, "T6 Post Coordinator"), "editor_ids": ids(p, "Editors"),
                "online_editor_ids": ids(p, "Online Editor"), "colorist_ids": ids(p, "Colorist"),
                "sound_tech_ids": ids(p, "Sound Tech"), "assistant_editor_ids": ids(p, "Assistant Editor"),
                "director_ids": ids(p, "Director"), "cinematographer_ids": ids(p, "Cinematographer"),
                "prod_coor_supe_ids": ids(p, "Prod Coor/Supe"),
                "last_edited": page.get("last_edited_time"),
            })
        return projects

    def project_get(self, inputs):
        found = [p for p in self._projects() if p["page_id"] == inputs.project]
        self.calls.append("project get")
        if self.down:
            return Unavailable("connection refused")
        return Done(result={"project": found[0]}) if found else Refused(rule="project", reason="Project not found")

    def project_flags(self, inputs):
        """``project flags`` for the ``projects`` of the input (one call, the Projects read once): one entry each, in order,
        ``found`` false for an unknown Project. The health flag is raised for a blank Project Health."""
        self.flag_calls = getattr(self, "flag_calls", []) + [inputs]
        self.calls.append("project flags")
        if self.down:
            return Unavailable("connection refused")
        known = {p["page_id"]: p for p in self._projects()}
        entries = [{"project": item.project, "found": False} if item.project not in known else
                   {"project": item.project, "found": True, **self._flags_of(known[item.project], item.stated, inputs)}
                   for item in inputs.projects]
        return Done(result={"projects": entries})

    def _flags_of(self, project, stated_in, inputs):
        stated = {n: d for n, d in stated_in.items() if project[n] is None}
        dates = {n: _day(project[n]) for n in ("playdate", "submission_deadline", "archival_deadline")}
        dates.update({n: d for n, d in stated.items() if d is not None})
        today, key = inputs.as_of or date(2026, 11, 14), project["status_key"]
        overdue, other = [], []

        def check(name, blank, late):
            when = dates[name]
            if when is None:
                if blank and name not in stated:
                    other.append({"kind": "data_gap", "field": name, "detail": "missing"})
            elif late and when < today:
                overdue.append({"kind": "overdue", "field": name, "detail": "overdue", "days": (today - when).days})

        if key in _IN_PROGRESS:
            for name in ("playdate", "submission_deadline"):
                check(name, True, key != "airing")
        elif key == "archiving":
            check("archival_deadline", True, True)
        elif key in _INCOMING:
            check("playdate", False, True)
            if dates["playdate"] is not None and 0 <= (dates["playdate"] - today).days < inputs.lead_warning_days:
                other.append({"kind": "upcoming", "field": "playdate", "detail": "upcoming", "days": (dates["playdate"] - today).days})
        if project["health_missing"]:
            other.append({"kind": "data_gap", "field": "health", "detail": "missing health update"})
        sanity = []
        for kind, first, second in (("submission_before_playdate", "submission_deadline", "playdate"),
                                    ("archival_before_submission", "archival_deadline", "submission_deadline")):
            if dates[first] and dates[second] and dates[first] < dates[second]:
                sanity.append({"kind": kind, "first": first, "second": second, "first_date": dates[first].isoformat(),
                               "second_date": dates[second].isoformat(),
                               "from_notes": stated.get(first) is not None or stated.get(second) is not None})
        return {"flags": overdue + other, "missing_roles": project["missing_roles"], "sanity": sanity}

    def concern_observe(self, inputs):
        self.calls.append("concern observe")
        if self.down:
            return Unavailable("connection refused")
        if "concern observe" in self.fail:
            return self.fail["concern observe"]
        return self.concerns.observe(inputs, self._ref)

    def concern_scan(self, inputs):
        """Records the call; ``fail["concern scan"]`` is an outcome (or a function of the inputs) returned instead."""
        self.calls.append("concern scan")
        self.concerns.scans.append(inputs)
        if self.down:
            return Unavailable("connection refused")
        fail = self.fail.get("concern scan")
        if fail is not None:
            return fail(inputs) if callable(fail) else fail
        return Done(result={"project": inputs.project})

    def concern_record_message(self, inputs):
        """Two steps, as the verb: without ``confirm`` a preview with a token, with it the record. ``fail["concern
        record-message"]`` is an outcome (or a function of the inputs) returned instead."""
        self.calls.append("concern record-message")
        if self.down:
            return Unavailable("connection refused")
        fail = self.fail.get("concern record-message")
        if fail is not None:
            return fail(inputs) if callable(fail) else fail
        if inputs.confirm is None:
            return Preview(plan={"concern": inputs.concern}, token=f"tok-{inputs.idempotency_key}")
        if inputs.confirm != f"tok-{inputs.idempotency_key}":
            return Refused(rule="confirm", reason="token does not match")
        row = (inputs.concern, inputs.chat, inputs.message_id, inputs.brief_date, inputs.idempotency_key)
        if row not in self.concerns.recorded:
            self.concerns.recorded.append(row)
        return Done(result={"concern": inputs.concern})

    def concern_list(self, inputs):
        self.calls.append("concern list")
        if self.down:
            return Unavailable("connection refused")
        if "concern list" in self.fail:
            return self.fail["concern list"]
        if inputs.raise_at_psr:  # every open, unsnoozed Concern, oldest evidence first; no text, no label
            return Done(result={"concerns": [
                {"id": i["id"], "project_id": self._ref(i["project"]), "owner_id": i["owner"], "waiting_on": i.get("waiting_on"),
                 "age_seconds": 0, "evidence_at": i.get("evidence_at", 0.0)}
                for i in sorted(self.concerns.items, key=lambda i: (i.get("evidence_at", 0.0), i["id"])) if i["state"] == "open"]})
        return Done(result={"concerns": [
            {"id": i["id"], "project_id": self._ref(i["project"]), "owner_id": i["owner"], "waiting_on": i.get("waiting_on"),
             "text": i["text"], "source": i["source"], "state": i["state"], "age_seconds": 0, "chronic": False}
            for i in sorted(self.concerns.items, key=lambda i: (i["opened_on"], i["id"])) if i["state"] != "closed"]})

    def jo_list(self, inputs):
        orders = []
        for page in self.notion.job_orders:
            if page.get("in_trash"):
                continue
            p = page["properties"]
            def opt(name):
                prop = p[name]
                return (prop[prop["type"]] or {}).get("name")
            def day(name):
                d = (p[name].get("date") or {})
                return d.get("start")
            projects = [self._ref(r["id"]) for r in p["Projects"]["relation"]]
            status_name, health = opt("Status"), p["Health"]["formula"]["string"]
            orders.append({
                "active": status_name not in ("Done", "Cancelled"), "done": status_name == "Done",
                "health_name": health_name(health),
                "status_group": {"Needs Intake Review": "not_started", "Pending": "not_started", "On Hold": "not_started",
                                 "In progress": "started",
                                 "Needs Completion Review": "started", "Done": "started", "Cancelled": "cancelled"}.get(status_name),
                "page_id": page["id"], "name": _text(p["Name"]), "number": _text(p["JO Number"]) or None,
                "task": _text(p["Task"]) or None, "status": opt("Status"), "department": opt("Department"),
                "blocker": _text(p["Blocker "]) or None, "health": p["Health"]["formula"]["string"],
                "date": day("Date"), "deadline": day("Deadline"),
                "assigned_ids": [self._ref(r["id"]) for r in p["Assigned"]["relation"]],
                "project_id": projects[0] if projects else None, "project_ids": projects, "last_edited": page.get("last_edited_time"),
            })
        return self._done("jo list", {"job_orders": orders})


def refused(rule="notion"):
    return Refused(rule=rule, reason="no")


def down():
    return Unavailable("connection refused")


def rules_of(states):
    """The rule results (as ``ProjectRules``) for snapshot states, by page id: what ``project list`` would say."""
    from t6_notion_reports.t6_api import ATTRIBUTE_OF, _project_rules
    return {p: _project_rules(project_rules(s.get("Status"), {ATTRIBUTE_OF[r] for r in ROLES if s.get(r)}, s.get("Project Health")))
            for p, s in states.items()}
