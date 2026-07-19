"""Pytest fixtures — isolate every test on a fresh temp database.

PULSE_DB is set before agent_pulse.collector.db is imported, so DB_PATH
(read at import time) points at the throwaway file.
"""
import os
import tempfile
import pathlib

_TMP = pathlib.Path(tempfile.gettempdir()) / "pulse_pytest.db"
os.environ["PULSE_DB"] = str(_TMP)

import pytest


@pytest.fixture(autouse=True)
def fresh_db():
    from agent_pulse.collector import db
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(_TMP) + suffix)
        if p.exists():
            p.unlink()
    db.init_db()
    yield
