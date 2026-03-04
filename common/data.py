from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import pandas as pd
import yaml

_log = logging.getLogger("ferment.data")

CONFIG_DEFAULT: dict[str, Any] = {
    "data_files": {
        "main_table": "data/production.xlsx",
        "flavor_map": "data/flavor_map.csv",
    },
    "images_dir": "assets",
}

_BUSINESS_DEFAULTS: dict = {
    "default_loss_large": 800,
    "default_loss_small": 400,
    "ddm_days": 365,
    "price_ref_hl": 400.0,
    "max_slots": 6,
    "default_window_days": 60,
    "tanks": {
        "Cuve de 7200L (1 goût)": {
            "capacity": 7200, "transfer_loss": 400, "bottling_loss": 400,
            "nb_gouts": 1, "nominal_hL": 64.0,
        },
        "Cuve de 5200L (1 goût)": {
            "capacity": 5200, "transfer_loss": 200, "bottling_loss": 200,
            "nb_gouts": 1, "nominal_hL": 48.0,
        },
    },
}

def load_config() -> dict[str, Any]:
    path = "config.yaml"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {**CONFIG_DEFAULT, **(yaml.safe_load(f) or {})}
    return CONFIG_DEFAULT


@lru_cache(maxsize=1)
def get_business_config() -> dict[str, Any]:
    """Retourne la section 'business' de config.yaml avec valeurs par défaut."""
    cfg = load_config()
    biz = cfg.get("business", {})
    result = {**_BUSINESS_DEFAULTS, **{k: v for k, v in biz.items() if k != "tanks"}}
    # Merge tanks: fichier config prend le dessus sur les défauts
    result["tanks"] = {**_BUSINESS_DEFAULTS["tanks"], **(biz.get("tanks") or {})}
    return result


_SECURITY_DEFAULTS: dict[str, Any] = {
    "min_password_length": 10,
    "lockout_thresholds": [
        {"failures": 5, "seconds": 300},
        {"failures": 10, "seconds": 1800},
        {"failures": 15, "seconds": 7200},
    ],
}


@lru_cache(maxsize=1)
def get_security_config() -> dict[str, Any]:
    """Retourne la section 'security' de config.yaml avec valeurs par défaut."""
    cfg = load_config()
    sec = cfg.get("security", {})
    return {**_SECURITY_DEFAULTS, **sec}


@lru_cache(maxsize=1)
def get_paths() -> tuple[str, str, str]:
    cfg = load_config()
    return (
        cfg["data_files"]["main_table"],
        cfg["data_files"]["flavor_map"],
        cfg["images_dir"],
    )

@lru_cache(maxsize=2)
def _read_table_cached() -> pd.DataFrame:
    """Cache interne — ne jamais appeler directement (retourne une ref mutable)."""
    main_table, _, _ = get_paths()

    if not os.path.exists(main_table):
        return pd.DataFrame()

    lower = main_table.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
            return pd.read_excel(main_table, engine="openpyxl", header=None)
        elif lower.endswith(".xls"):
            return pd.read_excel(main_table, engine="xlrd", header=None)
        elif lower.endswith((".csv", ".txt")):
            try:
                return pd.read_csv(main_table, sep=";", engine="python", header=None)
            except (pd.errors.ParserError, ValueError, UnicodeDecodeError):
                _log.debug("Erreur lecture CSV sep=;, tentative sep=,", exc_info=True)
                return pd.read_csv(main_table, sep=",", engine="python", header=None)
        else:
            try:
                return pd.read_excel(main_table, engine="openpyxl", header=None)
            except (ValueError, KeyError, OSError):
                _log.debug("Erreur lecture xlsx, tentative xlrd", exc_info=True)
                return pd.read_excel(main_table, engine="xlrd", header=None)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        _log.exception("Erreur lecture %s", main_table)
        return pd.DataFrame()


def read_table() -> pd.DataFrame:
    """Retourne une COPIE du tableau principal (safe pour mutation)."""
    return _read_table_cached().copy()


@lru_cache(maxsize=2)
def _read_flavor_map_cached() -> pd.DataFrame:
    """Cache interne — ne jamais appeler directement."""
    _, flavor_map, _ = get_paths()
    if not os.path.exists(flavor_map):
        return pd.DataFrame(columns=["name", "canonical"])
    try:
        return pd.read_csv(flavor_map, encoding="utf-8")
    except (pd.errors.ParserError, ValueError, UnicodeDecodeError):
        _log.debug("Erreur lecture flavor_map, tentative sep=;", exc_info=True)
        return pd.read_csv(flavor_map, encoding="utf-8", sep=";")


def read_flavor_map() -> pd.DataFrame:
    """Retourne une COPIE de la flavor map (safe pour mutation)."""
    return _read_flavor_map_cached().copy()
