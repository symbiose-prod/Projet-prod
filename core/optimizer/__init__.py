"""
core/optimizer — Production optimization package.

Re-exports all public symbols so that existing imports keep working:
    from core.optimizer import compute_plan, fix_text, parse_stock, ...
"""
from __future__ import annotations

# --- excel I/O ---
from .excel_io import (
    DEFAULT_WINDOW_DAYS,
    read_input_excel_and_period_from_bytes,
)

# --- flavors ---
from .flavors import (
    BLOCKED_LABELS_EXACT,
    BLOCKED_LABELS_LOWER,
    apply_canonical_flavor,
    load_flavor_map_from_path,
    sanitize_gouts,
)

# --- losses ---
from .losses import compute_losses_table_v48

# --- normalization ---
from .normalization import (
    ACCENT_CHARS,
    CUSTOM_REPLACEMENTS,
    _norm_colname,
    _pick_column,
    fix_text,
)

# --- parsing ---
from .parsing import (
    ALLOWED_FORMATS,
    VOL_TOL,
    detect_header_row,
    is_allowed_format,
    parse_days_from_b2,
    parse_stock,
    rows_to_keep_by_fill,
    safe_num,
)

# --- planning ---
from .planning import (
    EPS,
    ROUND_TO_CARTON,
    compute_plan,
)

__all__ = [
    # normalization
    "_norm_colname", "_pick_column", "fix_text", "ACCENT_CHARS", "CUSTOM_REPLACEMENTS",
    # parsing
    "ALLOWED_FORMATS", "VOL_TOL", "parse_stock", "safe_num", "is_allowed_format",
    "detect_header_row", "rows_to_keep_by_fill", "parse_days_from_b2",
    # flavors
    "load_flavor_map_from_path", "apply_canonical_flavor", "sanitize_gouts",
    "BLOCKED_LABELS_EXACT", "BLOCKED_LABELS_LOWER",
    # planning
    "ROUND_TO_CARTON", "EPS", "compute_plan",
    # losses
    "compute_losses_table_v48",
    # excel I/O
    "DEFAULT_WINDOW_DAYS", "read_input_excel_and_period_from_bytes",
]
