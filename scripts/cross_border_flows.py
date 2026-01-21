#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Interactive monthly cross-border trade (stacked bar) for Ukraine using Energy-Charts /cbet.

- Downloads hourly CBET data (MW, per neighbour), CET/CEST aligned (Europe/Berlin)
- Aggregates to monthly GWh
- Drops Russia
- Orders partners by total absolute volume; forces Belarus last
- Inserts a visible break after 2022-02 (data gap)
- Exports a self-contained HTML (Plotly) for embedding in an online dashboard
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter, Retry
from pathlib import Path

import plotly.graph_objects as go


# ----------------------- Config -----------------------
API_BASE = "https://api.energy-charts.info"
ENDPOINT = "/cbet"

COUNTRY = "UA"
TZ = "Europe/Berlin"

FETCH_PADDING = pd.Timedelta(days=1)
CHUNK_SIZE = pd.DateOffset(months=6)

# Where to save outputs (change if you like)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PLOTS_DIR = BASE_DIR / "plots"
OUT_HTML = PLOTS_DIR / "ua_crossborder_monthly.html"
OUT_XLSX_HOURLY_WIDE = DATA_DIR / "ua_cbet_hourly_wide.xlsx"   # optional convenience export
# ------------------------------------------------------


def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5, connect=5, read=5, backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    s.headers.update({"Accept": "application/json", "User-Agent": "ua-dashboard-cbet/1.0"})
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def fetch_cbet(session: requests.Session, country: str, start_iso: Optional[str], end_iso: Optional[str]) -> Any:
    url = f"{API_BASE}{ENDPOINT}"
    params: Dict[str, str] = {"country": country.lower()}
    if start_iso:
        params["start"] = start_iso
    if end_iso:
        params["end"] = end_iso
    r = session.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _countries_list_to_long(payload: dict) -> pd.DataFrame:
    ts = pd.to_datetime(payload["unix_seconds"], unit="s", utc=True)
    rows: List[Tuple[pd.Timestamp, str, float]] = []
    for it in payload.get("countries", []):
        code = it.get("code") or it.get("country") or it.get("bzn") or it.get("name")
        values = it.get("values") or it.get("data") or it.get("series")
        if code is None or not isinstance(values, list):
            continue
        if len(values) != len(ts):
            continue
        rows.extend((t, str(code), v) for t, v in zip(ts, values))
    if not rows:
        raise ValueError("Found 'countries' list but no usable (code, values) pairs.")
    return pd.DataFrame(rows, columns=["timestamp_utc", "neighbor", "value_gw"])


def melt_response(payload: Any) -> pd.DataFrame:
    """
    Normalize payload to long form with UTC timestamps:
    columns = [timestamp_utc (tz-aware), neighbor, value_gw]
    """
    if isinstance(payload, dict) and "unix_seconds" in payload and "countries" in payload:
        df = _countries_list_to_long(payload)

    elif isinstance(payload, dict) and "unix_seconds" in payload:
        ts = pd.to_datetime(payload["unix_seconds"], unit="s", utc=True)
        rows: List[Tuple[pd.Timestamp, str, float]] = []
        skip = {"unix_seconds", "timezone", "country", "unit", "status", "message", "countries", "deprecated", "sum"}
        for key, values in payload.items():
            if key in skip or not isinstance(values, list):
                continue
            if len(values) != len(ts):
                continue
            rows.extend((t, str(key), v) for t, v in zip(ts, values))
        if rows:
            df = pd.DataFrame(rows, columns=["timestamp_utc", "neighbor", "value_gw"])
        elif "countries" in payload:
            df = _countries_list_to_long(payload)
        else:
            pretty = json.dumps({k: type(v).__name__ for k, v in payload.items()}, indent=2)[:1200]
            raise ValueError(f"No neighbour arrays matched unix_seconds.\nKeys/types:\n{pretty}")

    elif isinstance(payload, dict) and "xAxisValues" in payload and "data" in payload:
        ts = pd.to_datetime(payload["xAxisValues"], utc=True, errors="coerce")
        rows = []
        for neighbor, values in (payload.get("data") or {}).items():
            if isinstance(values, list) and len(values) == len(ts):
                rows.extend((t, str(neighbor), v) for t, v in zip(ts, values))
        df = pd.DataFrame(rows, columns=["timestamp_utc", "neighbor", "value_gw"])

    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rows = []
        for item in payload:
            ts = item.get("timestamp") or item.get("time") or item.get("datetime")
            neighbor = item.get("country") or item.get("neighbor") or item.get("border") or item.get("code")
            val = item.get("value") or item.get("mw") or item.get("power")
            if ts is None or neighbor is None or val is None:
                continue
            rows.append((ts, str(neighbor), val))
        df = pd.DataFrame(rows, columns=["timestamp_utc", "neighbor", "value_gw"])
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")

    else:
        preview = json.dumps(payload if isinstance(payload, (dict, list)) else {"payload": payload}, indent=2)[:1200]
        raise ValueError(f"Unexpected response format from /cbet.\nPreview:\n{preview}")

    df["value_gw"] = pd.to_numeric(df["value_gw"], errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "value_gw"]).sort_values(["timestamp_utc", "neighbor"]).reset_index(drop=True)
    return df


def convert_utc_to_cet_mw(df_utc: pd.DataFrame) -> pd.DataFrame:
    df = df_utc.copy()
    df["timestamp_cet"] = df["timestamp_utc"].dt.tz_convert(TZ)
    df["value_mw"] = df["value_gw"] * 1000.0
    return df[["timestamp_cet", "neighbor", "value_mw"]]


def fetch_chunked_cet(session: requests.Session, start_cet: pd.Timestamp, end_cet: Optional[pd.Timestamp]) -> pd.DataFrame:
    end_cet_eff = end_cet if end_cet is not None else pd.Timestamp.now(tz=TZ).floor("h")
    frames: List[pd.DataFrame] = []

    cursor = start_cet
    while cursor <= end_cet_eff:
        chunk_end = (cursor + CHUNK_SIZE) - pd.Timedelta(hours=1)
        if chunk_end > end_cet_eff:
            chunk_end = end_cet_eff

        start_pad_utc = (cursor - FETCH_PADDING).tz_convert("UTC")
        end_pad_utc = (chunk_end + FETCH_PADDING).tz_convert("UTC")

        start_iso = start_pad_utc.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_pad_utc.strftime("%Y-%m-%dT%H:%M:%S")

        payload = fetch_cbet(session, COUNTRY, start_iso, end_iso)
        df_utc = melt_response(payload)
        df_cet = convert_utc_to_cet_mw(df_utc)

        # tighten to chunk window after conversion
        mask = (df_cet["timestamp_cet"] >= cursor) & (df_cet["timestamp_cet"] <= chunk_end)
        df_cet = df_cet.loc[mask]
        if not df_cet.empty:
            frames.append(df_cet)

        cursor = chunk_end + pd.Timedelta(hours=1)

    if frames:
        return pd.concat(frames, ignore_index=True)
    return pd.DataFrame(columns=["timestamp_cet", "neighbor", "value_mw"])


def to_hourly_wide(df_cet_long: pd.DataFrame) -> pd.DataFrame:
    # complete hourly grid over the available range, fill missing with 0
    if df_cet_long.empty:
        return pd.DataFrame(columns=["timestamp"])
    start = df_cet_long["timestamp_cet"].min().floor("h")
    end = df_cet_long["timestamp_cet"].max().floor("h")

    full_idx = pd.date_range(start=start, end=end, freq="h", tz=TZ, name="timestamp_cet")
    wide = (df_cet_long
            .pivot(index="timestamp_cet", columns="neighbor", values="value_mw")
            .reindex(full_idx)
            .fillna(0.0))
    if "sum" in wide.columns:
        wide = wide.drop(columns=["sum"])

    out = wide.reset_index()
    out["timestamp"] = out["timestamp_cet"].dt.tz_localize(None)
    out = out.drop(columns=["timestamp_cet"])
    # keep timestamp first
    cols = ["timestamp"] + [c for c in out.columns if c != "timestamp"]
    return out[cols]


def monthly_gwh(df_hourly_wide: pd.DataFrame) -> pd.DataFrame:
    df = df_hourly_wide.copy()
    df["month"] = pd.to_datetime(df["timestamp"]).dt.to_period("M")
    neigh_cols = [c for c in df.columns if c not in ("timestamp", "month")]

    # Drop Russia (case-insensitive exact match)
    neigh_cols = [c for c in neigh_cols if c.lower() != "russia"]
    df = df[["month"] + neigh_cols]

    # MW summed over hours gives MWh; divide by 1000 to GWh
    monthly = df.groupby("month")[neigh_cols].sum() / 1000.0
    monthly = monthly.dropna(how="all")

    # Order partners by absolute volume; force Belarus last if present
    partner_order = (monthly.abs().sum().sort_values(ascending=False)).index.tolist()
    if "Belarus" in partner_order:
        partner_order = [p for p in partner_order if p != "Belarus"] + ["Belarus"]

    return monthly[partner_order]


def build_plotly_stacked(monthly: pd.DataFrame) -> go.Figure:
    # Use a date axis so we can hide the 2022-03..2022-12 gap.
    monthly_plot = monthly.copy()
    monthly_plot.index = monthly_plot.index.to_timestamp()

    fig = go.Figure()
    color_map = {
        "Hungary": "#0097A7",
        "Slovakia": "#A9C4E3",
        "Poland": "#6EC1C7",
        "Romania": "#7FAEDD",
        "Moldova": "#A6C884",
        "Belarus": "#F2AA7E",
    }

    for partner in monthly_plot.columns:
        fig.add_trace(
            go.Bar(
                name=partner,
                x=monthly_plot.index,
                y=monthly_plot[partner],
                marker=dict(color=color_map.get(partner)),
                hovertemplate=f"<b>{partner}</b><br>%{{x|%b %Y}}<br>GWh: %{{y:.2f}}<extra></extra>",
            )
        )

    fig.update_layout(
        barmode="relative",
        xaxis_title="Month",
        yaxis_title="Monthly net imports (GWh)",
        legend_title="Trade partner",
        hovermode="closest",
        margin=dict(l=60, r=20, t=60, b=60),
    )

    # Optional: show fewer ticks (quarter starts) but keep last
    # (Plotly categorical axis: just set tickvals)
    tickvals = []
    for lab in monthly_plot.index:
        try:
            if lab.month in (1, 4, 7, 10):
                tickvals.append(lab)
        except Exception:
            pass
    if monthly_plot.index.size > 0:
        tickvals.append(monthly_plot.index[-1])

    # de-duplicate while preserving order
    seen = set()
    tickvals = [t for t in tickvals if not (t in seen or seen.add(t))]

    fig.update_xaxes(
        tickmode="array",
        tickvals=tickvals,
        tickangle=45,
        tickformat="%b %Y",
        rangebreaks=[dict(bounds=["2022-03-01", "2023-01-01"])],
    )

    return fig


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    session = build_session()

    # Mirrors your colleague’s spans:
    # (a) May 2021 – Feb 2022
    start_a = pd.Timestamp("2021-05-01 00:00", tz=TZ)
    end_a = pd.Timestamp("2022-02-28 23:00", tz=TZ)

    # (b) 2023 – latest available hour
    start_b = pd.Timestamp("2023-01-01 00:00", tz=TZ)
    end_b = None

    df_a = fetch_chunked_cet(session, start_a, end_a)
    df_b = fetch_chunked_cet(session, start_b, end_b)

    df_long = pd.concat([df_a, df_b], ignore_index=True)

    hourly_wide = to_hourly_wide(df_long)

    # Optional: save hourly wide for debugging / reproducibility
    hourly_wide.to_excel(OUT_XLSX_HOURLY_WIDE, index=False)

    monthly = monthly_gwh(hourly_wide)

    fig = build_plotly_stacked(monthly)
    fig.write_html(OUT_HTML, include_plotlyjs=True, full_html=True)

    print(f"Wrote interactive HTML → {OUT_HTML}")
    print(f"(Optional) wrote hourly wide Excel → {OUT_XLSX_HOURLY_WIDE}")


if __name__ == "__main__":
    main()
