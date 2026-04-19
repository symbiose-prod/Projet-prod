"""Tests pour common/easybeer/endpoint.execute_endpoint.

Mocke get_session (HTTP) + cache_get/cache_put (L2 DB) — zéro dépendance
réseau ou DB réelle.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from common.easybeer.endpoint import execute_endpoint


def _fake_response(*, ok: bool = True, status_code: int = 200,
                   text: str = "", json_data: dict | list | None = None):
    """Stand-in minimal pour requests.Response."""
    r = SimpleNamespace(
        ok=ok,
        status_code=status_code,
        text=text,
        headers={"content-type": "application/json"},
        elapsed=None,  # _check_response est null-safe
        request=None,
    )
    r.json = lambda: json_data if json_data is not None else {}
    return r


@dataclass(frozen=True)
class _FakeModel:
    """Modèle de test avec from_dict."""
    value: int
    label: str

    @classmethod
    def from_dict(cls, d: dict) -> _FakeModel:
        if not isinstance(d, dict):
            return cls(0, "")
        return cls(value=int(d.get("value") or 0), label=str(d.get("label") or ""))


# ─── Bascule method (GET / POST) ────────────────────────────────────────────

class TestMethodDispatch:
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_get_without_payload(self, _mock_auth, mock_session):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"ok": True})
        mock_session.return_value = sess
        result = execute_endpoint(method="GET", path="some/path")
        sess.get.assert_called_once()
        args, kwargs = sess.get.call_args
        assert args[0].endswith("/some/path")
        assert kwargs["auth"] == ("u", "p")
        # Pas de json sur un GET
        assert "json" not in kwargs
        assert result == {"ok": True}

    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_post_with_payload_and_params(self, _mock_auth, mock_session):
        sess = MagicMock()
        sess.post.return_value = _fake_response(json_data={"items": [1, 2]})
        mock_session.return_value = sess
        result = execute_endpoint(
            method="POST",
            path="ep",
            payload={"a": 1},
            params={"forceRefresh": False},
        )
        sess.post.assert_called_once()
        _, kwargs = sess.post.call_args
        assert kwargs["json"] == {"a": 1}
        assert kwargs["params"] == {"forceRefresh": False}
        assert result == {"items": [1, 2]}

    def test_method_case_insensitive(self):
        with patch("common.easybeer.endpoint.get_session") as mock_session, \
             patch("common.easybeer.endpoint._auth", return_value=("u", "p")):
            sess = MagicMock()
            sess.get.return_value = _fake_response(json_data={})
            mock_session.return_value = sess
            execute_endpoint(method="get", path="x")  # lowercase
            sess.get.assert_called_once()

    def test_invalid_method_raises(self):
        with pytest.raises(ValueError, match="method non supportée"):
            execute_endpoint(method="DELETE", path="x")


# ─── Response model parsing ──────────────────────────────────────────────────

class TestResponseModel:
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_parses_via_from_dict(self, _auth, mock_session):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"value": 42, "label": "x"})
        mock_session.return_value = sess
        result = execute_endpoint(
            method="GET", path="x", response_model=_FakeModel,
        )
        assert isinstance(result, _FakeModel)
        assert result.value == 42
        assert result.label == "x"

    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_null_response_goes_through_from_dict_defensively(
        self, _auth, mock_session,
    ):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data=None)
        mock_session.return_value = sess
        result = execute_endpoint(
            method="GET", path="x", response_model=_FakeModel,
        )
        # from_dict doit encaisser None et renvoyer defaults
        assert isinstance(result, _FakeModel)
        assert result.value == 0

    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_no_model_returns_raw_dict(self, _auth, mock_session):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"raw": True})
        mock_session.return_value = sess
        assert execute_endpoint(method="GET", path="x") == {"raw": True}

    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_response_model_without_from_dict_returns_raw(
        self, _auth, mock_session,
    ):
        class NotAModel:  # pas de from_dict
            pass

        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"x": 1})
        mock_session.return_value = sess
        # Si le caller passe un type sans from_dict, on retourne le raw sans crash
        assert execute_endpoint(
            method="GET", path="x", response_model=NotAModel,
        ) == {"x": 1}


# ─── Cache L2 DB ─────────────────────────────────────────────────────────────

class TestCacheL2:
    @patch("common.eb_cache.cache_get")
    @patch("common._session.current_tenant_id", return_value="tenant-abc")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_cache_hit_skips_http(self, _auth, mock_session, _tid, mock_get):
        mock_get.return_value = {"cached": True}
        sess = MagicMock()
        mock_session.return_value = sess
        result = execute_endpoint(
            method="GET", path="x",
            cache_key="my_key", cache_item_id="123", cache_ttl=60,
        )
        assert result == {"cached": True}
        # Pas d'appel HTTP
        sess.get.assert_not_called()
        # cache_get appelé avec les bons paramètres
        mock_get.assert_called_once_with(
            "tenant-abc", "my_key", item_id="123", max_age_s=60,
        )

    @patch("common.eb_cache.cache_put")
    @patch("common.eb_cache.cache_get", return_value=None)
    @patch("common._session.current_tenant_id", return_value="tenant-abc")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_cache_miss_calls_api_and_persists(
        self, _auth, mock_session, _tid, _get, mock_put,
    ):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"fresh": True})
        mock_session.return_value = sess
        result = execute_endpoint(
            method="GET", path="x", cache_key="my_key", cache_item_id="v2",
        )
        assert result == {"fresh": True}
        sess.get.assert_called_once()
        mock_put.assert_called_once_with(
            "tenant-abc", "my_key", {"fresh": True}, item_id="v2",
        )

    @patch("common.eb_cache.cache_put")
    @patch("common.eb_cache.cache_get", return_value=None)
    @patch("common._session.current_tenant_id", return_value="tenant-abc")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_empty_response_not_cached(
        self, _auth, mock_session, _tid, _get, mock_put,
    ):
        """Ne pas écrire en cache si la réponse est vide / falsy."""
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={})
        mock_session.return_value = sess
        execute_endpoint(method="GET", path="x", cache_key="my_key")
        mock_put.assert_not_called()

    @patch("common.eb_cache.cache_get", side_effect=OSError("DB down"))
    @patch("common._session.current_tenant_id", return_value="t")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_cache_read_error_falls_back_to_api(
        self, _auth, mock_session, _tid, _cg,
    ):
        """Si le cache L2 est down, on retombe sur l'API sans crash."""
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"api": "ok"})
        mock_session.return_value = sess
        result = execute_endpoint(
            method="GET", path="x", cache_key="my_key",
        )
        assert result == {"api": "ok"}

    @patch("common.eb_cache.cache_put", side_effect=OSError("write fail"))
    @patch("common.eb_cache.cache_get", return_value=None)
    @patch("common._session.current_tenant_id", return_value="t")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_cache_write_error_does_not_break_return(
        self, _auth, mock_session, _tid, _cg, _cp,
    ):
        """Un échec d'écriture cache ne doit pas empêcher le retour de la donnée."""
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={"ok": 1})
        mock_session.return_value = sess
        # Ne doit pas lever, et la donnée API est bien retournée
        assert execute_endpoint(
            method="GET", path="x", cache_key="k",
        ) == {"ok": 1}


# ─── Cache + modèle typé (combinaison) ───────────────────────────────────────

class TestCacheAndModelCombined:
    @patch("common.eb_cache.cache_get")
    @patch("common._session.current_tenant_id", return_value="t")
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("u", "p"))
    def test_cache_hit_is_parsed_into_model(
        self, _auth, mock_session, _tid, mock_get,
    ):
        """Le cache L2 stocke du JSON dict ; on doit le parser via le modèle."""
        mock_get.return_value = {"value": 5, "label": "cached"}
        result = execute_endpoint(
            method="GET", path="x",
            cache_key="k", response_model=_FakeModel,
        )
        assert isinstance(result, _FakeModel)
        assert result.label == "cached"


# ─── HTTP auth passé partout ─────────────────────────────────────────────────

class TestAuthForwarded:
    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("user", "pass"))
    def test_auth_on_get(self, _auth, mock_session):
        sess = MagicMock()
        sess.get.return_value = _fake_response(json_data={})
        mock_session.return_value = sess
        execute_endpoint(method="GET", path="x")
        assert sess.get.call_args.kwargs["auth"] == ("user", "pass")

    @patch("common.easybeer.endpoint.get_session")
    @patch("common.easybeer.endpoint._auth", return_value=("user", "pass"))
    def test_auth_on_post(self, _auth, mock_session):
        sess = MagicMock()
        sess.post.return_value = _fake_response(json_data={})
        mock_session.return_value = sess
        execute_endpoint(method="POST", path="x", payload={"a": 1})
        assert sess.post.call_args.kwargs["auth"] == ("user", "pass")
