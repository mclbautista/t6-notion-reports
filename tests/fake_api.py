"""A fake t6 API client that serves the same data as a FakeNotion, in the shape the verbs return."""
from __future__ import annotations

from t6.client import Unavailable
from t6.verbs import Done


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


class FakeApi:
    """Every call returns Unavailable, like an unreachable API, while ``down`` or ``notion.down`` is true."""

    def __init__(self, notion, down=False):
        self.notion, self._down = notion, down
        self.calls: list[str] = []

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
            notes.append({
                "page_id": page["id"], "name": name, "last_edited": page.get("last_edited_time"),
                "meeting_date": (p["Meeting Date"].get("date") or {}).get("start"), "sent_at": sent,
                "ready_to_send": ready, "attendee_ids": [u["id"] for u in people],
                "attendee_names": [u["name"] for u in people],
                "body": _body(self.notion.page_blocks.get(page["id"], [])) if inputs.include_body else [],
            })
        return self._done("meeting-notes list", {"meeting_notes": notes})

    def project_list(self, inputs):
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
            projects.append({
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
        return self._done("project list", {"projects": projects})

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
            orders.append({
                "page_id": page["id"], "name": _text(p["Name"]), "number": _text(p["JO Number"]) or None,
                "task": _text(p["Task"]) or None, "status": opt("Status"), "department": opt("Department"),
                "blocker": _text(p["Blocker "]) or None, "health": p["Health"]["formula"]["string"],
                "date": day("Date"), "deadline": day("Deadline"),
                "assigned_ids": [self._ref(r["id"]) for r in p["Assigned"]["relation"]],
                "project_id": projects[0] if projects else None, "project_ids": projects, "last_edited": page.get("last_edited_time"),
            })
        return self._done("jo list", {"job_orders": orders})
