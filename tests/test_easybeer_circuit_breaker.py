"""Tests for the circuit breaker in common/easybeer/_client.py."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import common.easybeer._client as client
from common.easybeer._client import (
    EasyBeerError,
    _cb_check,
    _cb_on_failure,
    _cb_on_success,
    _check_response,
    circuit_breaker_state,
)


@pytest.fixture(autouse=True)
def _reset_cb_state():
    """Reset circuit breaker between tests."""
    client._cb_failures = 0
    client._cb_open_until = 0.0
    yield
    client._cb_failures = 0
    client._cb_open_until = 0.0


def _fake_response(status_code: int, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        ok=200 <= status_code < 400,
        status_code=status_code,
        text=text,
        headers={"content-type": "application/json"},
    )


class TestCircuitBreakerState:
    def test_initial_state_closed(self):
        st = circuit_breaker_state()
        assert st["failures"] == 0
        assert st["remaining"] == 0.0

    def test_cb_check_passes_when_closed(self):
        # Should not raise
        _cb_check()

    def test_single_failure_does_not_open(self):
        _cb_on_failure()
        assert circuit_breaker_state()["failures"] == 1
        _cb_check()  # still closed


class TestCircuitBreakerOpening:
    def test_opens_after_threshold_failures(self, monkeypatch):
        # Force low threshold for test speed
        monkeypatch.setattr(client, "_CB_FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(client, "_CB_OPEN_DURATION", 60.0)
        for _ in range(3):
            _cb_on_failure()
        # Now circuit should be open
        with pytest.raises(EasyBeerError, match="circuit-breaker ouvert"):
            _cb_check()
        st = circuit_breaker_state()
        assert st["remaining"] > 0
        # Counter was reset after opening
        assert st["failures"] == 0

    def test_success_resets_counter(self):
        _cb_on_failure()
        _cb_on_failure()
        assert circuit_breaker_state()["failures"] == 2
        _cb_on_success()
        assert circuit_breaker_state()["failures"] == 0

    def test_check_response_500_triggers_failure(self, monkeypatch):
        monkeypatch.setattr(client, "_CB_FAILURE_THRESHOLD", 2)
        r1 = _fake_response(500, "internal error")
        with pytest.raises(EasyBeerError):
            _check_response(r1, "test/endpoint")
        assert circuit_breaker_state()["failures"] == 1

        r2 = _fake_response(502, "bad gateway")
        with pytest.raises(EasyBeerError):
            _check_response(r2, "test/endpoint")
        # After 2nd 5xx, circuit opens
        with pytest.raises(EasyBeerError, match="circuit-breaker ouvert"):
            _cb_check()

    def test_check_response_ok_resets_counter(self, monkeypatch):
        monkeypatch.setattr(client, "_CB_FAILURE_THRESHOLD", 5)
        r_500 = _fake_response(500, "err")
        with pytest.raises(EasyBeerError):
            _check_response(r_500, "ep")
        with pytest.raises(EasyBeerError):
            _check_response(r_500, "ep")
        assert circuit_breaker_state()["failures"] == 2

        r_ok = _fake_response(200, "")
        _check_response(r_ok, "ep")  # should not raise
        assert circuit_breaker_state()["failures"] == 0

    def test_check_response_4xx_not_counted(self, monkeypatch):
        """4xx (hors rate-limit) = erreur client, ne doit pas ouvrir le circuit."""
        monkeypatch.setattr(client, "_CB_FAILURE_THRESHOLD", 2)
        r_404 = _fake_response(404, "not found")
        with pytest.raises(EasyBeerError):
            _check_response(r_404, "ep")
        with pytest.raises(EasyBeerError):
            _check_response(r_404, "ep")
        # Not counted as server failures
        assert circuit_breaker_state()["failures"] == 0
        _cb_check()  # still closed


class TestCircuitBreakerRecovery:
    def test_circuit_closes_after_duration(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(client, "_CB_FAILURE_THRESHOLD", 1)
        monkeypatch.setattr(client, "_CB_OPEN_DURATION", 0.01)  # 10ms
        _cb_on_failure()
        with pytest.raises(EasyBeerError):
            _cb_check()
        _time.sleep(0.02)
        _cb_check()  # should pass after cooldown
