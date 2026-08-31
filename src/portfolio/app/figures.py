"""Figure builders.

One function per chart, each taking already-computed analytics frames. No
database access and no arithmetic beyond formatting, so the charts can be
exercised from a notebook or a test with hand-built frames.

Conventions applied throughout, from the shared chart spec:
  * 2px lines, 8px markers, hairline recessive grid
  * a 2px surface-coloured hairline between stacked bands — a gap, not a border
  * crosshair + unified tooltip on every line and area chart
  * a legend whenever there are two or more series; none for a single series,
    because the title already names it
  * selective direct labels only — never a value on every point
  * text in ink tokens, never in a series colour
  * no dual-axis charts, anywhere
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from .theme import (
    DIVERGING,
    FILL_GAP,
    LINE_WIDTH,
    MARKER_SIZE,
    SEVERITY_STATUS,
    STATUS,
    colour_map,
    template_name,
    tokens,
)

# Sector count exceeds the eight validated categorical slots, so the tail folds
# into "Other" rather than generating a ninth hue.
MAX_SECTOR_SLICES = 7
OTHER = "Other"

# Passed to every dcc.Graph. responsive=True matters: Plotly otherwise renders at a
# fixed 700px width, so on a narrower viewport the plot is clipped and the card
# grows a nested horizontal scrollbar to reach its own axis.
GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}

# Container heights, consumed by components.graph(). Figures themselves autosize:
# in Plotly's responsive mode the container is authoritative, so setting height in
# both places invites them to disagree.
H_STANDARD = 380
H_TALL = 460


def _month_axis(dates, t: dict) -> dict:
    """X-axis ticked at exactly the month-ends present in the data.

    Plotly's automatic ticking picks round intervals from its own range padding,
    which on a twelve-month series produced a phantom "Jan 2026" tick beyond the
    last observation. Naming the ticks explicitly means the axis can only show
    dates the extract actually contains.
    """
    return dict(
        tickmode="array",
        tickvals=list(dates),
        tickformat="%b %Y",
        showspikes=True,
        spikemode="across",
        spikethickness=1,
        spikecolor=t["axis"],
        spikedash="solid",
        automargin=True,
        title=None,
    )


def _m(v: float) -> str:
    return f"${v / 1e6:,.1f}M"


def _truncate(text: str, limit: int = 46) -> str:
    """Shorten on a word boundary. A hard character cut leaves fragments like
    "for security and m", which reads as a rendering bug rather than a summary."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def sector_order(mix: pd.DataFrame, dimension: str = "sector") -> list[str]:
    """Deterministic series order, largest by final-month value first.

    Ranked once against the last month and then frozen, so the ordering — and
    therefore each sector's colour — does not change as the user moves through
    time. Anything past the slot limit folds into "Other", which always sorts
    last and takes the final slot.
    """
    last = mix["as_of_date"].max()
    ranked = (
        mix[mix["as_of_date"] == last]
        .sort_values("market_value", ascending=False)[dimension]
        .tolist()
    )
    if len(ranked) <= MAX_SECTOR_SLICES + 1:
        return ranked
    return ranked[:MAX_SECTOR_SLICES] + [OTHER]


def fold_to_other(
    mix: pd.DataFrame, order: list[str], dimension: str = "sector"
) -> pd.DataFrame:
    """Collapse everything outside `order` into a single "Other" series."""
    keep = set(order) - {OTHER}
    out = mix.copy()
    if OTHER in order:
        out[dimension] = out[dimension].where(out[dimension].isin(keep), OTHER)
        out = (
            out.groupby(["as_of_date", dimension], as_index=False)[
                ["market_value", "weight_pct"]
            ]
            .sum()
        )
    return out


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def market_value_over_time(overview: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Total portfolio market value by month-end.

    A single series, so no legend: the title names it. The final point is
    direct-labelled — selectively, not every point.
    """
    t = tokens(mode)
    colour = colour_map(["mv"], mode)["mv"]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=overview["as_of_date"],
            y=overview["market_value"],
            mode="lines+markers",
            name="Market value",
            line=dict(width=LINE_WIDTH, color=colour),
            marker=dict(size=MARKER_SIZE, color=colour),
            hovertemplate="%{x|%b %Y}<br>%{customdata}<extra></extra>",
            customdata=[_m(v) for v in overview["market_value"]],
        )
    )
    last = overview.iloc[-1]
    fig.add_annotation(
        x=last["as_of_date"],
        y=last["market_value"],
        text=_m(last["market_value"]),
        showarrow=False,
        xshift=-6,
        yshift=18,
        font=dict(color=t["text_primary"], size=12),
    )
    fig.update_layout(
        template=template_name(mode),
        title="Total portfolio market value",
        showlegend=False,
        hovermode="x unified",
        autosize=True,
        # No y-axis title: the chart title already names the measure, and the $
        # tick prefix carries the unit.
        yaxis=dict(title=None, tickprefix="$", showspikes=False),
        xaxis=_month_axis(overview["as_of_date"], t),
    )
    return fig


def mv_change_bars(overview: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Month-over-month change.

    Polarity, so the diverging pair: cool for a gain, warm for a loss. Not the
    status palette — a market loss is not an error state, and status colours are
    reserved.
    """
    t = tokens(mode)
    d = DIVERGING[mode]
    df = overview[overview["mv_change"].notna()]
    colours = [d["positive"] if v >= 0 else d["negative"] for v in df["mv_change"]]
    fig = go.Figure(
        go.Bar(
            x=df["as_of_date"],
            y=df["mv_change"],
            marker=dict(
                color=colours,
                # Surface-coloured hairline: the 2px gap between adjacent bars,
                # not an outline drawn to separate them.
                line=dict(color=t["surface"], width=FILL_GAP),
            ),
            hovertemplate="%{x|%b %Y}<br>%{customdata}<extra></extra>",
            customdata=[f"{'+' if v >= 0 else ''}{_m(v)}" for v in df["mv_change"]],
            name="Change",
        )
    )
    fig.update_layout(
        template=template_name(mode),
        title="Month-over-month change in market value",
        showlegend=False,
        autosize=True,
        yaxis=dict(title=None, tickprefix="$"),
        xaxis=_month_axis(df["as_of_date"], t),
        bargap=0.35,
    )
    return fig


def attribution_bars(attribution: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Monthly change split into market and trading effects.

    Two series, so a legend is present and both are direct-labelled by it.
    Grouped rather than stacked: the two effects frequently have opposite signs,
    and a stack of opposing signed values is unreadable.
    """
    cmap = colour_map(["Market", "Trading"], mode)
    t = tokens(mode)
    fig = go.Figure()
    for name, col in (("Market", "market_effect"), ("Trading", "trading_effect")):
        fig.add_trace(
            go.Bar(
                x=attribution["as_of_date"],
                y=attribution[col],
                name=name,
                marker=dict(
                    color=cmap[name], line=dict(color=t["surface"], width=FILL_GAP)
                ),
                hovertemplate=f"%{{x|%b %Y}}<br>{name}: %{{customdata}}<extra></extra>",
                customdata=[
                    f"{'+' if v >= 0 else ''}{_m(v)}" for v in attribution[col]
                ],
            )
        )
    fig.update_layout(
        template=template_name(mode),
        title="What drove each month: market moves versus trading",
        barmode="group",
        bargap=0.3,
        bargroupgap=0.08,
        autosize=True,
        yaxis=dict(title=None, tickprefix="$"),
        xaxis=_month_axis(attribution["as_of_date"], t),
    )
    return fig


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def allocation_area(
    mix: pd.DataFrame,
    dimension: str = "sector",
    mode: str = "light",
    as_weight: bool = True,
) -> go.Figure:
    """Allocation mix over time as a stacked area.

    Each band carries a surface-coloured 2px line, which renders as a gap between
    segments rather than an outline around them.
    """
    t = tokens(mode)
    order = sector_order(mix, dimension)
    folded = fold_to_other(mix, order, dimension)
    cmap = colour_map(order, mode)
    value_col = "weight_pct" if as_weight else "market_value"

    fig = go.Figure()
    # Reversed so the largest series sits at the bottom of the stack, which keeps
    # the most-read band against the axis.
    for key in reversed(order):
        s = folded[folded[dimension] == key].sort_values("as_of_date")
        if s.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=s["as_of_date"],
                y=s[value_col],
                name=key,
                mode="lines",
                stackgroup="one",
                line=dict(width=FILL_GAP, color=t["surface"]),
                fillcolor=cmap[key],
                hovertemplate=(
                    f"{key}: %{{y:.1f}}%<extra></extra>"
                    if as_weight
                    else f"{key}: %{{customdata}}<extra></extra>"
                ),
                customdata=None if as_weight else [_m(v) for v in s[value_col]],
            )
        )
    fig.update_layout(
        template=template_name(mode),
        title=f"{dimension.title()} allocation over time"
        + (" (% of market value)" if as_weight else " (market value)"),
        hovermode="x unified",
        autosize=True,
        yaxis=dict(
            title=None,
            ticksuffix="%" if as_weight else "",
            tickprefix="" if as_weight else "$",
            range=[0, 100] if as_weight else None,
        ),
        xaxis=_month_axis(sorted(folded["as_of_date"].unique()), t),
    )
    return fig


def shift_bars(shift: pd.DataFrame, dimension: str = "sector", mode: str = "light") -> go.Figure:
    """Largest allocation shifts, with the driver behind each.

    Two series because the question is not just how much moved but why: a bar for
    the market effect and one for the trading effect, side by side.
    """
    t = tokens(mode)
    cmap = colour_map(["Market", "Trading"], mode)
    df = shift.sort_values("weight_change_pct")
    fig = go.Figure()
    for name, col in (("Market", "market_effect"), ("Trading", "trading_effect")):
        fig.add_trace(
            go.Bar(
                y=df[dimension],
                x=df[col],
                name=name,
                orientation="h",
                marker=dict(color=cmap[name], line=dict(color=t["surface"], width=FILL_GAP)),
                hovertemplate=f"%{{y}}<br>{name}: %{{customdata}}<extra></extra>",
                customdata=[f"{'+' if v >= 0 else ''}{_m(v)}" for v in df[col]],
            )
        )
    fig.update_layout(
        template=template_name(mode),
        title="Largest allocation shifts, and what drove them",
        barmode="group",
        xaxis=dict(title="Effect on market value", tickprefix="$"),
        yaxis=dict(title=None, automargin=True),
        autosize=True,
        margin=dict(l=8, r=24, t=56, b=56),
    )
    return fig


# ---------------------------------------------------------------------------
# Security drill-down — three separate figures, never one with three y-scales
# ---------------------------------------------------------------------------


def security_position(history: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Par and market value are both in dollars, so they legitimately share one axis."""
    t = tokens(mode)
    cmap = colour_map(["Par", "Market value"], mode)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=history["as_of_date"], y=history["par_amount"], name="Par",
            mode="lines+markers", line=dict(width=LINE_WIDTH, color=cmap["Par"], shape="hv"),
            marker=dict(size=MARKER_SIZE, color=cmap["Par"]),
            hovertemplate="Par: %{customdata}<extra></extra>",
            customdata=[_m(v) for v in history["par_amount"]],
        )
    )
    fig.add_trace(
        go.Scatter(
            x=history["as_of_date"], y=history["market_value"], name="Market value",
            mode="lines+markers",
            line=dict(width=LINE_WIDTH, color=cmap["Market value"]),
            marker=dict(size=MARKER_SIZE, color=cmap["Market value"]),
            hovertemplate="Market value: %{customdata}<extra></extra>",
            customdata=[_m(v) if pd.notna(v) else "n/a" for v in history["market_value"]],
        )
    )
    fig.update_layout(
        template=template_name(mode),
        title="Position history",
        hovermode="x unified",
        autosize=True,
        yaxis=dict(title=None, tickprefix="$"),
        xaxis=_month_axis(history["as_of_date"], t),
    )
    return fig


def security_price(history: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Clean price. A repaired point is ringed rather than shown as delivered."""
    t = tokens(mode)
    colour = colour_map(["price"], mode)["price"]
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=history["as_of_date"], y=history["price"], name="Clean price",
            mode="lines+markers",
            line=dict(width=LINE_WIDTH, color=colour),
            marker=dict(size=MARKER_SIZE, color=colour),
            hovertemplate="Price: %{y:.4f}<extra></extra>",
        )
    )
    # Mark observations whose price came from the market value rather than a
    # delivered mark, so an inferred point is never read as an observed one.
    if "price_source" in history.columns:
        inferred = history[history["price_source"] == "implied"]
        if not inferred.empty:
            fig.add_trace(
                go.Scatter(
                    x=inferred["as_of_date"], y=inferred["price"],
                    name="Implied from market value", mode="markers",
                    marker=dict(
                        size=MARKER_SIZE + 6, color="rgba(0,0,0,0)",
                        line=dict(color=t["text_secondary"], width=FILL_GAP),
                    ),
                    hovertemplate="Implied price: %{y:.4f}<extra></extra>",
                )
            )
    fig.update_layout(
        template=template_name(mode),
        title="Clean price",
        hovermode="x unified",
        # bool() is load-bearing: pandas .any() returns numpy.bool_, which Plotly
        # rejects outright rather than coercing.
        showlegend=bool(
            "price_source" in history.columns
            and (history["price_source"] == "implied").any()
        ),
        autosize=True,
        yaxis=dict(title="Price (per 100 par)"),
        xaxis=_month_axis(history["as_of_date"], t),
    )
    return fig


def security_oas(history: pd.DataFrame, mode: str = "light") -> go.Figure:
    """OAS, in its own figure.

    Deliberately NOT plotted against price on a second y-axis. Two scales on one
    plot make their alignment arbitrary and manufacture a correlation that is not
    in the data; two stacked charts sharing an x-range convey the same
    relationship honestly.
    """
    t = tokens(mode)
    colour = colour_map(["a", "b", "oas"], mode)["oas"]
    fig = go.Figure(
        go.Scatter(
            x=history["as_of_date"], y=history["oas_bps"], name="OAS",
            mode="lines+markers",
            line=dict(width=LINE_WIDTH, color=colour),
            marker=dict(size=MARKER_SIZE, color=colour),
            hovertemplate="OAS: %{y:.1f}bp<extra></extra>",
            connectgaps=False,
        )
    )
    fig.update_layout(
        template=template_name(mode),
        title="Option-adjusted spread",
        hovermode="x unified",
        showlegend=False,
        autosize=True,
        yaxis=dict(title=None, ticksuffix="bp"),
        xaxis=_month_axis(history["as_of_date"], t),
    )
    return fig


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def findings_by_rule(summary: pd.DataFrame, mode: str = "light") -> go.Figure:
    """Findings per rule, coloured by severity.

    Severity is a state, so it uses the reserved status palette — and every bar
    carries its severity in the legend and in the hover text, so the colour never
    carries the meaning alone.
    """
    t = tokens(mode)
    df = summary.sort_values("count")
    fig = go.Figure()
    for severity in ("ERROR", "WARNING", "INFO"):
        s = df[df["severity"] == severity]
        if s.empty:
            continue
        fig.add_trace(
            go.Bar(
                y=s["rule_code"] + "  " + s["rule_title"].map(_truncate),
                x=s["count"],
                name=f"{severity.title()}",
                orientation="h",
                marker=dict(
                    color=STATUS[SEVERITY_STATUS[severity]],
                    line=dict(color=t["surface"], width=FILL_GAP),
                ),
                hovertemplate="%{y}<br>" + f"{severity}: " + "%{x} finding(s)<extra></extra>",
            )
        )
    fig.update_layout(
        template=template_name(mode),
        title="Data-quality findings by rule",
        barmode="stack",
        # Severity order, not Plotly's default reversal for stacked bars.
        legend=dict(traceorder="normal"),
        xaxis=dict(title="Findings", dtick=5),
        yaxis=dict(title=None, automargin=True),
        margin=dict(l=8, r=24, t=56, b=56),
        autosize=True,
    )
    return fig
