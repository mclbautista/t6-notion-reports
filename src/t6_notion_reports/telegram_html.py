"""Telegram HTML renderer and splitter (spec section 9). The minutes reuse it.

Content is a list of blocks: ``Line`` (a row of spans) and ``Quote`` (an expandable blockquote of
lines). ``render_messages`` turns blocks into messages of at most ``LIMIT`` characters. It escapes
all user text and splits only between lines (bullets), closing and reopening a blockquote that
spans a split so that every part is valid HTML on its own. A single line that is itself too long
is split on word boundaries; an over-long link is split into several links to the same address.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LIMIT = 4096
_OPEN_QUOTE = "<blockquote expandable>"
_CLOSE_QUOTE = "</blockquote>"


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    code: bool = False
    href: str | None = None


@dataclass(frozen=True)
class Line:
    spans: tuple[Span, ...] = ()


@dataclass(frozen=True)
class Quote:
    lines: tuple[Line, ...]


Block = Line | Quote


def escape(text: str) -> str:
    """The only characters Telegram HTML needs escaped, plus the double quote for attributes."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def line(*parts: str | Span) -> Line:
    return Line(tuple(Span(p) if isinstance(p, str) else p for p in parts))


def bold(text: str, href: str | None = None) -> Span:
    return Span(text, bold=True, href=href)


def code(text: str) -> Span:
    return Span(text, code=True)


def link(text: str, href: str) -> Span:
    return Span(text, href=href)


def mention_user(name: str, user_id: int) -> Span:
    """A real @mention for someone without a public handle: a tg://user link."""
    return Span(name, href=f"tg://user?id={int(user_id)}")


def mention_handle(handle: str) -> Span:
    """Telegram turns a plain ``@handle`` into a mention; no markup is needed."""
    return Span("@" + handle.lstrip("@"))


def quote(*lines: Line) -> Quote:
    return Quote(tuple(lines))


def render_span(span: Span) -> str:
    html = escape(span.text)
    if span.code:
        html = f"<code>{html}</code>"
    if span.bold:
        html = f"<b>{html}</b>"
    if span.href:
        html = f'<a href="{escape(span.href)}">{html}</a>'
    return html


def render_line(item: Line) -> str:
    return "".join(render_span(s) for s in item.spans)


def render_messages(blocks: list[Block], prefix: str = "", limit: int = LIMIT) -> list[str]:
    """Render ``blocks`` into one or more messages, each at most ``limit`` characters.

    ``prefix`` (already HTML, for example a test banner) starts every message.
    """
    head = prefix + "\n" if prefix else ""
    room = limit - len(head)
    if room < 200:
        raise ValueError("prefix leaves no room for content")
    items: list[tuple[Line, bool]] = []
    for block in blocks:
        if isinstance(block, Quote):
            items += [(ln, True) for ln in block.lines]
        else:
            items.append((block, False))

    parts: list[str] = []
    body = ""
    in_quote = False

    def finish() -> None:
        nonlocal body, in_quote
        text = (body + (_CLOSE_QUOTE if in_quote else "")).rstrip("\n")
        if text.strip():
            parts.append(head + text)
        body, in_quote = "", False

    for ln, quoted in items:
        if not ln.spans and not body:
            continue  # no blank line at the top of a part
        for piece in _fit(ln, quoted, room):
            html = render_line(piece)
            while True:
                candidate = _append(body, in_quote, html, quoted)
                closing = _CLOSE_QUOTE if quoted else ""
                if len(candidate) + len(closing) <= room:
                    body, in_quote = candidate, quoted
                    break
                if not body:
                    raise ValueError("line does not fit in an empty message")
                finish()
    finish()
    return parts


def _append(body: str, in_quote: bool, html: str, quoted: bool) -> str:
    if quoted:
        if in_quote:
            return body + "\n" + html
        return body + ("\n" if body else "") + _OPEN_QUOTE + html
    if in_quote:
        return body + _CLOSE_QUOTE + "\n" + html
    return body + ("\n" if body else "") + html


def _fit(item: Line, quoted: bool, room: int) -> list[Line]:
    """Split one line into pieces that fit an otherwise empty message."""
    # Worst case overhead: opening and closing tags of a quote, plus the newline.
    budget = room - len(_OPEN_QUOTE) - len(_CLOSE_QUOTE) - 1
    if len(render_line(item)) <= budget:
        return [item]
    tokens: list[Span] = []
    for span in item.spans:
        if len(render_span(span)) <= budget:
            tokens.append(span)
            continue
        for word in re.findall(r"\S+\s*", span.text):
            tokens += _chop(Span(word, span.bold, span.code, span.href), budget)
    pieces: list[Line] = []
    current: list[Span] = []
    used = 0
    for token in tokens:
        size = len(render_span(token))
        if current and used + size > budget:
            pieces.append(Line(tuple(current)))
            current, used = [], 0
        current.append(token)
        used += size
    if current:
        pieces.append(Line(tuple(current)))
    return pieces


def _chop(span: Span, budget: int) -> list[Span]:
    """Break a single over-long word by characters, measuring escaped size."""
    if len(render_span(span)) <= budget:
        return [span]
    out: list[Span] = []
    chunk = ""
    for ch in span.text:
        trial = Span(chunk + ch, span.bold, span.code, span.href)
        if len(render_span(trial)) > budget:
            out.append(Span(chunk, span.bold, span.code, span.href))
            chunk = ch
        else:
            chunk += ch
    if chunk:
        out.append(Span(chunk, span.bold, span.code, span.href))
    return out


def split_line(item: Line, quoted: bool, room: int) -> list[Line]:
    """Split one line into pieces that each fit ``room`` characters of rendered HTML, quote tags included."""
    return _fit(item, quoted, room)
