from datetime import datetime, timezone

import pytest

from fake_notion import MEETING, FakeNotion, block, meeting_page, rt
from html_check import assert_valid_html
from t6_notion_reports.gateway import GatewayError
from t6_notion_reports.mom_send import MomSendFailed, run_send_ready_mom
from t6_notion_reports.telegram_html import LIMIT

NOW = datetime(2026, 11, 16, 6, 5, tzinfo=timezone.utc)  # 14:05 Manila
CHAT, TOPIC = -100123, 7


class FakeGateway:
    def __init__(self, fail_keys=()):
        self.sent, self.fail_keys = [], set(fail_keys)

    def notify(self, to, text, key, topic=None):
        if key in self.fail_keys:
            raise GatewayError("down")
        self.sent.append((to, topic, key, text))
        return len(self.sent)


def bullet(text, *children, **kw):
    return block("bulleted_list_item", text, children, **kw)


def full_page():
    return [
        block("heading_1", "PSR MOM"),
        block("paragraph", "Monday, Nov 16, 2026"),
        block("paragraph", "Attendees: Quilla, Oto"),
        block("heading_2", "🔴 Critical / off track"),
        bullet("CR", bullet("Delay in Mixing"), bullet("VBM & Co will <haggle>")),
        block("heading_2", "🟡 On track"),
        bullet("NH (Health not set) - last updated Nov 6"),
        block("heading_2", "🔵 Archiving"),
        bullet("LING - last updated Nov 6"),
        block("heading_2", "⚫️ Others"),
        bullet("Leaves and holidays: none in the next 14 days"),
        bullet("Heavy workloads"),
        block("paragraph", "@quil Oto"),
        block("heading_2", "Unchanged (Notion only, not sent)"),
        bullet("IDL - no notes yet"),
    ]


def setup(blocks=None, **page_kw):
    page_kw.setdefault("ready", True)
    n = FakeNotion(meetings=[meeting_page("m1", "PSR MOM 2026-11-16", "2026-11-16", **page_kw)])
    n.page_blocks["m1"] = blocks if blocks is not None else full_page()
    return n


def send(n, gw=None, **kw):
    gw = gw or FakeGateway()
    res = run_send_ready_mom(n, MEETING, gw, CHAT, TOPIC, NOW, log=lambda _: None, **kw)
    return gw, res


def test_ticked_page_is_sent_once_in_group_order_and_sent_at_set():
    n = setup()
    gw, res = send(n)
    assert res.sent_pages == 1
    assert [(to, topic) for to, topic, _, _ in gw.sent] == [(CHAT, TOPIC)] * 4
    assert [k for _, _, k, _ in gw.sent] == [f"psr-mom-2026-11-16-{i}" for i in (1, 2, 3, 4)]
    texts = [t for *_, t in gw.sent]
    heads = ["Critical", "On track", "Archiving", "Others"]
    assert all(h in t for h, t in zip(heads, texts))
    assert "PSR MOM" in texts[0] and "Attendees: Quilla, Oto" in texts[0]  # header first
    assert n.sent_marked == [("m1", {"Sent at": {"date": {"start": "2026-11-16T14:05:00+08:00"}}})]


def test_second_check_sends_nothing():
    n = setup()
    gw, _ = send(n)
    again, res = send(n, FakeGateway())
    assert res.sent_pages == 0 and again.sent == [] and len(gw.sent) == 4


def test_unticked_sent_and_other_pages_are_skipped():
    n = FakeNotion(meetings=[
        meeting_page("a", "PSR MOM 2026-11-09", "2026-11-09", ready=False),
        meeting_page("b", "PSR MOM 2026-11-02", "2026-11-02", ready=True, sent_at="2026-11-02"),
        meeting_page("c", "Client call", "2026-11-16", ready=True),
    ])
    for pid in "abc":
        n.page_blocks[pid] = full_page()
    gw, res = send(n)
    assert gw.sent == [] and res.sent_pages == 0


def test_edits_in_the_page_are_what_gets_sent():
    blocks = full_page()
    blocks[4] = bullet("CR", bullet("Edited by Quilla"))
    gw, _ = send(setup(blocks))
    body = gw.sent[0][3]
    assert "Edited by Quilla" in body and "Delay in Mixing" not in body


def test_unchanged_list_and_raw_toggles_never_sent():
    blocks = full_page()
    blocks[4] = bullet("CR", bullet("Delay in Mixing"), block("toggle", "Raw source", [bullet("RAW BLOCK TEXT")]))
    gw, _ = send(setup(blocks))
    joined = "\n".join(t for *_, t in gw.sent)
    assert "IDL" not in joined and "Unchanged" not in joined
    assert "RAW BLOCK TEXT" not in joined and "Raw source" not in joined and "Delay in Mixing" in joined


def test_messages_are_valid_html_escaped_with_expandable_quotes_and_mentions():
    gw, _ = send(setup())
    first = gw.sent[0][3]
    for *_, text in gw.sent:
        assert len(text) <= LIMIT
        assert_valid_html(text)
    assert "VBM &amp; Co will &lt;haggle&gt;" in first and "<blockquote expandable>" in first
    assert "@quil" in gw.sent[-1][3]


def test_rich_text_formatting_is_kept():
    blocks = full_page()
    blocks[4] = bullet("CR", bullet([rt("QC ", bold=True), rt("Nov 6", code=True), rt(" site", href="https://x.io/a?b=1&c=2")]))
    gw, _ = send(setup(blocks))
    t = gw.sent[0][3]
    assert "<b>QC </b>" in t and "<code>Nov 6</code>" in t and '<a href="https://x.io/a?b=1&amp;c=2"> site</a>' in t
    assert_valid_html(t)


def test_long_group_splits_with_balanced_tags_and_parts_are_numbered_through():
    blocks = full_page()
    blocks[4] = bullet("CR", *[bullet(f"item {i} " + "word " * 30) for i in range(60)])
    gw, _ = send(setup(blocks))
    keys = [k for _, _, k, _ in gw.sent]
    assert len(gw.sent) > 4 and keys == [f"psr-mom-2026-11-16-{i}" for i in range(1, len(keys) + 1)]
    for *_, text in gw.sent:
        assert len(text) <= LIMIT
        assert_valid_html(text)


def test_gateway_failure_leaves_sent_at_empty_and_retry_resends_same_keys():
    n = setup()
    gw = FakeGateway(fail_keys=["psr-mom-2026-11-16-3"])
    with pytest.raises(MomSendFailed):
        send(n, gw)
    assert n.sent_marked == []
    assert [k for _, _, k, _ in gw.sent] == ["psr-mom-2026-11-16-1", "psr-mom-2026-11-16-2"]
    retry, res = send(n)  # next check: the gateway dedupes parts it already accepted by key
    assert res.sent_pages == 1
    assert [k for _, _, k, _ in retry.sent] == [f"psr-mom-2026-11-16-{i}" for i in (1, 2, 3, 4)]
    assert len(n.sent_marked) == 1


def test_one_failing_page_does_not_stop_the_next():
    n = FakeNotion(meetings=[
        meeting_page("a", "PSR MOM 2026-11-09", "2026-11-09", ready=True),
        meeting_page("b", "PSR MOM 2026-11-16", "2026-11-16", ready=True),
    ])
    n.page_blocks["a"], n.page_blocks["b"] = full_page(), full_page()
    gw = FakeGateway(fail_keys=["psr-mom-2026-11-09-1"])
    with pytest.raises(MomSendFailed):
        send(n, gw)
    assert [pid for pid, _ in n.sent_marked] == ["b"]


def test_test_banner_starts_every_message():
    gw, _ = send(setup(), banner=True)
    assert all(t.startswith("🧪 TEST\n") for *_, t in gw.sent)
    assert all(len(t) <= LIMIT for *_, t in gw.sent)


def test_dry_print_sends_nothing_and_leaves_sent_at_empty():
    n = setup()
    out = []
    res = run_send_ready_mom(n, MEETING, None, CHAT, TOPIC, NOW, dry_print=True, out=out.append, log=lambda _: None)
    assert n.sent_marked == [] and res.sent_pages == 0 and any("psr-mom-2026-11-16-1" in o for o in out)


def test_logs_counts_only():
    logs = []
    run_send_ready_mom(setup(), MEETING, FakeGateway(), CHAT, TOPIC, NOW, log=logs.append)
    assert logs and "Delay in Mixing" not in "".join(logs) and "sent=1" in "".join(logs)


# ---- CLI and scheduler job ----

def cli_cfg(tmp_path, monkeypatch, notes=True, secret=True):
    import yaml
    cfg = {"projects_data_source": "11111111-1111-1111-1111-111111111111",
           "contacts_data_source": "22222222-2222-2222-2222-222222222222",
           "database_path": str(tmp_path / "psr.db"), "gateway_url": "http://gw:8780",
           "psr_group_chat_id": CHAT, "psr_group_topic_id": TOPIC,
           "cyril_contact_id": "33333333-3333-3333-3333-333333333330"}
    if notes:
        cfg["meeting_notes_data_source"] = "44444444-4444-4444-4444-444444444444"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.setenv("PSR_SECRET", "s3cret") if secret else monkeypatch.delenv("PSR_SECRET", raising=False)
    return str(path)


class Routed(FakeNotion):
    def query_data_source(self, ds, filter=None):
        assert ds == "44444444-4444-4444-4444-444444444444"
        return super().query_data_source(MEETING)


def test_cli_dry_print_renders_and_does_not_mark_sent(tmp_path, monkeypatch, capsys):
    from t6_notion_reports.cli import main
    n = setup()
    n.__class__ = Routed
    assert main(["send-ready-mom", "--config", cli_cfg(tmp_path, monkeypatch, secret=False), "--dry-print"],
                notion=n, now=NOW) == 0
    assert "psr-mom-2026-11-16-1" in capsys.readouterr().out and n.sent_marked == []


def test_cli_needs_meeting_notes_config_and_secret(tmp_path, monkeypatch, capsys):
    from t6_notion_reports.cli import main
    assert main(["send-ready-mom", "--config", cli_cfg(tmp_path, monkeypatch, notes=False)], notion=FakeNotion()) == 2
    assert "meeting_notes_data_source" in capsys.readouterr().err
    assert main(["send-ready-mom", "--config", cli_cfg(tmp_path, monkeypatch, secret=False)], notion=FakeNotion()) == 2
    assert "PSR_SECRET" in capsys.readouterr().err


def test_scheduler_job_runs_the_check_and_serve_adds_the_slot(tmp_path, monkeypatch):
    import sqlite3
    from t6_notion_reports.cli import _slots, make_job
    from t6_notion_reports.config import load_config
    from t6_notion_reports.schedule import SEND_MOM_SLOT
    cfg = load_config(cli_cfg(tmp_path, monkeypatch))
    assert SEND_MOM_SLOT in _slots(cfg)
    assert SEND_MOM_SLOT not in _slots(load_config(cli_cfg(tmp_path, monkeypatch, notes=False)))
    n, gw = setup(), FakeGateway()
    n.__class__ = Routed
    make_job(sqlite3.connect(":memory:"), n, cfg, gw, lambda: NOW)("send_mom")
    assert len(gw.sent) == 4 and gw.sent[0][:2] == (CHAT, TOPIC) and len(n.sent_marked) == 1
