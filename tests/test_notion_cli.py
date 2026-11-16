import json
from datetime import datetime, timezone

from fake_notion import FakeNotion, contact, project
from t6_notion_reports import cli
from t6_notion_reports.notion import NotionClient
from t6_notion_reports.snapshots import connect, latest_snapshot_ids, snapshot_info


def test_cli_snapshot_writes_db_and_logs_counts_only(tmp_path, capsys):
    db = tmp_path / "psr.db"
    notion = FakeNotion([project("p1", name="Secret Show")], [contact("c1", "Oto")])
    code = cli.main(["snapshot", "--kind", "monday", "--db", str(db)],
                    notion=notion, now=datetime(2026, 11, 9, 11, tzinfo=timezone.utc))
    assert code == 0
    conn = connect(str(db))
    info = snapshot_info(conn, latest_snapshot_ids(conn, 1)[0])
    assert (info.kind, info.is_baseline) == ("monday", True)
    out = capsys.readouterr().out
    assert "baseline" in out and "Secret Show" not in out


def test_client_writes_use_data_source_parent_and_methods():
    calls = []

    def transport(url, headers, body, method="POST"):
        calls.append((method, url, body))
        return {"id": "pg"}

    c = NotionClient("tok", transport)
    c.create_page("ds1", {"Name": {}}, [{"x": 1}])
    c.update_page("pg", {"in_trash": True})
    c.append_children("pg", [{"y": 2}])
    assert calls[0][0] == "POST" and calls[0][2]["parent"] == {"type": "data_source_id", "data_source_id": "ds1"}
    assert [(m, u.rsplit("/v1", 1)[1]) for m, u, _ in calls[1:]] == [
        ("PATCH", "/pages/pg"), ("PATCH", "/blocks/pg/children")]
