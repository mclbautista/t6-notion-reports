"""``switch-concerns``: back up the database file, then drop the old Concern table (#119). Nothing is moved into the API."""
import sqlite3

from t6_notion_reports.cli import main
from t6_notion_reports.concern_switch import BACKUP_SUFFIX
from test_brief_job import write_config

OLD_TABLE = ("CREATE TABLE concerns (page_id TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL,"
             " first_on TEXT NOT NULL, last_on TEXT NOT NULL, resolved_on TEXT, source TEXT NOT NULL DEFAULT 'llm',"
             " PRIMARY KEY (page_id, key))")


def old_db(tmp_path):
    path = tmp_path / "psr.db"
    conn = sqlite3.connect(path)
    conn.execute(OLD_TABLE)
    conn.executemany("INSERT INTO concerns VALUES (?, ?, ?, '2026-11-01', '2026-11-08', NULL, 'flags')",
                     [("p1", "a", "A secret-looking text"), ("p1", "b", "Another text")])
    conn.execute("CREATE TABLE snapshots (snapshot_id INTEGER)")
    conn.commit()
    conn.close()
    return path


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_it_backs_up_first_then_drops_the_table_and_prints_counts_only(tmp_path, capsys):
    db = old_db(tmp_path)
    cfg = write_config(tmp_path, db)
    assert main(["switch-concerns", "--config", cfg, "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"switch-concerns backup={db}{BACKUP_SUFFIX} rows_dropped=2"
    assert "secret-looking" not in out and "p1" not in out
    assert "concerns" not in tables(db) and "snapshots" in tables(db)
    backup = tmp_path / f"psr.db{BACKUP_SUFFIX}"
    assert "concerns" in tables(backup)  # the rollback: copy it back over the database file
    conn = sqlite3.connect(backup)
    assert conn.execute("SELECT COUNT(*) FROM concerns").fetchone() == (2,)
    conn.close()


def test_it_stops_and_drops_nothing_when_a_backup_is_already_there(tmp_path, capsys):
    db = old_db(tmp_path)
    (tmp_path / f"psr.db{BACKUP_SUFFIX}").write_text("earlier backup")
    assert main(["switch-concerns", "--config", write_config(tmp_path, db), "--db", str(db)]) == 1
    assert "concerns" in tables(db)
    assert (tmp_path / f"psr.db{BACKUP_SUFFIX}").read_text() == "earlier backup"
    assert "stopped" in capsys.readouterr().err


def test_it_stops_when_there_is_no_database(tmp_path, capsys):
    assert main(["switch-concerns", "--config", write_config(tmp_path, tmp_path / "no.db"), "--db", str(tmp_path / "no.db")]) == 1
    assert not (tmp_path / f"no.db{BACKUP_SUFFIX}").exists()


def test_a_database_without_the_table_is_backed_up_and_reports_zero(tmp_path, capsys):
    db = tmp_path / "new.db"
    sqlite3.connect(db).close()
    assert main(["switch-concerns", "--config", write_config(tmp_path, db), "--db", str(db)]) == 0
    assert capsys.readouterr().out.strip().endswith("rows_dropped=0")


def test_the_brief_code_no_longer_has_a_concern_store():
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "t6_notion_reports"
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.glob("*.py") if p.name != "concern_switch.py")
    assert "update_concerns" not in text and "ensure_source_column" not in text and "CREATE TABLE IF NOT EXISTS concerns" not in text
