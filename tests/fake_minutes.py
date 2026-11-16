"""A fake t6 API client for the Minutes writes: ``FakeApi`` reads plus meeting-notes create, append, trash, mark-sent."""
from __future__ import annotations

from t6.client import Unavailable
from t6.verbs import Done, Refused

from fake_api import FakeApi
from fake_notion import MEETING, date, meeting_page, people


class MinutesApi(FakeApi):
    """Writes land in the wrapped FakeNotion's meetings, so a later read sees them.

    ``fail`` maps a verb ("create", "append", "trash", "mark-sent") to an outcome returned instead of doing it.
    """

    def __init__(self, notion, fail=None, **kw):
        super().__init__(notion, **kw)
        self.fail = dict(fail or {})
        self.writes: list[tuple[str, object]] = []  # (verb, the input model), in call order

    def meeting_notes_list(self, inputs):
        out = super().meeting_notes_list(inputs)
        notes = out.result["meeting_notes"]
        if inputs.before is not None:
            notes = [n for n in notes if n["meeting_date"] and n["meeting_date"][:10] < inputs.before]
        if inputs.latest:
            notes = [max((n for n in notes if n["meeting_date"]), key=lambda n: n["meeting_date"], default=None)]
            notes = [n for n in notes if n]
        if not inputs.include_body:
            notes = [{**n, "body": []} for n in notes]
        return Done(result={"meeting_notes": notes})

    def _write(self, verb, inputs, do):
        self.writes.append((verb, inputs))
        if verb in self.fail:
            return self.fail[verb]
        return Done(result={"meeting_note": do()})

    def _page(self, page_id):
        return next(p for p in self.notion.meetings if p["id"] == page_id)

    def meeting_notes_create(self, inputs):
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


def refused(rule="notion"):
    return Refused(rule=rule, reason="no")


def down():
    return Unavailable("connection refused")
