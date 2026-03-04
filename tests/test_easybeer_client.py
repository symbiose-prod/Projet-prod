"""
tests/test_easybeer_client.py
=============================
Comprehensive unit tests for common/easybeer/_client.py.

No network, no database — pure unit tests with monkeypatching.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import common.easybeer._client as client
from common.easybeer._client import (
    EasyBeerError,
    _auth,
    _base_payload,
    _check_response,
    _dates,
    _excel_payload,
    _indicator_payload,
    _is_retryable,
    _throttle,
    is_configured,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """Reset the global throttle timestamp before every test."""
    client._api_last_ts = 0.0
    yield
    client._api_last_ts = 0.0


@pytest.fixture()
def _clean_env(monkeypatch):
    """Remove all EasyBeer-related env vars so tests start clean."""
    for var in (
        "EASYBEER_API_USER",
        "EASYBEER_API_PASS",
        "EASYBEER_ID_BRASSERIE",
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_response(*, ok: bool, status_code: int, text: str) -> SimpleNamespace:
    """Lightweight stand-in for requests.Response."""
    return SimpleNamespace(ok=ok, status_code=status_code, text=text)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. TestIsConfigured
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsConfigured:
    """is_configured() checks both EASYBEER_API_USER and EASYBEER_API_PASS."""

    def test_both_set(self, monkeypatch):
        monkeypatch.setenv("EASYBEER_API_USER", "user")
        monkeypatch.setenv("EASYBEER_API_PASS", "pass")
        assert is_configured() is True

    def test_only_user_set(self, monkeypatch, _clean_env):
        monkeypatch.setenv("EASYBEER_API_USER", "user")
        assert is_configured() is False

    def test_only_pass_set(self, monkeypatch, _clean_env):
        monkeypatch.setenv("EASYBEER_API_PASS", "pass")
        assert is_configured() is False

    def test_neither_set(self, monkeypatch, _clean_env):
        assert is_configured() is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TestThrottle
# ═══════════════════════════════════════════════════════════════════════════════


class TestThrottle:
    """_throttle() enforces a 200ms minimum gap between API calls."""

    def test_first_call_no_sleep(self, monkeypatch):
        """First call after reset (_api_last_ts=0) should never sleep."""
        calls = []
        monkeypatch.setattr(client._time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(client._time, "sleep", lambda d: calls.append(d))

        _throttle()

        assert calls == [], "First call should not trigger sleep"

    def test_rapid_second_call_sleeps(self, monkeypatch):
        """Two calls within 200ms should trigger a sleep on the second one."""
        clock = iter([100.0, 100.0, 100.05, 100.05])
        sleep_durations = []

        monkeypatch.setattr(client._time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(
            client._time, "sleep", lambda d: sleep_durations.append(d)
        )

        _throttle()  # first call — sets _api_last_ts = 100.0
        _throttle()  # second call — now=100.05, wait = 0.2 - 0.05 = 0.15

        assert len(sleep_durations) == 1
        assert sleep_durations[0] == pytest.approx(0.15, abs=0.01)

    def test_after_sufficient_wait_no_sleep(self, monkeypatch):
        """If enough time has passed, no sleep needed."""
        clock = iter([100.0, 100.0, 100.5, 100.5])
        sleep_durations = []

        monkeypatch.setattr(client._time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(
            client._time, "sleep", lambda d: sleep_durations.append(d)
        )

        _throttle()  # first call
        _throttle()  # now=100.5, elapsed=0.5 > 0.2 → no sleep

        assert sleep_durations == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TestAuth
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuth:
    """_auth() returns (user, pass) from environment and calls _throttle."""

    def test_returns_env_vars(self, monkeypatch):
        monkeypatch.setenv("EASYBEER_API_USER", "alice")
        monkeypatch.setenv("EASYBEER_API_PASS", "s3cret")
        # Bypass throttle delay
        monkeypatch.setattr(client._time, "monotonic", lambda: 9999.0)
        monkeypatch.setattr(client._time, "sleep", lambda d: None)

        assert _auth() == ("alice", "s3cret")

    def test_returns_empty_when_not_set(self, monkeypatch, _clean_env):
        monkeypatch.setattr(client._time, "monotonic", lambda: 9999.0)
        monkeypatch.setattr(client._time, "sleep", lambda d: None)

        assert _auth() == ("", "")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. TestCheckResponse
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckResponse:
    """_check_response() raises EasyBeerError on non-OK responses."""

    def test_ok_response_no_exception(self):
        r = _fake_response(ok=True, status_code=200, text="")
        _check_response(r, "/test")  # should not raise

    def test_html_error_page_mentions_html(self):
        r = _fake_response(
            ok=False,
            status_code=500,
            text="<!DOCTYPE html><html><body>Service unavailable</body></html>",
        )
        with pytest.raises(EasyBeerError, match="HTML"):
            _check_response(r, "/indicateur/test")

    def test_html_error_page_lowercase(self):
        r = _fake_response(
            ok=False,
            status_code=502,
            text="<html><body>Bad Gateway</body></html>",
        )
        with pytest.raises(EasyBeerError, match="HTML"):
            _check_response(r, "/gateway")

    def test_json_error_includes_body(self):
        body = '{"error": "Bad Request", "message": "missing field idBrasserie"}'
        r = _fake_response(ok=False, status_code=400, text=body)
        with pytest.raises(EasyBeerError, match="missing field"):
            _check_response(r, "/brassin")

    def test_404_includes_status_code(self):
        r = _fake_response(ok=False, status_code=404, text="Not Found")
        with pytest.raises(EasyBeerError, match="404"):
            _check_response(r, "/produit/999")

    def test_error_includes_endpoint(self):
        r = _fake_response(ok=False, status_code=503, text="Unavailable")
        with pytest.raises(EasyBeerError, match="/my-endpoint"):
            _check_response(r, "/my-endpoint")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. TestDates
# ═══════════════════════════════════════════════════════════════════════════════


class TestDates:
    """_dates() returns ISO-formatted date strings for a sliding window."""

    @pytest.fixture(autouse=True)
    def _freeze_now(self):
        """Pin datetime.now to a known instant for deterministic tests."""
        frozen = datetime.datetime(2026, 3, 4, 12, 0, 0, tzinfo=datetime.UTC)
        with patch.object(datetime, "datetime", wraps=datetime.datetime) as mock_dt:
            mock_dt.now.return_value = frozen
            yield frozen

    def test_iso_format(self):
        debut, fin = _dates(30)
        assert debut.endswith("T00:00:00.000Z")
        assert fin.endswith("T23:59:59.999Z")

    def test_window_zero_days(self, _freeze_now):
        debut, fin = _dates(0)
        # Both should reference the same date (2026-03-04)
        assert debut.startswith("2026-03-04")
        assert fin.startswith("2026-03-04")

    def test_window_60_days(self, _freeze_now):
        debut, fin = _dates(60)
        # fin = 2026-03-04, debut = 2026-01-03
        assert debut.startswith("2026-01-03")
        assert fin.startswith("2026-03-04")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. TestBasePayload
# ═══════════════════════════════════════════════════════════════════════════════


class TestBasePayload:
    """_base_payload() builds the ModeleIndicateur JSON structure."""

    def test_default_id_brasserie(self, monkeypatch, _clean_env):
        payload = _base_payload(30)
        assert payload["idBrasserie"] == 2013

    def test_custom_id_brasserie(self, monkeypatch):
        monkeypatch.setenv("EASYBEER_ID_BRASSERIE", "9999")
        payload = _base_payload(30)
        assert payload["idBrasserie"] == 9999

    def test_contains_periode_libre(self):
        payload = _base_payload(30)
        assert payload["periode"]["type"] == "PERIODE_LIBRE"

    def test_dates_match_window(self):
        """Payload dates should come from _dates() with the same window."""
        with patch.object(client, "_dates", return_value=("DEBUT", "FIN")) as mock_d:
            payload = _base_payload(42)
            mock_d.assert_called_once_with(42)
        assert payload["periode"]["dateDebut"] == "DEBUT"
        assert payload["periode"]["dateFin"] == "FIN"

    def test_aliases_point_to_base_payload(self):
        """_excel_payload and _indicator_payload should be _base_payload."""
        assert _excel_payload is _base_payload
        assert _indicator_payload is _base_payload


# ═══════════════════════════════════════════════════════════════════════════════
# 7. TestIsRetryable
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsRetryable:
    """_is_retryable() classifies exceptions for tenacity retry logic."""

    # ── Network-level errors (always retryable) ──

    def test_connection_error_retryable(self):
        assert _is_retryable(requests.ConnectionError("reset")) is True

    def test_timeout_retryable(self):
        assert _is_retryable(requests.Timeout("timed out")) is True

    # ── HTTPError with retryable status codes ──

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_http_error_retryable_codes(self, code):
        resp = MagicMock()
        resp.status_code = code
        exc = requests.HTTPError(response=resp)
        assert _is_retryable(exc) is True

    def test_http_error_200_not_retryable(self):
        resp = MagicMock()
        resp.status_code = 200
        exc = requests.HTTPError(response=resp)
        assert _is_retryable(exc) is False

    def test_http_error_400_not_retryable(self):
        resp = MagicMock()
        resp.status_code = 400
        exc = requests.HTTPError(response=resp)
        assert _is_retryable(exc) is False

    def test_http_error_no_response_not_retryable(self):
        exc = requests.HTTPError()
        # .response defaults to None on HTTPError()
        assert _is_retryable(exc) is False

    # ── EasyBeerError containing status codes ──

    def test_easybeer_error_429_retryable(self):
        exc = EasyBeerError("EasyBeer /foo → HTTP 429 : rate limited")
        assert _is_retryable(exc) is True

    def test_easybeer_error_500_retryable(self):
        exc = EasyBeerError("EasyBeer /bar → HTTP 500 : internal")
        assert _is_retryable(exc) is True

    def test_easybeer_error_503_retryable(self):
        exc = EasyBeerError("EasyBeer /baz → HTTP 503 : unavailable")
        assert _is_retryable(exc) is True

    def test_easybeer_error_no_code_not_retryable(self):
        exc = EasyBeerError("Something went wrong")
        assert _is_retryable(exc) is False

    def test_easybeer_error_401_not_retryable(self):
        exc = EasyBeerError("EasyBeer /auth → HTTP 401 : unauthorized")
        assert _is_retryable(exc) is False

    # ── Unrelated exception types ──

    def test_value_error_not_retryable(self):
        assert _is_retryable(ValueError("bad value")) is False

    def test_runtime_error_not_retryable(self):
        assert _is_retryable(RuntimeError("generic")) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. TestRetryApiDecorator
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryApiDecorator:
    """retry_api decorator should retry on transient errors and reraise."""

    def test_retry_api_exists_and_is_callable(self):
        assert callable(client.retry_api)

    def test_decorated_function_succeeds_on_first_try(self):
        @client.retry_api
        def ok():
            return "done"

        assert ok() == "done"

    def test_decorated_function_retries_on_connection_error(self):
        call_count = 0

        @client.retry_api
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.ConnectionError("reset")
            return "recovered"

        result = flaky()
        assert result == "recovered"
        assert call_count == 3

    def test_decorated_function_reraises_after_max_attempts(self):
        @client.retry_api
        def always_fails():
            raise requests.ConnectionError("always down")

        with pytest.raises(requests.ConnectionError):
            always_fails()


# ═══════════════════════════════════════════════════════════════════════════════
# 9. TestModuleConstants
# ═══════════════════════════════════════════════════════════════════════════════


class TestModuleConstants:
    """Verify module-level constants have expected values."""

    def test_base_url(self):
        assert client.BASE == "https://api.easybeer.fr"

    def test_timeout(self):
        assert client.TIMEOUT == 30

    def test_min_interval(self):
        assert client._API_MIN_INTERVAL == 0.2
