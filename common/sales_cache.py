"""
common/sales_cache.py
=====================
Cache DB des ventes mensuelles par goût canon (table ``monthly_sales``).

Utilisé par la page Prévisions pour éviter les appels répétés à
EasyBeer sur l'historique. Stratégie :
- Mois passés (clos) : fetch one-time depuis EB puis lus depuis la DB
- Mois en cours : refresh à chaque sync explicite

Exposé :
- ``sync_month_from_eb(tenant_id, year, month, fm)`` — extraction EB + upsert
- ``get_monthly_sales(tenant_id, years)`` — lit le cache
- ``ensure_history_synced(tenant_id, fm, ...)`` — boucle de sync sur plage de mois
- ``get_sync_status(tenant_id, ...)`` — status par mois (cached/missing)
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
from typing import Any

import pandas as pd

from db.conn import run_sql

_log = logging.getLogger("ferment.sales_cache")


# ─── DB — lecture / écriture ────────────────────────────────────────────────

def upsert_month_sales(
    tenant_id: str, year: int, month: int, gout_volumes: dict[str, float],
) -> int:
    """Insère ou met à jour les ventes d'un mois pour un tenant.

    Supprime les lignes existantes de ce (tenant, year, month) puis insère
    les nouvelles — opération atomique côté DB.
    """
    if not gout_volumes:
        run_sql(
            "DELETE FROM monthly_sales WHERE tenant_id = :tid AND year = :y AND month = :m",
            {"tid": tenant_id, "y": year, "m": month},
        )
        return 0

    run_sql(
        "DELETE FROM monthly_sales WHERE tenant_id = :tid AND year = :y AND month = :m",
        {"tid": tenant_id, "y": year, "m": month},
    )
    inserted = 0
    for gout, vol in gout_volumes.items():
        if not gout:
            continue
        run_sql(
            """
            INSERT INTO monthly_sales (tenant_id, year, month, gout_canon, volume_hl, fetched_at)
            VALUES (:tid, :y, :m, :g, :v, now())
            """,
            {"tid": tenant_id, "y": year, "m": month, "g": gout, "v": float(vol)},
        )
        inserted += 1
    _log.info("upsert_month_sales: tenant=%s %d-%02d → %d goûts", tenant_id, year, month, inserted)
    return inserted


def get_monthly_sales(
    tenant_id: str, years: list[int],
) -> dict[tuple[int, int, str], float]:
    """Lit le cache. Retourne ``{(year, month, gout): volume_hl}``."""
    if not years:
        return {}
    rows = run_sql(
        """
        SELECT year, month, gout_canon, volume_hl
        FROM monthly_sales
        WHERE tenant_id = :tid AND year = ANY(:years)
        """,
        {"tid": tenant_id, "years": list(years)},
    )
    if not isinstance(rows, list):
        return {}
    return {
        (int(r["year"]), int(r["month"]), str(r["gout_canon"])): float(r["volume_hl"] or 0)
        for r in rows
    }


def get_sync_status(
    tenant_id: str, year_from: int, year_to: int,
) -> dict[tuple[int, int], _dt.datetime | None]:
    """Retourne ``{(year, month): fetched_at}`` (None si jamais synchronisé)."""
    rows = run_sql(
        """
        SELECT year, month, MAX(fetched_at) AS last_fetched
        FROM monthly_sales
        WHERE tenant_id = :tid AND year BETWEEN :yf AND :yt
        GROUP BY year, month
        """,
        {"tid": tenant_id, "yf": year_from, "yt": year_to},
    )
    out: dict[tuple[int, int], _dt.datetime | None] = {}
    if isinstance(rows, list):
        for r in rows:
            out[(int(r["year"]), int(r["month"]))] = r["last_fetched"]
    return out


# ─── Extraction EB → DataFrame canonicalisé ──────────────────────────────────

def _month_iso_bounds(year: int, month: int) -> tuple[str, str]:
    """Retourne (debut_iso, fin_iso) pour le mois donné."""
    debut = _dt.datetime(year, month, 1, 0, 0, 0, tzinfo=_dt.UTC)
    if month == 12:
        fin = _dt.datetime(year + 1, 1, 1, tzinfo=_dt.UTC) - _dt.timedelta(milliseconds=1)
    else:
        fin = _dt.datetime(year, month + 1, 1, tzinfo=_dt.UTC) - _dt.timedelta(milliseconds=1)
    return (
        debut.strftime("%Y-%m-%dT00:00:00.000Z"),
        fin.strftime("%Y-%m-%dT23:59:59.999Z"),
    )


def fetch_month_sales_from_eb(
    year: int, month: int, fm: pd.DataFrame,
) -> dict[str, float]:
    """Récupère les ventes d'un mois depuis EB et canonicalise par goût.

    Retourne ``{gout_canon: volume_hl}``. Lève une exception si l'API échoue.
    """
    from common.easybeer import get_autonomie_stocks_excel_period
    from core.optimizer import apply_canonical_flavor, sanitize_gouts

    debut, fin = _month_iso_bounds(year, month)
    blob = get_autonomie_stocks_excel_period(debut, fin)

    raw = pd.read_excel(io.BytesIO(blob), header=None)
    hdr = 0
    for i in range(min(10, len(raw))):
        row = [str(x).strip() for x in raw.iloc[i].tolist()]
        if "Produit" in row and "Volume vendu (hl)" in row:
            hdr = i
            break
    df = pd.read_excel(io.BytesIO(blob), header=hdr)

    if "Volume vendu (hl)" not in df.columns or "Produit" not in df.columns:
        _log.warning("fetch_month_sales: colonnes manquantes pour %d-%02d", year, month)
        return {}

    df = apply_canonical_flavor(df, fm)
    df = sanitize_gouts(df)
    df["Volume vendu (hl)"] = pd.to_numeric(df["Volume vendu (hl)"], errors="coerce").fillna(0.0)

    agg = df.groupby("GoutCanon", as_index=False)["Volume vendu (hl)"].sum()
    return {
        str(row["GoutCanon"]).strip(): float(row["Volume vendu (hl)"])
        for _, row in agg.iterrows()
        if str(row["GoutCanon"]).strip()
    }


def sync_month_from_eb(
    tenant_id: str, year: int, month: int, fm: pd.DataFrame,
) -> int:
    """Pipeline complet : fetch EB → canonicalize → upsert DB."""
    gout_vol = fetch_month_sales_from_eb(year, month, fm)
    return upsert_month_sales(tenant_id, year, month, gout_vol)


# ─── Sync orchestration ──────────────────────────────────────────────────────

def ensure_history_synced(
    tenant_id: str, fm: pd.DataFrame,
    *,
    year_from: int, month_from: int,
    year_to: int, month_to: int,
    force_refresh_current: bool = True,
    progress_callback=None,
) -> dict[str, Any]:
    """Synchronise tous les mois manquants entre (year_from, month_from) et (year_to, month_to).

    Mois passés déjà cachés : skip. Mois en cours : refresh si force_refresh_current.
    Retourne ``{"synced": N, "skipped": N, "errors": [...]}``.
    """
    today = _dt.date.today()
    cache_status = get_sync_status(tenant_id, year_from, year_to)

    todo: list[tuple[int, int]] = []
    cur = (year_from, month_from)
    end = (year_to, month_to)
    while cur <= end:
        y, m = cur
        is_current_month = (y == today.year and m == today.month)
        already_cached = (y, m) in cache_status
        if not already_cached or (is_current_month and force_refresh_current):
            todo.append((y, m))
        # next month
        cur = (y + 1, 1) if m == 12 else (y, m + 1)

    synced, skipped, errors = 0, 0, []
    total = len(todo)
    for i, (y, m) in enumerate(todo, 1):
        if progress_callback:
            try:
                progress_callback(i, total, y, m)
            except Exception:
                _log.debug("progress callback raised", exc_info=True)
        try:
            sync_month_from_eb(tenant_id, y, m, fm)
            synced += 1
        except Exception as exc:
            _log.warning("Erreur sync %d-%02d: %s", y, m, exc, exc_info=True)
            errors.append(f"{y}-{m:02d}: {exc}")

    skipped = (
        ((year_to - year_from) * 12 + (month_to - month_from) + 1) - total
    )
    return {"synced": synced, "skipped": skipped, "errors": errors}
