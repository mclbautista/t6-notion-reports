import json
from datetime import datetime, timezone

from fake_notion import FakeNotion, contact, project
from t6_notion_reports import cli
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
