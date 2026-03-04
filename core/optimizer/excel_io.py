"""
core/optimizer/excel_io.py
==========================
Read Excel input from uploaded bytes (Streamlit/NiceGUI upload).
"""
from __future__ import annotations

import io
import logging

import pandas as pd

_log = logging.getLogger("ferment.optimizer.excel_io")

from .parsing import detect_header_row, parse_days_from_b2, rows_to_keep_by_fill

DEFAULT_WINDOW_DAYS = 60


def read_input_excel_and_period_from_bytes(file_bytes: bytes):
    """Meme logique que _from_path mais pour des bytes (uploader)."""
    import openpyxl  # noqa: F401

    raw = pd.read_excel(io.BytesIO(file_bytes), header=None)
    header_idx = detect_header_row(raw)
    df = pd.read_excel(io.BytesIO(file_bytes), header=header_idx)
    keep_mask = rows_to_keep_by_fill(file_bytes, header_idx)
    if len(keep_mask) < len(df):
        keep_mask = keep_mask + [True] * (len(df) - len(keep_mask))
    df = df.iloc[[i for i, k in enumerate(keep_mask) if k]].reset_index(drop=True)

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb[wb.sheetnames[0]]
        b2_val = ws["B2"].value
        wd = parse_days_from_b2(b2_val)
    except Exception:
        _log.debug("Erreur parsing periode depuis B2", exc_info=True)
        wd = None
    return df, (wd if wd and wd > 0 else DEFAULT_WINDOW_DAYS)
