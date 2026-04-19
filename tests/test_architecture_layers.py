"""Garde-fou architectural : empêche les régressions de couches.

Les règles (voir docs/ARCHITECTURE.md) :
- common/ ne doit JAMAIS importer depuis pages/
- common/services/ ne doit PAS importer nicegui
- common/easybeer/ ne doit PAS importer common/services/ (transport < domaine)
- pages/X.py ne doit PAS importer pages/Y.py (sauf auth et theme — utilitaires UI
  ubiquitaires)

Test léger (AST parsing, pas d'exécution) — tourne en <100 ms même sur tout le repo.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_COMMON = _REPO / "common"
_PAGES = _REPO / "pages"
_SERVICES = _REPO / "common" / "services"
_EASYBEER = _REPO / "common" / "easybeer"

# Pages "utilitaires" (mini-framework) — le seul import cross-page toléré.
_PAGE_UTILITIES = {"auth", "theme"}


def _iter_py(root: Path) -> Iterator[Path]:
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _collect_imports(path: Path) -> list[tuple[str, int]]:
    """Retourne [(module_dotted, lineno)] pour tous les imports du fichier."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Exclure les imports relatifs (level > 0) — ils restent dans la même couche par construction
                if node.level == 0:
                    out.append((node.module, node.lineno))
    return out


def _rel_for_msg(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO))
    except ValueError:
        return str(path)


# ─── Rule 1 : common/ ne doit pas importer pages/ ─────────────────────────

def test_common_does_not_import_pages():
    """common/ (transport + services + utils) ne dépend jamais de pages/.

    Permet aux services d'être utilisables depuis scripts CLI, cron,
    tests unitaires sans devoir instancier NiceGUI.
    """
    offenders: list[str] = []
    for py in _iter_py(_COMMON):
        for module, lineno in _collect_imports(py):
            if module == "pages" or module.startswith("pages."):
                offenders.append(f"{_rel_for_msg(py)}:{lineno} → {module}")
    assert not offenders, (
        "Les modules common/ doivent rester indépendants de pages/ :\n  "
        + "\n  ".join(offenders)
    )


# ─── Rule 2 : services/ ne doit pas importer nicegui ──────────────────────

def test_services_do_not_import_nicegui():
    """common/services/ doit tourner sans NiceGUI (testable en script pur)."""
    offenders: list[str] = []
    for py in _iter_py(_SERVICES):
        for module, lineno in _collect_imports(py):
            if module == "nicegui" or module.startswith("nicegui."):
                offenders.append(f"{_rel_for_msg(py)}:{lineno} → {module}")
    assert not offenders, (
        "common/services/ ne doit pas dépendre de NiceGUI :\n  "
        + "\n  ".join(offenders)
    )


# ─── Rule 3 : easybeer/ ne doit pas importer services/ ────────────────────

def test_easybeer_does_not_import_services():
    """Transport (easybeer) < domaine (services) : flèche ↓ uniquement.

    Si easybeer/ a besoin d'une règle métier, c'est probablement mal placé
    et devrait être extrait côté service.
    """
    offenders: list[str] = []
    for py in _iter_py(_EASYBEER):
        for module, lineno in _collect_imports(py):
            if module.startswith("common.services"):
                offenders.append(f"{_rel_for_msg(py)}:{lineno} → {module}")
    assert not offenders, (
        "common/easybeer/ (transport) ne doit pas dépendre de common/services/ "
        "(domaine) :\n  "
        + "\n  ".join(offenders)
    )


# ─── Rule 4 : pages/X ne doit pas importer pages/Y (sauf auth/theme) ──────

def test_pages_do_not_cross_import_except_utilities():
    """Évite l'emmêlement entre pages — si besoin de partage, extraire un service.

    Exceptions acceptables :
    - ``pages.auth`` / ``pages.theme`` : utilitaires UI ubiquitaires.
    - ``pages._xxx`` (préfixe underscore) : helpers UI privés explicitement
      réservés à être partagés entre pages d'une même feature (ex:
      ``_production_easybeer`` consommé par ``production``). Attention : toute
      logique métier testable doit aller dans ``common/services/``, pas dans
      un ``pages/_helper``.

    Toute autre dépendance cross-page publique est un code-smell.
    """
    offenders: list[str] = []
    for py in _iter_py(_PAGES):
        self_name = py.stem
        if self_name.startswith("_"):
            continue  # les privés peuvent tout importer entre eux
        for module, lineno in _collect_imports(py):
            if not module.startswith("pages."):
                continue
            parts = module.split(".")
            if len(parts) < 2:
                continue
            target = parts[1]
            if target in _PAGE_UTILITIES:
                continue
            if target.startswith("_"):
                continue  # helper UI privé — explicitement partagé
            if target == self_name:
                continue
            offenders.append(f"{_rel_for_msg(py)}:{lineno} → {module}")
    if offenders:
        pytest.fail(
            "Dépendances cross-pages détectées hors utilitaires "
            "{auth, theme, _helpers privés} :\n  "
            + "\n  ".join(offenders)
            + "\n\nSi du code est partagé entre pages publiques, extraire "
            "un service dans common/services/.",
        )
