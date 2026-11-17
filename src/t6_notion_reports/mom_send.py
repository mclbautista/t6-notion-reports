"""Send the approved minutes to the PSR group (spec sections 8.5 and 9).

``run_send_ready_mom`` finds Meeting Notes pages with ``Ready to send`` ticked and ``Sent at`` empty,
reads each page as it is now (so the minutes owner's edits are what goes out), converts it to Telegram HTML
with the briefing renderer, and posts one message per group through the gateway. ``Sent at`` is set
only after every part was accepted, so a failure is retried by the next check; the idempotency keys
(``psr-mom-<date>-<part n>``) stop the gateway from sending an accepted part twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from .briefing import BANNER
from .gateway import GatewayError
from .minutes_api import MinutesApiError, mark_sent
from .mom_job import TITLE_PREFIX
from .schedule import MANILA
from .t6_api import Block, Note
from .telegram_html import Block as BlockOut, Line, Span, Quote, render_messages

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
    notion, gateway, chat_id: int, topic_id: int | None, now: datetime,
    banner: bool = False, dry_print: bool = False,
    out: Callable[[str], None] = print, log: Callable[[str], None] = print,
) -> SendResult:
    pages = [n for n in notion.meeting_notes(ready_to_send=True) if _is_due(n)]
    sent_pages = failed = total = 0
    for page in pages:
        meeting_day = page.meeting_day
        if meeting_day is None:
            failed += 1
            log(f"send-ready-mom skipped page={page.page_id} reason=no Meeting Date")
            continue
        try:
            messages = build_messages(page.body, banner)
            if not messages:
                failed += 1
                log(f"send-ready-mom skipped page={page.page_id} reason=nothing to send")
                continue
            keyed = [(f"psr-mom-{meeting_day.isoformat()}-{n}", text) for n, text in enumerate(messages, 1)]
            if dry_print or gateway is None:
                for key, text in keyed:
                    out(f"--- to={chat_id} topic={topic_id} key={key} ---\n{text}\n")
                total += len(keyed)
                continue
            for key, text in keyed:  # in order; the first failure stops this page, the next check retries it
                gateway.notify(chat_id, text, key, topic_id)
            # A failed mark leaves Sent at empty and counts the page as failed; the next check re-sends the
            # parts, which is safe because the gateway keys stop an accepted part from going twice.
            mark_sent(notion.client, page.page_id, now.astimezone(MANILA))
            sent_pages += 1
            total += len(keyed)
        except GatewayError as exc:
            failed += 1
            log(f"send-ready-mom send failed page={page.page_id} error={exc}")
        except MinutesApiError as exc:  # the verb and the API's rule, no page text
            failed += 1
            log(f"send-ready-mom mark failed page={page.page_id} error={exc}")
        except Exception as exc:  # type only: messages may hold page text
            failed += 1
            log(f"send-ready-mom failed page={page.page_id} error={type(exc).__name__}")
    if pages:
        log(f"send-ready-mom pages={len(pages)} sent={sent_pages} failed={failed} messages={total} "
            f"dry_print={dry_print}")
    if failed:
        raise MomSendFailed(f"{failed} of {len(pages)} minutes pages were not sent")
    return SendResult(len(pages), sent_pages, failed, total)


def _is_due(note: Note) -> bool:
    return note.title.startswith(TITLE_PREFIX) and note.ready_to_send and not note.sent_at


# ---- Meeting Notes body → Telegram HTML -----------------------------------------------------------

def build_messages(body: Sequence[Block], banner: bool = False) -> list[str]:
    """One message per group (the header rides with the first), each split to fit Telegram's limit."""
    messages: list[str] = []
    for blocks in _groups(body):
        messages += render_messages(blocks, prefix=BANNER if banner else "")
    return messages


def _groups(body: Sequence[Block]) -> list[list[BlockOut]]:
    header: list[list[Block]] = [[]]
    groups: list[list[Block]] = []
    for blk in body:
        if blk.kind in HEADING_KINDS and blk.text.strip().lower().startswith(UNCHANGED_PREFIX):
            break  # whatever the heading level, the Unchanged list and all after it stay in Notion
        if blk.kind == "heading_2":
            groups.append([blk])
        else:
            (groups[-1] if groups else header[0]).append(blk)
    rendered = [_render_group(g) for g in groups]
    rendered = [g for g in rendered if len(g) > 1 or (g and not isinstance(g[0], Line))]
    head = _render_group(header[0])
    if not rendered:
        return [head] if head else []
    rendered[0] = head + rendered[0]
    return rendered


def _render_group(blocks: Sequence[Block]) -> list[BlockOut]:
    """Headings and paragraphs are plain lines; runs of list items share one expandable quote."""
    out: list[BlockOut] = []
    quoted: list[Line] = []

    def flush() -> None:
        nonlocal quoted
        if quoted:
            out.append(Quote(tuple(quoted)))
        quoted = []

    for blk in blocks:
        if blk.kind == "toggle":
            continue  # the raw-source toggles stay in Notion
        if blk.kind in LIST_KINDS:
            quoted += _list_lines(blk, 0)
            continue
        flush()
        spans = list(blk.spans)
        if not spans:
            continue
        if blk.kind in HEADING_KINDS:
            spans = [Span(s.text, True, s.code, s.href) for s in spans]
        out.append(Line(tuple(spans)))
        if blk.children:
            out += _render_group(blk.children)
    flush()
    return out


def _list_lines(blk: Block, depth: int) -> list[Line]:
    spans = list(blk.spans)
    if blk.kind == "to_do":
        mark = "☑ " if blk.checked else "☐ "
    else:
        mark = "• "
    lead = INDENT * depth + mark
    lines = [Line((Span(lead), *spans))] if spans else []
    for child in blk.children:
        if child.kind == "toggle":
            continue
        if child.kind in LIST_KINDS:
            lines += _list_lines(child, depth + 1)
        elif child.spans:
            lines.append(Line((Span(INDENT * (depth + 1)), *child.spans)))
    return lines
