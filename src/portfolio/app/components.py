"""Shared UI pieces: stat tiles, tables, cards, and the table-view twin.

Two rules from the chart spec drive most of what is here:

  * Every chart has a table-view twin. A chart that encodes anything in colour
    needs a WCAG-clean equivalent, and three of the light-mode categorical slots
    sit below 3:1 contrast, so the table is not optional — it is the relief that
    makes those slots legal.

  * A stat tile is a legitimate chart form. When the story is one number, a
    one-bar bar chart is the wrong answer.
"""

from __future__ import annotations

import pandas as pd
from dash import dash_table, dcc, html

from .theme import FONT_STACK, LIGHT, SEVERITY_ICON, SEVERITY_STATUS, STATUS

# Plotly's responsive mode sizes the plot to its container rather than to
# layout.height, so the container is what must carry a height. Without it the
# graph collapses to zero and the card renders empty; with a fixed width instead,
# the plot is clipped on a narrow viewport and the card grows a nested scrollbar.
# Height here includes the x-axis band, so tick labels stay inside the card.
GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}


def graph(figure, height: int = 380):
    return dcc.Graph(
        figure=figure,
        config=GRAPH_CONFIG,
        style={"height": f"{height}px", "width": "100%"},
    )


def money(v: float | None, unit: str = "M") -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v / 1e6:,.1f}M" if unit == "M" else f"${v:,.0f}"


def signed_money(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{'+' if v >= 0 else '−'}${abs(v) / 1e6:,.1f}M"


def pct(v: float | None, dp: int = 1) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.{dp}f}%"


def stat_tile(label: str, value: str, note: str | None = None, tone: str | None = None):
    """One number, told properly.

    The value uses proportional figures, not tabular-nums: equal-width digits make
    a large standalone number look loose. Tone, when given, is a status colour and
    always arrives with the accompanying text, never as colour alone.
    """
    colour = STATUS[tone] if tone else LIGHT["text_primary"]
    children = [
        html.Div(label, className="tile-label"),
        html.Div(value, className="tile-value", style={"color": colour}),
    ]
    if note:
        children.append(html.Div(note, className="tile-note"))
    return html.Div(children, className="tile")


def severity_tile(severity: str, count: int):
    """Severity counts, with an icon and a label so colour is never the only cue."""
    tone = SEVERITY_STATUS[severity]
    return stat_tile(
        label=f"{SEVERITY_ICON[severity]}  {severity.title()}",
        value=f"{count:,}",
        note="findings",
        tone=tone,
    )


def card(*children, title: str | None = None, wide: bool = False):
    inner = list(children)
    if title:
        inner.insert(0, html.H3(title, className="card-title"))
    return html.Div(inner, className="card card-wide" if wide else "card")


def data_table(
    df: pd.DataFrame,
    columns: list[dict] | None = None,
    page_size: int = 15,
    table_id: str | None = None,
):
    """A readable table.

    tabular-nums here, unlike the stat tiles: these digits must align vertically.
    """
    cols = columns or [{"name": c.replace("_", " ").title(), "id": c} for c in df.columns]
    kwargs = {}
    if table_id:
        kwargs["id"] = table_id
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=cols,
        page_size=page_size,
        sort_action="native",
        filter_action="native",
        style_table={"overflowX": "auto"},
        style_cell={
            "fontFamily": FONT_STACK,
            "fontSize": "13px",
            "padding": "8px 12px",
            "textAlign": "left",
            "border": "none",
            "borderBottom": f"1px solid {LIGHT['grid']}",
            "color": LIGHT["text_secondary"],
            "fontVariantNumeric": "tabular-nums",
            "maxWidth": "420px",
            "whiteSpace": "normal",
            "height": "auto",
        },
        style_header={
            "fontWeight": "600",
            "color": LIGHT["text_primary"],
            "backgroundColor": LIGHT["plane"],
            "border": "none",
            "borderBottom": f"1px solid {LIGHT['axis']}",
        },
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": LIGHT["plane"]},
        ],
        **kwargs,
    )


def table_view(df: pd.DataFrame, label: str = "Table view", page_size: int = 12):
    """The collapsible WCAG-clean twin that sits beneath a chart."""
    return html.Details(
        [html.Summary(label, className="table-summary"), data_table(df, page_size=page_size)],
        className="table-view",
    )


def note(*children):
    return html.P(list(children), className="note")


def empty_state(message: str):
    return html.Div(
        [
            html.H2("No data loaded", className="empty-title"),
            html.P(message, className="empty-body"),
            html.Pre("python -m portfolio.load.loader", className="empty-code"),
        ],
        className="empty",
    )
