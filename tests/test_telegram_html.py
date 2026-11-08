import pytest

from html_check import assert_valid_html
from t6_notion_reports.telegram_html import (
    LIMIT, bold, code, line, link, mention_handle, mention_user, quote, render_messages,
)


def test_escapes_user_text():
    nasty = 'A <b>&</b> "quoted" & more'
    [msg] = render_messages([line(nasty), quote(line(nasty))])
    assert "<b>&</b>" not in msg
    assert msg.count("&lt;b&gt;&amp;&lt;/b&gt; &quot;quoted&quot; &amp; more") == 2
    assert_valid_html(msg)


def test_spans_links_mentions_and_expandable_quote():
    [msg] = render_messages([
        line(bold("QAZ · Show", href="https://www.notion.so/abc"), " · ", code("Nov 5")),
        line(mention_user("Oto", 42), " ", mention_handle("@heyjunebug")),
        quote(line("a"), line("b")),
    ])
    assert msg == (
        '<a href="https://www.notion.so/abc"><b>QAZ · Show</b></a> · <code>Nov 5</code>\n'
        '<a href="tg://user?id=42">Oto</a> @heyjunebug\n'
        "<blockquote expandable>a\nb</blockquote>"
    )
    assert_valid_html(msg)


def test_link_href_is_escaped():
    [msg] = render_messages([line(link("x", 'https://e.com/?a=1&b="2"'))])
    assert 'href="https://e.com/?a=1&amp;b=&quot;2&quot;"' in msg
    assert_valid_html(msg)


def test_prefix_starts_every_message():
    blocks = [line(f"bullet {i} " + "x" * 300) for i in range(30)]
    parts = render_messages(blocks, prefix="🧪 TEST")
    assert len(parts) > 1
    assert all(p.startswith("🧪 TEST\n") and len(p) <= LIMIT for p in parts)


def test_splits_on_line_boundaries_within_limit():
    blocks = [line(f"• item {i:03d} " + "y" * 200) for i in range(100)]
    parts = render_messages(blocks)
    assert len(parts) > 1
    assert all(len(p) <= LIMIT for p in parts)
    joined = "\n".join(parts)
    assert [ln for ln in joined.split("\n")] == [f"• item {i:03d} " + "y" * 200 for i in range(100)]


def test_quote_spanning_a_split_is_closed_and_reopened():
    blocks = [line("<b>head</b>"), quote(*[line(f"n{i} " + "z" * 180) for i in range(60)]), line("after")]
    parts = render_messages(blocks)
    assert len(parts) > 2
    for p in parts:
        assert len(p) <= LIMIT
        assert_valid_html(p)
    assert all(p.count("<blockquote expandable>") == p.count("</blockquote>") for p in parts)
    assert parts[0].startswith("&lt;b&gt;head")
    assert parts[1].startswith("<blockquote expandable>")
    assert parts[-1].endswith("after")


def test_one_overlong_line_is_split_on_words_and_stays_valid():
    words = " ".join(f"w{i}&<" for i in range(2000))
    parts = render_messages([quote(line(words))])
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= LIMIT
        assert_valid_html(p)
    text = " ".join(p.replace("<blockquote expandable>", "").replace("</blockquote>", "") for p in parts)
    assert text.replace("&amp;", "&").replace("&lt;", "<").split() == words.split()


def test_unbreakable_word_is_chopped_without_splitting_an_entity():
    parts = render_messages([line("&" * 10000)])
    for p in parts:
        assert len(p) <= LIMIT
        assert_valid_html(p)
    assert "".join(parts).count("&amp;") == 10000


def test_blank_lines_never_start_or_end_a_part():
    parts = render_messages([line("a" * 4000), line(), line("b" * 4000), line()])
    assert len(parts) == 2 and not any(p.startswith("\n") or p.endswith("\n") for p in parts)


def test_prefix_too_long_is_rejected():
    with pytest.raises(ValueError):
        render_messages([line("x")], prefix="p" * 4000)
