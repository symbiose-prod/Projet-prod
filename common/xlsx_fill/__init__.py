"""
common/xlsx_fill — Excel/PDF template filling package.

Re-exports all public symbols so that existing imports keep working:
    from common.xlsx_fill import fill_fiche_xlsx, build_bl_enlevements_pdf, ...
"""
from __future__ import annotations

from .fiche_production import fill_fiche_xlsx
from .bl_excel import fill_bl_enlevements_xlsx
from .bl_pdf import build_bl_enlevements_pdf
from ._tank_ruler import interpolate_ruler_height

__all__ = [
    "fill_fiche_xlsx",
    "fill_bl_enlevements_xlsx",
    "build_bl_enlevements_pdf",
    "interpolate_ruler_height",
]
