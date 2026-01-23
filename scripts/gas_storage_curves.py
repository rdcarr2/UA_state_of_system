#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Interactive gas storage curves (UA 2024-2026 + 2026 proj 2).

- Uses mcm columns from UA_gas_storage_daily_2024_2026.csv
- Plots day-of-year on x-axis, storage filledness (mcm) on y-axis
"""

from __future__ import annotations

import csv
from pathlib import Path
import argparse
from typing import Dict, List, Optional, Tuple

import plotly.graph_objects as go


BASE_DIR = Path(__file__).resolve().parent.parent


SERIES = [
    ("ua_2024_mcm", "UA 2024", "2024", "#7FB3D5", "solid", "Observed"),
    ("ua_2025_mcm", "UA 2025", "2025", "#4A90C2", "solid", "Observed"),
    ("ua_2026_mcm", "UA 2026", "2026", "#1F4E79", "solid", "Observed"),
    ("ua_2026_proj_2_mcm", "Projection", "2026", "#D9534F", "dot", "Projected"),
]


def _parse_float(value: str) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_series(input_csv: Path) -> Tuple[List[int], Dict[str, List[Optional[float]]]]:
    with input_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        days: List[int] = []
        data: Dict[str, List[Optional[float]]] = {key: [] for key, *_ in SERIES}
        for row in reader:
            day_val = row.get("day")
            if not day_val:
                continue
            days.append(int(float(day_val)))
            for key, *_ in SERIES:
                data[key].append(_parse_float(row.get(key, "")))
    return days, data


def build_figure(days: List[int], data: Dict[str, List[Optional[float]]]) -> go.Figure:
    fig = go.Figure()
    for key, label, year_label, color, dash, status in SERIES:
        fig.add_trace(
            go.Scatter(
                name=label,
                x=days,
                y=data.get(key, []),
                mode="lines",
                line=dict(color=color, dash=dash, width=2.5),
                meta=[year_label, status],
                hovertemplate=(
                    "Year: %{meta[0]}<br>"
                    "Status: %{meta[1]}<br>"
                    "Day: %{x}<br>"
                    "Filledness: %{y:.0f} mcm<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        yaxis_title="Volume in storage (mcm)",
        legend_title="Series",
        hovermode="closest",
        margin=dict(l=60, r=20, t=60, b=60),
    )
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    month_starts = [1, 32, 61, 92, 122, 153, 183, 214, 245, 275, 306, 336]
    fig.update_xaxes(
        tickmode="array",
        tickvals=month_starts,
        ticktext=month_labels,
    )
    return fig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build gas storage curves")
    parser.add_argument("period", help="Data/output subfolder name, e.g. Jan_2026")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = BASE_DIR / "data" / args.period
    plots_dir = BASE_DIR / "plots" / args.period
    input_csv = data_dir / "UA_gas_storage_daily_2024_2026.csv"
    out_html = plots_dir / "ua_gas_storage_curves.html"

    if not input_csv.exists():
        raise FileNotFoundError(f"Missing input file: {input_csv}")

    days, data = load_series(input_csv)
    fig = build_figure(days, data)
    plots_dir.mkdir(parents=True, exist_ok=True)
    fig.write_html(out_html, include_plotlyjs=True, full_html=True)


if __name__ == "__main__":
    main()
