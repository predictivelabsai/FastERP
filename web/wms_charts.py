"""FastBI-style Plotly charts and result tables for warehouse reports."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from fasthtml.common import Div, H3, NotStr, P, Script, Table, Tbody, Td, Th, Thead, Tr


PALETTE = ["#d97706", "#2563eb", "#16a34a", "#7c3aed", "#0891b2"]


def _value(value):
    if isinstance(value, (Decimal, int, float)):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def plotly(div_id: str, columns: list[str], rows: list[list], *,
           chart_type: str = "bar", x_col: str | None = None,
           y_col: str | None = None, height: int = 300):
    if not columns or not rows or len(columns) < 2:
        return Div(P("No chart data yet.", cls="sub"))
    x_index = columns.index(x_col) if x_col in columns else 0
    y_index = columns.index(y_col) if y_col in columns else 1
    x_values = [_value(row[x_index]) for row in rows]
    y_values = [_value(row[y_index]) for row in rows]
    if not all(isinstance(value, (int, float)) for value in y_values):
        return Div(P("Choose a numeric measure to chart.", cls="sub"))
    if chart_type == "line":
        traces = [{"type": "scatter", "mode": "lines+markers", "x": x_values,
                   "y": y_values, "line": {"color": PALETTE[0], "width": 2}}]
    elif chart_type == "pie":
        traces = [{"type": "pie", "labels": x_values, "values": y_values,
                   "hole": 0.4, "marker": {"colors": PALETTE}}]
    else:
        traces = [{"type": "bar", "x": x_values, "y": y_values,
                   "marker": {"color": PALETTE[0]}}]
    layout = {
        "height": height, "margin": {"t": 15, "r": 15, "b": 55, "l": 55},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"size": 11, "color": "#5b5246"},
        "xaxis": {"automargin": True}, "yaxis": {"automargin": True},
        "showlegend": chart_type == "pie",
    }
    figure = json.dumps({"data": traces, "layout": layout}).replace("<", "\\u003c")
    return (
        Div(id=div_id, cls="plot"),
        Script(NotStr(
            f"(function(){{var f={figure};if(window.Plotly)"
            f"Plotly.newPlot({json.dumps(div_id)},f.data,f.layout,"
            "{displayModeBar:false,responsive:true});})();"
        )),
    )


def result_table(columns: list[str], rows: list[list], *, max_rows: int = 100):
    if not columns:
        return P("No columns.")
    return Div(
        Table(
            Thead(Tr(*[Th(column) for column in columns])),
            Tbody(*[
                Tr(*[Td("—" if value is None else str(value)) for value in row])
                for row in rows[:max_rows]
            ] or [Tr(Td("No matching rows", colspan=len(columns)))]),
            cls="tbl",
        ),
        P(f"Showing {min(len(rows), max_rows)} rows", cls="sub"),
    )


def chart_card(title: str, div_id: str, columns: list[str], rows: list[list],
               *, chart_type: str = "bar", x_col: str | None = None,
               y_col: str | None = None):
    block = plotly(div_id, columns, rows, chart_type=chart_type,
                   x_col=x_col, y_col=y_col)
    if not isinstance(block, tuple):
        block = (block,)
    return Div(Div(H3(title), cls="card-header"), *block, cls="card")
