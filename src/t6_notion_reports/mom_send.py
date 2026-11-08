"""Send the approved minutes to the PSR group (spec sections 8.5 and 9).

``run_send_ready_mom`` finds Meeting Notes pages with ``Ready to send`` ticked and ``Sent at`` empty,
reads each page as it is now (so the minutes owner's edits are what goes out), converts it to Telegram HTML
with the briefing renderer, and posts one message per group through the gateway. ``Sent at`` is set
only after every part was accepted, so a failure is retried by the next check; the idempotency keys
(``psr-mom-<date>-<part n>``) stop the gateway from sending an accepted part twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .briefing import BANNER
from .gateway import GatewayError
from .mom_job import TITLE_PREFIX
from .schedule import MANILA
from .telegram_html import Block, Line, Span, Quote, render_messages

UNCHANGED_PREFIX = "unchanged"  # the "Unchanged (Notion only, not sent)" heading and all after it stay in Notion
LIST_KINDS = {"bulleted_list_item", "numbered_list_item", "to_do"}
HEADING_KINDS = {"heading_1", "heading_2", "heading_3"}
INDENT = "  "  # Telegram trims plain leading spaces


class MomSendFailed(Exception):
    """At least one page could not be sent. The others were still tried; the next check retries."""


@dataclass(frozen=True)
class SendResult:
    candidates: int = 0
    sent_pages: int = 0
    failed_pages: int = 0
    messages: int = 0


def run_send_ready_mom(
    notion, meeting_notes_ds: str, gateway, chat_id: int, topic_id: int | None, now: datetime,
    banner: bool = False, dry_print: bool = False,
    out: Callable[[str], None] = print, log: Callable[[str], None] = print,
) -> SendResult:
    pages = [p for p in notion.query_data_source(meeting_notes_ds, filter=_FILTER) if _is_due(p)]
    sent_pages = failed = total = 0
    for page in pages:
        meeting_day = _meeting_day(page)
        if meeting_day is None:
            failed += 1
            log(f"send-ready-mom skipped page={page['id']} reason=no Meeting Date")
            continue
        try:
            messages = build_messages(notion, page["id"], banner)
            if not messages:
                failed += 1
                log(f"send-ready-mom skipped page={page['id']} reason=nothing to send")
                continue
            keyed = [(f"psr-mom-{meeting_day.isoformat()}-{n}", text) for n, text in enumerate(messages, 1)]
            if dry_print or gateway is None:
                for key, text in keyed:
                    out(f"--- to={chat_id} topic={topic_id} key={key} ---\n{text}\n")
                total += len(keyed)
                continue
            for key, text in keyed:  # in order; the first failure stops this page, the next check retries it
                gateway.notify(chat_id, text, key, topic_id)
            notion.update_page(page["id"], {"properties": {
                "Sent at": {"date": {"start": now.astimezone(MANILA).isoformat(timespec="seconds")}}}})
            sent_pages += 1
            total += len(keyed)
        except GatewayError as exc:
            failed += 1
            log(f"send-ready-mom send failed page={page['id']} error={exc}")
        except Exception as exc:  # type only: messages may hold page text
            failed += 1
            log(f"send-ready-mom failed page={page['id']} error={type(exc).__name__}")
    if pages:
        log(f"send-ready-mom pages={len(pages)} sent={sent_pages} failed={failed} messages={total} "
            f"dry_print={dry_print}")
    if failed:
        raise MomSendFailed(f"{failed} of {len(pages)} minutes pages were not sent")
    return SendResult(len(pages), sent_pages, failed, total)


_FILTER = {"and": [
    {"property": "Ready to send", "checkbox": {"equals": True}},
    {"property": "Sent at", "date": {"is_empty": True}},
]}


def _is_due(page: dict) -> bool:
    props = page["properties"]
    title = "".join(t.get("plain_text", "") for t in (props.get("Name") or {}).get("title") or [])
    return (not page.get("in_trash") and title.startswith(TITLE_PREFIX)
            and bool((props.get("Ready to send") or {}).get("checkbox"))
            and not ((props.get("Sent at") or {}).get("date") or {}).get("start"))


def _meeting_day(page: dict) -> date | None:
    start = ((page["properties"].get("Meeting Date") or {}).get("date") or {}).get("start")
    return date.fromisoformat(start[:10]) if start else None


# ---- Notion page → Telegram HTML -----------------------------------------------------------

def build_messages(notion, page_id: str, banner: bool = False) -> list[str]:
    """One message per group (the header rides with the first), each split to fit Telegram's limit."""
    messages: list[str] = []
    for blocks in _groups(notion, page_id):
        messages += render_messages(blocks, prefix=BANNER if banner else "")
    return messages


def _groups(notion, page_id: str) -> list[list[Block]]:
    header: list[list[dict]] = [[]]
    groups: list[list[dict]] = []
    for blk in notion.list_block_children(page_id):
        if blk["type"] in HEADING_KINDS and _text(blk).strip().lower().startswith(UNCHANGED_PREFIX):
            break  # whatever the heading level, the Unchanged list and all after it stay in Notion
        if blk["type"] == "heading_2":
            groups.append([blk])
        else:
            (groups[-1] if groups else header[0]).append(blk)
    rendered = [_render_group(notion, g) for g in groups]
    rendered = [g for g in rendered if len(g) > 1 or (g and not isinstance(g[0], Line))]
    head = _render_group(notion, header[0])
    if not rendered:
        return [head] if head else []
    rendered[0] = head + rendered[0]
    return rendered


def _render_group(notion, blocks: list[dict]) -> list[Block]:
    """Headings and paragraphs are plain lines; runs of list items share one expandable quote."""
    out: list[Block] = []
    quoted: list[Line] = []

    def flush() -> None:
        nonlocal quoted
        if quoted:
            out.append(Quote(tuple(quoted)))
        quoted = []

    for blk in blocks:
        kind = blk["type"]
        if kind == "toggle":
            continue  # the raw-source toggles stay in Notion
        if kind in LIST_KINDS:
            quoted += _list_lines(notion, blk, 0)
            continue
        flush()
        spans = _spans(blk)
        if not spans:
            continue
        if kind in HEADING_KINDS:
            spans = [Span(s.text, True, s.code, s.href) for s in spans]
        out.append(Line(tuple(spans)))
        if blk.get("has_children"):
            out += _render_group(notion, notion.list_block_children(blk["id"]))
    flush()
    return out


def _list_lines(notion, blk: dict, depth: int) -> list[Line]:
    spans = _spans(blk)
    if blk["type"] == "to_do":
        mark = "☑ " if blk["to_do"].get("checked") else "☐ "
    else:
        mark = "• "
    lead = INDENT * depth + mark
    lines = [Line((Span(lead), *spans))] if spans else []
    if blk.get("has_children"):
        for child in notion.list_block_children(blk["id"]):
            if child["type"] == "toggle":
                continue
            if child["type"] in LIST_KINDS:
                lines += _list_lines(notion, child, depth + 1)
            else:
                spans_c = _spans(child)
                if spans_c:
                    lines.append(Line((Span(INDENT * (depth + 1)), *spans_c)))
    return lines


def _spans(blk: dict) -> list[Span]:
    body = blk.get(blk["type"]) or {}
    spans = []
    for item in body.get("rich_text") or []:
        marks = item.get("annotations") or {}
        text = item.get("plain_text", "")
        if text:
            spans.append(Span(text, bool(marks.get("bold")), bool(marks.get("code")), item.get("href")))
    return spans


def _text(blk: dict) -> str:
    return "".join(s.text for s in _spans(blk))
