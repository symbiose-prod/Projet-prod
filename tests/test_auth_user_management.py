"""Tests des fonctions de gestion des accès (common/auth.py).

run_sql est mocké : on vérifie surtout les garde-fous de sécurité
(rôle valide, scoping, pas d'auto-rétrogradation, dernier admin protégé).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.auth import (
    ALLOWED_ROLES,
    list_users_in_tenant,
    normalize_role,
    set_user_active,
    update_user_role,
)

_ADMIN = {
    "id": "u-admin", "tenant_id": "t1", "email": "boss@x.fr",
    "role": "admin", "is_active": True,
}
_OPERATEUR = {
    "id": "u-op", "tenant_id": "t1", "email": "max@x.fr",
    "role": "operateur", "is_active": True,
}


# ─── normalize_role ─────────────────────────────────────────────────────────

class TestNormalizeRole:
    def test_allowed_roles(self):
        assert ALLOWED_ROLES == frozenset({"user", "admin", "operateur"})

    def test_normalizes_case_and_space(self):
        assert normalize_role("  Admin ") == "admin"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Rôle invalide"):
            normalize_role("superadmin")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize_role(None)


# ─── update_user_role ───────────────────────────────────────────────────────

class TestUpdateUserRole:
    @patch("common.auth.run_sql")
    def test_invalid_role_raises_before_sql(self, m: MagicMock):
        with pytest.raises(ValueError, match="Rôle invalide"):
            update_user_role("u-admin", "u-op", "t1", "root")
        m.assert_not_called()

    @patch("common.auth.run_sql")
    def test_target_not_found_raises(self, m: MagicMock):
        m.side_effect = [[]]  # _get_user_in_tenant → vide
        with pytest.raises(ValueError, match="introuvable"):
            update_user_role("u-admin", "ghost", "t1", "user")

    @patch("common.auth.run_sql")
    def test_self_demote_refused(self, m: MagicMock):
        m.side_effect = [[_ADMIN]]  # target = soi-même
        with pytest.raises(ValueError, match="ton propre rôle"):
            update_user_role("u-admin", "u-admin", "t1", "user")

    @patch("common.auth.run_sql")
    def test_last_admin_demote_refused(self, m: MagicMock):
        # _get_user_in_tenant → admin ; _count_active_admins (hors lui) → 0
        m.side_effect = [[_ADMIN], [{"n": 0}]]
        with pytest.raises(ValueError, match="dernier administrateur"):
            update_user_role("other-admin", "u-admin", "t1", "user")

    @patch("common.auth.run_sql")
    def test_promote_operateur_to_admin_ok(self, m: MagicMock):
        updated = {**_OPERATEUR, "role": "admin"}
        # _get_user_in_tenant → operateur ; pas de check dernier-admin (cible
        # n'est pas admin) ; UPDATE → updated
        m.side_effect = [[_OPERATEUR], [updated]]
        out = update_user_role("u-admin", "u-op", "t1", "admin")
        assert out["role"] == "admin"

    @patch("common.auth.run_sql")
    def test_demote_admin_ok_when_other_admin_exists(self, m: MagicMock):
        updated = {**_ADMIN, "role": "user"}
        m.side_effect = [[_ADMIN], [{"n": 1}], [updated]]
        out = update_user_role("other-admin", "u-admin", "t1", "user")
        assert out["role"] == "user"


# ─── set_user_active ─────────────────────────────────────────────────────────

class TestSetUserActive:
    @patch("common.auth.run_sql")
    def test_target_not_found_raises(self, m: MagicMock):
        m.side_effect = [[]]
        with pytest.raises(ValueError, match="introuvable"):
            set_user_active("u-admin", "ghost", "t1", False)

    @patch("common.auth.run_sql")
    def test_self_deactivate_refused(self, m: MagicMock):
        m.side_effect = [[_ADMIN]]
        with pytest.raises(ValueError, match="ton propre compte"):
            set_user_active("u-admin", "u-admin", "t1", False)

    @patch("common.auth.run_sql")
    def test_last_admin_deactivate_refused(self, m: MagicMock):
        m.side_effect = [[_ADMIN], [{"n": 0}]]
        with pytest.raises(ValueError, match="dernier administrateur"):
            set_user_active("other-admin", "u-admin", "t1", False)

    @patch("common.auth.run_sql")
    def test_deactivate_operateur_ok(self, m: MagicMock):
        updated = {**_OPERATEUR, "is_active": False}
        m.side_effect = [[_OPERATEUR], [updated]]
        out = set_user_active("u-admin", "u-op", "t1", False)
        assert out["is_active"] is False

    @patch("common.auth.run_sql")
    def test_reactivate_ok_without_admin_checks(self, m: MagicMock):
        # active=True → aucun garde-fou « dernier admin » : 1 seul SQL (UPDATE
        # après le _get_user_in_tenant).
        updated = {**_OPERATEUR, "is_active": True}
        m.side_effect = [[_OPERATEUR], [updated]]
        out = set_user_active("u-admin", "u-op", "t1", True)
        assert out["is_active"] is True


# ─── list_users_in_tenant ────────────────────────────────────────────────────

class TestListUsers:
    @patch("common.auth.run_sql")
    def test_returns_rows(self, m: MagicMock):
        m.return_value = [_ADMIN, _OPERATEUR]
        out = list_users_in_tenant("t1")
        assert len(out) == 2
        assert out[0]["email"] == "boss@x.fr"

    @patch("common.auth.run_sql")
    def test_empty_returns_list(self, m: MagicMock):
        m.return_value = None
        assert list_users_in_tenant("t1") == []
