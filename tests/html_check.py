"""Validate Telegram HTML: balanced tags from the allowed set, no stray < or &."""
import re
from html.parser import HTMLParser

ALLOWED = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED, f"tag {tag} not allowed"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}> with stack {self.stack}"
        self.stack.pop()

    def handle_data(self, data):
        assert "<" not in data and ">" not in data
        assert not _BARE_AMP.search(data), f"bare & in {data!r}"


def assert_valid_html(html: str) -> None:
    checker = _Checker()
    checker.feed(html)
    checker.close()
    assert not checker.stack, f"unclosed tags {checker.stack}"
    assert not _BARE_AMP.search(re.sub(r"<[^>]*>", "", html))
