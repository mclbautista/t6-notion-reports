import json
from datetime import datetime, timezone

from fake_notion import CONTACTS, PROJECTS, FakeNotion, contact, project
from t6_notion_reports import cli
from t6_notion_reports.notion import NotionClient
from t6_notion_reports.snapshots import connect, latest_snapshot_ids, snapshot_info


class CoreStub:
    """Stands in for t6's NotionClient: records each call."""

    def __init__(self):
        self.calls = []

    def query_data_source(self, ds, *, filter=None):
        self.calls.append(("query", ds, filter))
        return [{"id": "a"}, {"id": "b"}]

    def create_page(self, body):
        self.calls.append(("create", body))
        return {"id": "pg"}

    def update_page(self, page_id, body):
        self.calls.append(("update", page_id, body))
        return {"id": page_id}

    def append_block_children(self, block_id, children):
        self.calls.append(("append", block_id, children))
        return []


def test_client_reads_through_the_core_client():
    stub = CoreStub()
    pages = NotionClient("tok", stub).query_data_source("ds1", {"property": "x"})
    assert [p["id"] for p in pages] == ["a", "b"]
    assert stub.calls == [("query", "ds1", {"property": "x"})]


def test_cli_snapshot_writes_db_and_logs_counts_only(tmp_path, capsys):
    db = tmp_path / "psr.db"
    notion = FakeNotion([project("p1", name="Secret Show")], [contact("c1", "Oto")])
    code = cli.main(["snapshot", "--kind", "monday", "--db", str(db),
                     "--projects-ds", PROJECTS, "--contacts-ds", CONTACTS],
                    notion=notion, now=datetime(2026, 11, 9, 11, tzinfo=timezone.utc))
    assert code == 0
    conn = connect(str(db))
    info = snapshot_info(conn, latest_snapshot_ids(conn, 1)[0])
    assert (info.kind, info.is_baseline) == ("monday", True)
    out = capsys.readouterr().out
    assert "baseline" in out and "Secret Show" not in out


def test_client_writes_use_data_source_parent_and_core_methods():
    stub = CoreStub()
    c = NotionClient("tok", stub)
    c.create_page("ds1", {"Name": {}}, [{"x": 1}])
    c.update_page("pg", {"in_trash": True})
    c.append_children("pg", [{"y": 2}])
    assert stub.calls[0][1]["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
    assert stub.calls[1:] == [("update", "pg", {"in_trash": True}), ("append", "pg", [{"y": 2}])]


def test_an_outage_is_no_answer_a_rate_limit_or_a_5xx_but_not_a_refusal():
    from t6.notion.errors import NotionError
    from t6_notion_reports.notion import error_name, is_outage

    assert all(is_outage(NotionError(s, "c", "m")) for s in (None, 429, 500, 503))
    assert not any(is_outage(NotionError(s, "c", "m")) for s in (400, 401, 404))
    assert is_outage(OSError("down")) and not is_outage(ValueError("x"))
    assert error_name(NotionError(None, "timeout", "secret text")) == "timeout"
