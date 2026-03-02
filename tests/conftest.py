"""
tests/conftest.py
=================
Shared fixtures for Ferment Station test suite.
No database, no network — pure unit tests only.
"""
from __future__ import annotations

import sys
import unittest.mock as mock
from pathlib import Path

# Ensure project root is on sys.path so `from common.xxx` works
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Stub db.conn so that importing common.auth (which does
# `from db.conn import run_sql` at module level) doesn't fail.
# run_sql is only used by DB-dependent functions, not by the
# pure validators we test.
_db_mock = mock.MagicMock()
_db_mock.run_sql = mock.MagicMock(return_value=[])
sys.modules.setdefault("db", _db_mock)
sys.modules.setdefault("db.conn", _db_mock)
