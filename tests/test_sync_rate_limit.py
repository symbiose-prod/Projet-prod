"""Tests for common/sync/rate_limit.py."""
from __future__ import annotations

import time

import pytest

from common.sync import rate_limit


@pytest.fixture(autouse=True)
def _reset():
    rate_limit.reset()
    yield
    rate_limit.reset()


class TestCheck:
    def test_single_request_allowed(self):
        allowed, retry = rate_limit.check("k1")
        assert allowed is True
        assert retry == 0

    def test_within_limit_all_allowed(self):
        for _ in range(10):
            allowed, _ = rate_limit.check("k1", limit=10)
            assert allowed is True

    def test_limit_exceeded_returns_429_info(self):
        # Hit la limite exacte
        for _ in range(5):
            allowed, _ = rate_limit.check("k1", limit=5, window_seconds=60)
            assert allowed is True
        # 6ème → refus
        allowed, retry = rate_limit.check("k1", limit=5, window_seconds=60)
        assert allowed is False
        assert retry > 0
        assert retry <= 60

    def test_different_keys_isolated(self):
        # k1 à la limite
        for _ in range(3):
            rate_limit.check("k1", limit=3)
        allowed_k1, _ = rate_limit.check("k1", limit=3)
        assert allowed_k1 is False
        # k2 intact
        allowed_k2, _ = rate_limit.check("k2", limit=3)
        assert allowed_k2 is True

    def test_window_expiry_allows_again(self):
        # Saturate avec une fenêtre courte
        for _ in range(3):
            rate_limit.check("k1", limit=3, window_seconds=1)
        allowed, _ = rate_limit.check("k1", limit=3, window_seconds=1)
        assert allowed is False
        # Attend l'expiration complète
        time.sleep(1.1)
        allowed, _ = rate_limit.check("k1", limit=3, window_seconds=1)
        assert allowed is True


class TestReset:
    def test_reset_single_key(self):
        for _ in range(5):
            rate_limit.check("k1", limit=5)
        allowed, _ = rate_limit.check("k1", limit=5)
        assert allowed is False
        rate_limit.reset("k1")
        allowed, _ = rate_limit.check("k1", limit=5)
        assert allowed is True

    def test_reset_all(self):
        for _ in range(5):
            rate_limit.check("k1", limit=5)
            rate_limit.check("k2", limit=5)
        rate_limit.reset()
        assert rate_limit.state_snapshot() == {}


class TestStateSnapshot:
    def test_empty_by_default(self):
        assert rate_limit.state_snapshot() == {}

    def test_reflects_hits(self):
        rate_limit.check("k1")
        rate_limit.check("k1")
        rate_limit.check("k2")
        snap = rate_limit.state_snapshot()
        assert snap.get("k1") == 2
        assert snap.get("k2") == 1
