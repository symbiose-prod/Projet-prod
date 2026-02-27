import os, yaml, pandas as pd
from functools import lru_cache

CONFIG_DEFAULT = {
    "data_files": {
        "main_table": "data/production.xlsx",
        "flavor_map": "data/flavor_map.csv",
    },
    "images_dir": "assets",
}

def load_config() -> dict:
    path = "config.yaml"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return {**CONFIG_DEFAULT, **(yaml.safe_load(f) or {})}
    return CONFIG_DEFAULT

@lru_cache(maxsize=1)
def get_paths():
    cfg = load_config()
    return (
        cfg["data_files"]["main_table"],
        cfg["data_files"]["flavor_map"],
        cfg["images_dir"],
    )

@lru_cache(maxsize=2)
def _read_table_cached():
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
            except Exception:
                return pd.read_csv(main_table, sep=",", engine="python", header=None)
        else:
            try:
                return pd.read_excel(main_table, engine="openpyxl", header=None)
            except Exception:
                return pd.read_excel(main_table, engine="xlrd", header=None)
    except Exception:
        import logging
        logging.getLogger("ferment.data").exception("Erreur lecture %s", main_table)
        return pd.DataFrame()


def read_table() -> pd.DataFrame:
    """Retourne une COPIE du tableau principal (safe pour mutation)."""
    return _read_table_cached().copy()


@lru_cache(maxsize=2)
def _read_flavor_map_cached():
    """Cache interne — ne jamais appeler directement."""
    _, flavor_map, _ = get_paths()
    if not os.path.exists(flavor_map):
        return pd.DataFrame(columns=["name", "canonical"])
    try:
        return pd.read_csv(flavor_map, encoding="utf-8")
    except Exception:
        return pd.read_csv(flavor_map, encoding="utf-8", sep=";")


def read_flavor_map() -> pd.DataFrame:
    """Retourne une COPIE de la flavor map (safe pour mutation)."""
    return _read_flavor_map_cached().copy()
