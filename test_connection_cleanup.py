import sqlite3
import pytest


def test_connection_closed_on_exception(monkeypatch):
    closed = {"value": False}

    class DummyConnection:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            closed["value"] = True
        def cursor(self):
            raise RuntimeError("boom")

    def dummy_connect(path):
        return DummyConnection()

    monkeypatch.setattr(sqlite3, "connect", dummy_connect)

    def sample_function():
        try:
            with sqlite3.connect("dummy.db") as conn:
                conn.cursor()
        except Exception:
            pass

    sample_function()
    assert closed["value"] is True
