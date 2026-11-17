import sqlite3

import pytest


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch):
    """Close every SQLite connection a test opened (Python 3.14 reports a leaked one as a warning)."""
    opened = []
    real = sqlite3.connect

    def tracked(*args, **kwargs):
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked)
    yield
    for conn in opened:
        conn.close()
