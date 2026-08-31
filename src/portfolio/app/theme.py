"""Chart palette, Plotly templates, and the shared figure conventions.

Every colour here was checked with a palette validator rather than chosen by eye,
in both light and dark modes:

  Categorical (8 slots, adjacent pairs — stacks, bars, lines)
    light: lightness band PASS, chroma floor PASS, worst adjacent CVD dE 9.1,
           worst adjacent normal-vision dE 19.6
    dark:  all of the above PASS, plus all 8 clear 3:1 on the dark surface

  Three light slots (aqua, yellow, magenta) sit below 3:1 contrast on the light
  surface. That is a known WARN and it obliges relief: every chart using them
  ships a legend and a table view, so identity is never carried by colour alone.

Rules this module enforces, and why they are not negotiable:

  * Categorical hues are assigned in fixed order and never cycled. A ninth series
    would be indistinguishable from an existing slot under colour-vision
    deficiency, so the tail folds into "Other" instead (see figures.sector_order).

  * Colour follows the entity, not its rank. SECTOR_COLOURS is keyed by sector
    name, so filtering the portfolio never repaints the survivors — a reader who
    learned "Energy is orange" stays right.

  * No dual-axis charts anywhere. Price and OAS are separate figures rather than
    two y-scales on one plot: the alignment of two scales is arbitrary and
    invents a correlation the data does not contain.

  * Ratings use categorical hues ordered by credit quality in the stack rather
    than a light-to-dark ramp. Ratings are genuinely ordinal, so a ramp would be
    legitimate, but the light-mode ordinal ramp only clears its step-separation
    floor at five steps and the data has seven rating buckets. Merging BB with B
    to fit a palette would be a design constraint driving a domain decision.
    Stacking order carries the ordinality instead.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

# ---------------------------------------------------------------------------
# Surfaces and ink
# ---------------------------------------------------------------------------

LIGHT = {
    "surface": "#fcfcfb",
    "plane": "#f9f9f7",
    "text_primary": "#0b0b0b",
    "text_secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "border": "rgba(11,11,11,0.10)",
}

DARK = {
    "surface": "#1a1a19",
    "plane": "#0d0d0d",
    "text_primary": "#ffffff",
    "text_secondary": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "border": "rgba(255,255,255,0.10)",
}

# Categorical slots, in validated order. Never reorder without re-validating:
# the ordering *is* the colour-vision-deficiency safety mechanism.
CATEGORICAL_LIGHT = [
    "#2a78d6",  # 1 blue
    "#eb6834",  # 2 orange
    "#1baf7a",  # 3 aqua
    "#eda100",  # 4 yellow
    "#e87ba4",  # 5 magenta
    "#008300",  # 6 green
    "#4a3aa7",  # 7 violet
    "#e34948",  # 8 red
]

CATEGORICAL_DARK = [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
]

# Diverging pair for polarity (month-over-month gain versus loss). Warm/cool so
# the poles read as opposite, with a neutral grey midpoint that reads as nothing.
DIVERGING = {
    "light": {"positive": "#2a78d6", "negative": "#d03b3b", "mid": "#f0efec"},
    "dark": {"positive": "#3987e5", "negative": "#e66767", "mid": "#383835"},
}

# Status palette — reserved. Never reused for a data series, and always paired
# with an icon and a label so state is never carried by colour alone.
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

SEVERITY_STATUS = {"ERROR": "critical", "WARNING": "warning", "INFO": "good"}
SEVERITY_ICON = {"ERROR": "●", "WARNING": "▲", "INFO": "■"}

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Mark geometry, per the shared spec.
LINE_WIDTH = 2
MARKER_SIZE = 8
FILL_GAP = 2  # surface-coloured hairline between stacked bands, not a border


def tokens(mode: str = "light") -> dict:
    return LIGHT if mode == "light" else DARK


def categorical(mode: str = "light") -> list[str]:
    return CATEGORICAL_LIGHT if mode == "light" else CATEGORICAL_DARK


def colour_map(keys: list[str], mode: str = "light") -> dict[str, str]:
    """Assign hues in fixed order, keyed by entity name.

    Keyed by name rather than position so a filter that changes the series count
    cannot repaint the survivors. Callers must pass a stable, deterministic key
    order — see figures.sector_order.
    """
    slots = categorical(mode)
    if len(keys) > len(slots):
        raise ValueError(
            f"{len(keys)} series exceeds the {len(slots)} validated categorical "
            "slots. Fold the tail into 'Other' or facet into small multiples; "
            "generating a ninth hue would produce a colour indistinguishable "
            "from an existing slot under CVD."
        )
    return {k: slots[i] for i, k in enumerate(keys)}


# ---------------------------------------------------------------------------
# Plotly templates
# ---------------------------------------------------------------------------


def _template(mode: str) -> go.layout.Template:
    t = tokens(mode)
    return go.layout.Template(
        layout=go.Layout(
            paper_bgcolor=t["surface"],
            plot_bgcolor=t["surface"],
            font=dict(family=FONT_STACK, size=13, color=t["text_secondary"]),
            title=dict(
                font=dict(size=15, color=t["text_primary"]),
                x=0,
                xanchor="left",
                pad=dict(b=12),
            ),
            # Recessive chrome: solid hairlines one shade off the surface. Never
            # dashed — dashing reads as "projection" or "threshold" when it is
            # only a grid.
            xaxis=dict(
                gridcolor=t["grid"],
                griddash="solid",
                linecolor=t["axis"],
                zerolinecolor=t["axis"],
                tickfont=dict(color=t["muted"], size=12),
                title=dict(font=dict(color=t["text_secondary"], size=12)),
                showgrid=False,
            ),
            yaxis=dict(
                gridcolor=t["grid"],
                griddash="solid",
                linecolor=t["axis"],
                zerolinecolor=t["axis"],
                tickfont=dict(color=t["muted"], size=12),
                title=dict(font=dict(color=t["text_secondary"], size=12)),
                showgrid=True,
                zeroline=True,
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=t["text_secondary"], size=12),
                bgcolor="rgba(0,0,0,0)",
            ),
            # Generous padding. The bottom margin includes the x-axis band so the
            # card never grows a nested scrollbar to reach its own tick labels.
            # The right margin is wide enough for the final x tick label: at 24px
            # Plotly silently dropped the last month of a twelve-month axis, so the
            # series appeared to end a month early.
            margin=dict(l=64, r=56, t=56, b=56),
            hoverlabel=dict(
                bgcolor=t["surface"],
                bordercolor=t["axis"],
                font=dict(family=FONT_STACK, size=12, color=t["text_primary"]),
            ),
            colorway=categorical(mode),
        )
    )


pio.templates["portfolio_light"] = _template("light")
pio.templates["portfolio_dark"] = _template("dark")


def template_name(mode: str = "light") -> str:
    return f"portfolio_{mode}"
