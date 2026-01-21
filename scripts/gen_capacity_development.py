#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stacked bar chart of generation capacity development from Gen_capacity_development.xlsx.

- Each sheet is one bar (time snapshot).
- Each bar sums the following rows: Operating, Attacked Damaged, Destroyed, Occupied.
- Outputs HTML to plots/ and an optional CSV to data/ for convenience.
"""

from __future__ import annotations

import csv
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import plotly.graph_objects as go


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PLOTS_DIR = BASE_DIR / "plots"

INPUT_XLSX = DATA_DIR / "Gen_capacity_development.xlsx"
OUT_HTML = PLOTS_DIR / "gen_capacity_development.html"
OUT_CSV = DATA_DIR / "gen_capacity_development_summary.csv"

CATEGORIES = ["Operating", "Attacked Damaged", "Destroyed", "Occupied"]


@dataclass
class SheetInfo:
    name: str
    path: str


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        xml_bytes = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml_bytes)
    ns = {"ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out: List[str] = []
    for si in root.findall("ns:si", ns):
        texts = [t.text or "" for t in si.findall(".//ns:t", ns)]
        out.append("".join(texts))
    return out


def _sheet_map(zf: zipfile.ZipFile) -> List[SheetInfo]:
    wb_xml = zf.read("xl/workbook.xml")
    wb_root = ET.fromstring(wb_xml)
    ns = {
        "ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    rels_xml = zf.read("xl/_rels/workbook.xml.rels")
    rels_root = ET.fromstring(rels_xml)
    rels_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    rels = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall("r:Relationship", rels_ns)
    }

    sheets = []
    for sh in wb_root.findall("ns:sheets/ns:sheet", ns):
        name = sh.attrib["name"]
        rel_id = sh.attrib.get(f"{{{ns['r']}}}id")
        target = rels.get(rel_id, "")
        if target and not target.startswith("xl/"):
            target = f"xl/{target}"
        sheets.append(SheetInfo(name=name, path=target))
    return sheets


def _cell_value(cell: ET.Element, shared: List[str]) -> Optional[str]:
    ns = {"ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.attrib.get("t")
    v = cell.find("ns:v", ns)
    if cell_type == "inlineStr":
        t = cell.find(".//ns:t", ns)
        return t.text if t is not None else None
    if v is None:
        return None
    if cell_type == "s":
        return shared[int(v.text)] if shared else v.text
    return v.text


def _col_letters(cell_ref: str) -> str:
    out = []
    for ch in cell_ref:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out)


def _parse_sheet(zf: zipfile.ZipFile, sheet_path: str, shared: List[str]) -> Dict[str, float]:
    xml_bytes = zf.read(sheet_path)
    root = ET.fromstring(xml_bytes)
    ns = {"ns": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    values: Dict[str, float] = {}
    for row in root.findall(".//ns:sheetData/ns:row", ns):
        row_label = None
        numeric_vals: List[float] = []
        for cell in row.findall("ns:c", ns):
            ref = cell.attrib.get("r", "")
            col = _col_letters(ref)
            raw = _cell_value(cell, shared)
            if col == "A":
                row_label = raw.strip() if isinstance(raw, str) else raw
                continue
            if raw is None:
                continue
            try:
                numeric_vals.append(float(raw))
            except (TypeError, ValueError):
                continue
        if row_label in CATEGORIES:
            values[row_label] = sum(numeric_vals)

    # Ensure all categories exist
    for cat in CATEGORIES:
        values.setdefault(cat, 0.0)
    return values


def build_figure(sheet_names: List[str], data_by_sheet: List[Dict[str, float]]) -> go.Figure:
    fig = go.Figure()
    color_map = {
        "Operating": "#6CCB8B",
        "Attacked Damaged": "#F6B26B",
        "Destroyed": "#E57373",
        "Occupied": "#555555",
    }
    for category in CATEGORIES:
        values_gw = [d.get(category, 0.0) / 1000.0 for d in data_by_sheet]
        rounded_gw = [round(v * 2) / 2 for v in values_gw]
        fig.add_trace(
            go.Bar(
                name=category,
                x=sheet_names,
                y=values_gw,
                customdata=rounded_gw,
                marker=dict(color=color_map.get(category)),
                hovertemplate=f"<b>{category}</b><br>%{{x}}<br>Capacity: ~%{{customdata:.1f}} GW<extra></extra>",
            )
        )

    fig.update_layout(
        barmode="stack",
        xaxis_title="Assessment period",
        yaxis_title="Installed capacity (GW)",
        legend_title="Status",
        hovermode="closest",
        margin=dict(l=60, r=20, t=60, b=60),
    )
    fig.update_xaxes(tickangle=45)
    return fig


def write_summary_csv(sheet_names: List[str], data_by_sheet: List[Dict[str, float]]) -> None:
    rows = []
    for name, data in zip(sheet_names, data_by_sheet):
        row = {"Sheet": name}
        row.update({cat: data.get(cat, 0.0) for cat in CATEGORIES})
        rows.append(row)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Sheet"] + CATEGORIES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if not INPUT_XLSX.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_XLSX}")

    with zipfile.ZipFile(INPUT_XLSX) as zf:
        shared = _load_shared_strings(zf)
        sheets = _sheet_map(zf)

        sheet_names: List[str] = []
        data_by_sheet: List[Dict[str, float]] = []
        for sh in sheets:
            if not sh.path:
                continue
            sheet_names.append(sh.name)
            data_by_sheet.append(_parse_sheet(zf, sh.path, shared))

    fig = build_figure(sheet_names, data_by_sheet)
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    fig.write_html(OUT_HTML, include_plotlyjs=True, full_html=True)
    write_summary_csv(sheet_names, data_by_sheet)


if __name__ == "__main__":
    main()
