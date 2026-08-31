"""Security drill-down: position history, price, and OAS for any one security.

Required feature 3.

Price and OAS get their own figures rather than sharing a plot with two y-scales.
Two scales make their alignment arbitrary and invent a correlation the data does
not contain; two stacked charts over the same x-range show the same relationship
without the distortion.
"""

from __future__ import annotations

import pandas as pd
from dash import dcc, html

from .. import figures
from ..components import graph, card, money, note, pct, stat_tile, table_view
from ..data import Snapshot, security_history


def picker(snap: Snapshot):
    securities = snap.securities
    options = [
        {
            "label": f"{r.description}  ·  {r.sector} · {r.rating}",
            "value": r.security_id,
        }
        for r in securities.itertuples()
    ]
    default = options[0]["value"] if options else None
    # One filter row above everything it scopes, never inside a chart card.
    return html.Div(
        [
            html.Label("Security", htmlFor="security-picker", className="filter-label"),
            dcc.Dropdown(
                id="security-picker",
                options=options,
                value=default,
                clearable=False,
                className="filter-control",
            ),
        ],
        className="filter-row",
    )


def detail(snap: Snapshot, security_id: str, mode: str = "light") -> html.Div:
    if not security_id or snap.positions.empty:
        return html.Div(note("Select a security."))

    hist = security_history(snap, security_id)
    meta = snap.positions[snap.positions["security_id"] == security_id].iloc[0]

    first_price = hist["price"].dropna()
    price_return = (
        (first_price.iloc[-1] / first_price.iloc[0] - 1.0) * 100.0
        if len(first_price) >= 2
        else None
    )
    oas = hist["oas_bps"].dropna()
    oas_change = oas.iloc[-1] - oas.iloc[0] if len(oas) >= 2 else None

    tiles = html.Div(
        [
            stat_tile("Latest par", money(hist["par_amount"].iloc[-1])),
            stat_tile("Latest market value", money(hist["market_value"].iloc[-1])),
            stat_tile(
                "Price return over the period",
                pct(price_return, 2) if price_return is not None else "—",
                tone="critical" if price_return is not None and price_return < -5 else None,
            ),
            stat_tile(
                "OAS change",
                f"{oas_change:+.0f}bp" if oas_change is not None else "—",
            ),
        ],
        className="tile-row",
    )

    table = hist.assign(
        Month=hist["as_of_date"].dt.strftime("%b %Y"),
        **{
            "Par": hist["par_amount"].map(money),
            "Market value": hist["market_value"].map(money),
            "Price": hist["price"].map(lambda v: "—" if pd.isna(v) else f"{v:,.4f}"),
            "Source": hist.get("price_source", pd.Series(dtype=str)),
            "OAS": hist["oas_bps"].map(lambda v: "—" if pd.isna(v) else f"{v:,.1f}bp"),
            "Imputed MV": hist["market_value_imputed"].map({True: "yes", False: ""}),
        },
    )[["Month", "Par", "Market value", "Price", "Source", "OAS", "Imputed MV"]]

    gaps = int((hist["price_source"] == "none").sum()) if "price_source" in hist else 0
    implied = int((hist["price_source"] == "implied").sum()) if "price_source" in hist else 0

    provenance = []
    if implied:
        provenance.append(
            f"{implied} month-end(s) have no delivered mark; the price shown is implied "
            "from the reported market value and is ringed on the chart."
        )
    if gaps:
        provenance.append(f"{gaps} month-end(s) have no price at all.")
    if bool(hist["market_value_imputed"].any()):
        provenance.append(
            "At least one market value was imputed from par and price during loading."
        )

    return html.Div(
        [
            html.H2(meta["description"], className="security-title"),
            html.P(
                f"{meta['sector']} · {meta['rating']} · coupon "
                f"{meta['coupon_pct']:.3f}% · matures "
                f"{pd.Timestamp(meta['maturity_date']).strftime('%d %b %Y')}",
                className="security-meta",
            ),
            tiles,
            card(
                graph(
                    figures.security_position(hist, mode),
                    figures.H_STANDARD,
                ),
                wide=True,
            ),
            card(
                graph(
                    figures.security_price(hist, mode),
                    figures.H_STANDARD,
                ),
                wide=True,
            ),
            card(
                graph(
                    figures.security_oas(hist, mode),
                    figures.H_STANDARD,
                ),
                note(
                    "Price and OAS are deliberately separate charts sharing an x-range, "
                    "not one chart with two y-axes: two scales on a single plot make "
                    "their alignment arbitrary and suggest a correlation that is not in "
                    "the data.",
                ),
                wide=True,
            ),
            card(
                *([note(" ".join(provenance))] if provenance else []),
                table_view(table, "Full month-end history — table view", page_size=12),
                title="Position and mark history",
                wide=True,
            ),
        ]
    )


def layout(snap: Snapshot, mode: str = "light") -> html.Div:
    return html.Div(
        [
            html.H1("Security drill-down", className="page-title"),
            picker(snap),
            html.Div(id="security-detail"),
        ]
    )
