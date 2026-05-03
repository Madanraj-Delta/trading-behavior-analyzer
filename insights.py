"""
Metrics and behavior-focused insights from normalized trade data.

All monetary aggregates are numeric; the Flask app labels values as USD in the UI.
Does not compute PnL — behavior and execution stats only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd


def compute_breakdown(df: pd.DataFrame, *, top_symbols: int = 8, top_large_fills: int = 5) -> dict[str, Any]:
    """
    Structured summaries for the UI — symbol mix, side split, largest fills, date span.
    """
    n = int(len(df))
    fee_s = df["fee"].fillna(0).astype("float64") if "fee" in df.columns else pd.Series(0.0, index=df.index)
    price = df["price"].fillna(0).astype("float64") if "price" in df.columns else pd.Series(0.0, index=df.index)
    qty = df["quantity"].fillna(0).astype("float64") if "quantity" in df.columns else pd.Series(0.0, index=df.index)
    notional = (price * qty).abs()

    symbol_rows: list[dict[str, Any]] = []
    if n and "symbol" in df.columns:
        clean_sym = df["symbol"].astype(str).str.strip()
        clean_sym = clean_sym.mask(clean_sym.str.len() == 0, pd.NA)
        clean_sym = clean_sym.mask(clean_sym.str.lower().isin({"nan", "none", "<na>"}), pd.NA)
        sub = df.assign(_sym=clean_sym).dropna(subset=["_sym"])
        if len(sub):
            vc = sub["_sym"].value_counts()
            fee_groups = sub.groupby("_sym")["fee"].sum() if "fee" in sub.columns else None
            for sym_val in vc.head(top_symbols).index.tolist():
                c = int(vc.loc[sym_val])
                fee_val = round(float(fee_groups.loc[sym_val]), 4) if fee_groups is not None else 0.0
                symbol_rows.append(
                    {
                        "symbol": str(sym_val),
                        "fills": c,
                        "pct": round(100.0 * c / max(n, 1), 1),
                        "fees": fee_val,
                    }
                )

    side_counts = {"buy": 0, "sell": 0, "other": 0}
    if "side" in df.columns:
        for raw in df["side"]:
            if pd.isna(raw):
                side_counts["other"] += 1
                continue
            s = str(raw).strip().lower()
            if s in ("buy", "b", "long", "bid"):
                side_counts["buy"] += 1
            elif s in ("sell", "s", "ask", "short"):
                side_counts["sell"] += 1
            else:
                side_counts["other"] += 1

    bt = side_counts["buy"] + side_counts["sell"] + side_counts["other"]
    if bt:
        side_pct = {k: round(100.0 * side_counts[k] / bt, 1) for k in ("buy", "sell", "other")}
    else:
        side_pct = {"buy": 0.0, "sell": 0.0, "other": 0.0}

    largest_fills: list[dict[str, str]] = []
    if n:
        idx_order = notional.nlargest(min(top_large_fills, n)).index.tolist()
        for i in idx_order:
            row = df.loc[i]
            sym = str(row["symbol"]).strip() if "symbol" in df.columns and pd.notna(row.get("symbol")) else "—"
            side_raw = row.get("side")
            if pd.isna(side_raw):
                side_disp = "—"
            else:
                side_disp = str(side_raw).strip().title()
            nv = float(notional.loc[i])
            fv = float(fee_s.loc[i])
            ts_str = ""
            if "timestamp" in df.columns:
                ts = row.get("timestamp")
                if pd.notna(ts):
                    if isinstance(ts, pd.Timestamp):
                        ts_str = ts.strftime("%Y-%m-%d %H:%M")
                    elif isinstance(ts, datetime):
                        ts_str = ts.strftime("%Y-%m-%d %H:%M")
                    else:
                        ts_str = str(ts)[:16]
            largest_fills.append(
                {
                    "symbol": sym[:48],
                    "side": side_disp[:12],
                    "notional": f"{nv:,.2f}",
                    "fee": f"{fv:,.4f}",
                    "when": ts_str or "—",
                }
            )

    span_start = span_end = ""
    if n and "timestamp" in df.columns:
        ts_all = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        ok = ts_all.notna()
        if int(ok.sum()) >= max(1, int(n * 0.5)):
            tmin = ts_all[ok].min()
            tmax = ts_all[ok].max()
            span_start = tmin.strftime("%Y-%m-%d") if pd.notna(tmin) else ""
            span_end = tmax.strftime("%Y-%m-%d") if pd.notna(tmax) else ""

    return {
        "symbol_rows": symbol_rows,
        "side_counts": side_counts,
        "side_pct": side_pct,
        "has_side": "side" in df.columns,
        "largest_fills": largest_fills,
        "span_start": span_start,
        "span_end": span_end,
        "has_span": bool(span_start and span_end),
    }


def compute_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Trades, fees, symbols, and trade-size stats only."""
    n = int(len(df))
    fee = df["fee"].fillna(0).astype("float64") if "fee" in df.columns else pd.Series(0.0, index=df.index)
    price = df["price"].fillna(0).astype("float64") if "price" in df.columns else pd.Series(0.0, index=df.index)
    qty = df["quantity"].fillna(0).astype("float64") if "quantity" in df.columns else pd.Series(0.0, index=df.index)

    total_fees = float(fee.sum())
    notional = (price * qty).abs()
    avg_trade_size = float(notional.mean()) if n else 0.0
    median_trade_size = float(notional.median()) if n else 0.0

    most_traded_symbol: str | None = None
    top_symbol_share = 0.0
    if "symbol" in df.columns and df["symbol"].notna().any():
        vc = df["symbol"].astype(str).str.strip()
        vc = vc[vc != ""]
        if len(vc):
            counts = vc.value_counts()
            most_traded_symbol = str(counts.index[0])
            top_symbol_share = float(counts.iloc[0] / max(n, 1))

    scalping_hint = (
        n >= 5
        and median_trade_size > 1e-12
        and avg_trade_size < 0.65 * median_trade_size
    )

    notional_sum = float(notional.sum()) if n else 0.0
    fee_rate_pct = (
        (100.0 * total_fees / notional_sum) if notional_sum > 1e-12 else 0.0
    )
    if fee_rate_pct <= 0:
        fee_rate_display = "0%"
    else:
        trimmed = f"{fee_rate_pct:.4f}".rstrip("0").rstrip(".")
        fee_rate_display = f"{trimmed}%"

    unique_symbols = 0
    if "symbol" in df.columns and n:
        s = df["symbol"].astype(str).str.strip()
        s = s[s.str.len() > 0]
        s = s[~s.str.lower().isin({"nan", "none", "<na>"})]
        if len(s):
            unique_symbols = int(s.nunique())

    return {
        "total_trades": n,
        "total_fees": total_fees,
        "most_traded_symbol": most_traded_symbol,
        "top_symbol_share": top_symbol_share,
        "top_symbol_share_pct": round(100.0 * top_symbol_share, 1) if n else 0.0,
        "fee_rate_pct": fee_rate_pct,
        "fee_rate_display": fee_rate_display,
        "unique_symbols": unique_symbols,
        "avg_trade_size": avg_trade_size,
        "median_trade_size": median_trade_size,
        "scalping_hint": scalping_hint,
    }


def generate_insights(metrics: dict[str, Any]) -> list[str]:
    """Frequency, fees, symbol focus, sizing — behavior only."""
    insights: list[str] = []
    total_trades = int(metrics["total_trades"])
    total_fees = float(metrics["total_fees"])

    if total_trades > 50:
        insights.append("You are trading very frequently")

    if total_fees > 0:
        insights.append("Frequent trading is increasing your USD fee burden")

    sym = metrics.get("most_traded_symbol")
    if sym and total_trades > 0:
        insights.append(f"You are heavily focused on {sym}")

    if metrics.get("scalping_hint"):
        insights.append(
            "You are placing many small trades (possible scalping behavior)"
        )

    return insights


def generate_summary_lines(metrics: dict[str, Any]) -> list[str]:
    """Two-line narrative summary."""
    t = int(metrics["total_trades"])
    fees = float(metrics["total_fees"])
    scalping = bool(metrics.get("scalping_hint"))
    fee_per_trade = fees / max(t, 1)

    line1_parts: list[str] = []
    if t > 50:
        line1_parts.append("You are trading frequently")
    elif t > 20:
        line1_parts.append("You have an active fill history")
    else:
        line1_parts.append("Your fill volume is relatively modest")

    if fees > 0:
        line1_parts.append("with meaningful fees (USD) accruing across fills")
    else:
        line1_parts.append("with little or no USD fees shown in this export")

    line1 = " ".join(line1_parts) + "."

    if (t > 50 and fees > t * 0.25) or (fee_per_trade > 1.0 and t > 30):
        line2 = (
            "Your current execution pattern may not be efficient unless benefits "
            "clearly outweigh transaction costs."
        )
    elif scalping:
        line2 = (
            "Small average trade size suggests scalping-style execution; monitor "
            "fee drag relative to what you expect to capture per trade."
        )
    else:
        line2 = (
            "Consider aligning trade frequency and sizing with your risk plan and "
            "whether each fill adds clear value."
        )

    return [line1, line2]
