"""
CSV parsing and normalization for Delta Exchange-style fill history exports.
"""

from __future__ import annotations

import io
import math
import re
from datetime import datetime
from typing import Any

import pandas as pd

# Delta Fill History: datetime through timezone, drop trailing text (e.g. "IST Asia/Kolkata").
_DELTA_ISO_THROUGH_TZ = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*(Z|[+-]\d{2}:\d{2}|[+-]\d{4})\b",
    re.IGNORECASE,
)


# Canonical column -> acceptable source names (matched via _normalize_header)
COLUMN_ALIASES: dict[str, list[str]] = {
    "symbol": [
        "Contract",
        "symbol",
        "product_symbol",
        "instrument",
        "contract",
        "market",
        "pair",
    ],
    "side": ["side", "direction", "order_side"],
    "price": [
        "Exec.Price",
        "price",
        "fill_price",
        "avg_price",
        "execution_price",
        "px",
    ],
    "quantity": [
        "Filled Qty",
        "quantity",
        "size",
        "filled_size",
        "filled_qty",
        "qty",
        "amount",
        "filled",
        "fill_size",
        "volume",
    ],
    "fee": [
        "Fees paid",
        "fee",
        "fees",
        "commission",
        "trading_fee",
        "txn_fee",
    ],
    # Use _find_timestamp_column + _TIMESTAMP_ALIASES (Time, created_at only)
    "timestamp": [],
}

# Fill History: only map time from these CSV headers (normalized match is case-insensitive).
_TIMESTAMP_ALIASES: tuple[str, ...] = (
    "Time",
    "created_at",
)


def _normalize_header(name: str) -> str:
    """
    Normalize CSV headers for matching: lowercase, spaces and dots → underscores.
    Example: "Exec.Price" → "exec_price", "Filled Qty" → "filled_qty".
    """
    s = str(name).strip().lower()
    s = re.sub(r"[\s.]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _to_numeric_series(series: pd.Series) -> pd.Series:
    """Parse numbers from CSV strings (commas, whitespace, empty)."""
    if series.dtype != object and not pd.api.types.is_string_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
        .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
    )
    cleaned = cleaned.mask(cleaned == "")
    return pd.to_numeric(cleaned, errors="coerce")


def _build_lookup(columns: list[str]) -> dict[str, str]:
    """Map normalized header -> original column name."""
    return {_normalize_header(c): c for c in columns}


def _fix_clean_join(m: re.Match[str]) -> str:
    left = m.group(1).rstrip()
    tz = m.group(2)
    if tz.upper() == "Z":
        return left + "Z"
    return left + tz


def _clean_delta_time_string(val: Any) -> Any:
    """
    Strip junk after timezone offset, e.g.
    '2026-03-23 11:56:35.550936+05:30 IST Asia/Kolkata' -> '2026-03-23 11:56:35.550936+05:30'
    """
    if val is None:
        return val
    if isinstance(val, (pd.Timestamp, datetime)):
        return val
    if isinstance(val, float) and math.isnan(val):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val
    if pd.isna(val):
        return val
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return val
    m = _DELTA_ISO_THROUGH_TZ.match(s)
    if m:
        return _fix_clean_join(m)
    return s


def _apply_delta_time_cleanup(series: pd.Series) -> pd.Series:
    """Clean string timestamps before pd.to_datetime (Delta Exchange Time column)."""
    return series.map(_clean_delta_time_string)


def _coerce_datetime_series(series: pd.Series) -> pd.Series:
    """Parse timestamps from strings, Excel serials, Unix epoch (s/ms/us), mixed formats."""
    if series is None or len(series) == 0:
        return series
    s = series
    if pd.api.types.is_datetime64_any_dtype(s):
        out = pd.to_datetime(s, utc=True, errors="coerce")
        return out

    num = pd.to_numeric(s, errors="coerce")
    nn = int(num.notna().sum())
    if nn > 0 and nn >= max(1, int(len(s) * 0.4)):
        mx = float(num.max())
        if mx > 1e16:
            return pd.to_datetime(num, unit="us", utc=True, errors="coerce")
        if mx > 1e14:
            return pd.to_datetime(num, unit="ms", utc=True, errors="coerce")
        if mx > 1e11:
            return pd.to_datetime(num, unit="ms", utc=True, errors="coerce")
        if mx > 1e9:
            return pd.to_datetime(num, unit="s", utc=True, errors="coerce")

    out = pd.to_datetime(s, errors="coerce", utc=True)
    if out.isna().all() or (out.notna().sum() < len(s) * 0.5):
        alt = pd.to_datetime(s, errors="coerce", utc=True, dayfirst=True)
        if alt.notna().sum() >= out.notna().sum():
            out = alt
    try:
        mix = pd.to_datetime(s, errors="coerce", utc=True, format="mixed")
        if mix.notna().sum() > out.notna().sum():
            out = mix
    except (TypeError, ValueError):
        pass
    return out


def _find_timestamp_column(
    lookup: dict[str, str],
    used_originals: set[str],
) -> str | None:
    """Only 'Time' or 'created_at' (any casing / spacing → underscores). No other CSV columns."""
    for alias in _TIMESTAMP_ALIASES:
        key = _normalize_header(alias)
        if key and key in lookup and lookup[key] not in used_originals:
            return lookup[key]
    return None


def _find_column(
    canonical: str,
    aliases: list[str],
    lookup: dict[str, str],
    used_originals: set[str],
) -> str | None:
    """Match CSV header to canonical field using normalized exact names only."""
    candidates = [_normalize_header(canonical)] + [_normalize_header(a) for a in aliases]
    for key in candidates:
        if key and key in lookup and lookup[key] not in used_originals:
            return lookup[key]
    return None


def parse_uploaded_csv(
    file_storage: Any,
) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    """
    Read uploaded CSV and return a normalized dataframe plus warnings.

    Maps vendor columns (see COLUMN_ALIASES) to symbol, side, price, quantity, fee,
    timestamp. Missing columns get safe defaults; timestamps prefer Time / created_at.

    Returns:
        out: DataFrame with canonical columns ready for insights.compute_* .
        warnings: Human-readable parse notes (caller may log or expose).
        meta: Reserved for future metadata (currently {}).
    """
    warnings: list[str] = []
    if file_storage is None or not getattr(file_storage, "filename", None):
        raise ValueError("No file selected.")

    raw = file_storage.read()
    if not raw or not str(raw).strip():
        raise ValueError("The file is empty.")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    buf = io.StringIO(text)
    try:
        df = pd.read_csv(buf)
    except Exception as e:
        raise ValueError(f"Could not parse CSV: {e}") from e

    if df.empty:
        raise ValueError("The CSV has no rows.")

    df.columns = [str(c).strip() for c in df.columns]
    detected = list(df.columns)
    print(
        f"[Trading Insight] CSV columns detected ({len(detected)}): {detected}",
        flush=True,
    )

    lookup = _build_lookup(detected)
    used: set[str] = set()
    out = pd.DataFrame()

    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical == "timestamp":
            src = _find_timestamp_column(lookup, used)
        else:
            src = _find_column(canonical, aliases, lookup, used)
        if src is None:
            if canonical == "timestamp":
                out[canonical] = pd.Series([pd.NaT] * len(df), dtype="datetime64[ns, UTC]")
                continue
            warnings.append(f"Column '{canonical}' not found; using empty/default values.")
            if canonical == "quantity":
                out[canonical] = pd.Series(0.0, index=df.index, dtype="float64")
            elif canonical in ("fee", "price"):
                out[canonical] = pd.Series(0.0, index=df.index, dtype="float64")
            else:
                out[canonical] = pd.Series([pd.NA] * len(df), dtype=object)
            continue
        used.add(src)
        out[canonical] = df[src]

    # Coerce types
    for col in ("price", "quantity", "fee"):
        if col in out.columns:
            out[col] = _to_numeric_series(out[col])

    # No NA in core numeric columns (Fill History safe defaults)
    for col in ("price", "quantity", "fee"):
        if col in out.columns:
            out[col] = out[col].fillna(0.0).astype("float64")

    if "timestamp" in out.columns:
        ts_raw = out["timestamp"]
        raw_preview = ts_raw.head(3).tolist()
        cleaned_ts = _apply_delta_time_cleanup(ts_raw)
        cleaned_preview = cleaned_ts.head(3).tolist()
        primary = pd.to_datetime(cleaned_ts, utc=True, errors="coerce")
        fallback = _coerce_datetime_series(cleaned_ts)
        out["timestamp"] = primary.fillna(fallback)
        parsed_preview = out["timestamp"].head(3).tolist()
        print(f"[Trading Insight] Time raw (first 3): {raw_preview}", flush=True)
        print(f"[Trading Insight] Time cleaned (first 3): {cleaned_preview}", flush=True)
        print(f"[Trading Insight] Time parsed (first 3): {parsed_preview}", flush=True)

        nrows = len(out)
        ok = int(out["timestamp"].notna().sum())
        ok_ratio = (ok / nrows) if nrows else 0.0
        if ok == 0 or ok_ratio < 0.5:
            warnings.append(
                "Timestamp data not available or could not be parsed"
            )

    if "side" in out.columns:
        m = out["side"].notna()
        out.loc[m, "side"] = (
            out.loc[m, "side"].astype(str).str.strip().str.lower().replace({"": pd.NA})
        )

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out.loc[out["symbol"].isin(["", "nan", "None"]), "symbol"] = pd.NA

    meta: dict[str, Any] = {}

    non_null = {col: int(out[col].notna().sum()) for col in out.columns}
    print(f"[Trading Insight] Mapped columns: {list(out.columns)}", flush=True)
    print(f"[Trading Insight] Non-null counts: {non_null}", flush=True)
    print(
        "[Trading Insight] Sample rows (first 3, cleaned):\n"
        + out.head(3).to_string(),
        flush=True,
    )

    return out, warnings, meta
