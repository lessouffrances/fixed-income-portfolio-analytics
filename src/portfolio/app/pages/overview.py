"""Portfolio overview: total market value over time and month-over-month change.

Required feature 1. Also carries the Q1 answer, since the attribution is what
makes the month-over-month number mean anything.
"""

from __future__ import annotations

import pandas as pd
from dash import dcc, html

from .. import figures
from ..components import (
    card,
    data_table,
    money,
    note,
    pct,
    signed_money,
    stat_tile,
    table_view,
    graph,
)
from ..data import Snapshot


def layout(snap: Snapshot, mode: str = "light") -> html.Div:
    ov = snap.overview
    att = snap.monthly_attribution
    latest = ov.iloc[-1]
    first = ov.iloc[0]
    biggest = att.loc[att["abs_mv_change"].idxmax()]

    # The headline numbers. A stat tile, not a one-bar bar chart.
    tiles = html.Div(
        [
            stat_tile(
                "Market value, latest month-end",
                money(latest["market_value"]),
                pd.Timestamp(latest["as_of_date"]).strftime("%d %b %Y"),
            ),
            stat_tile(
                "Change over the year",
                signed_money(latest["market_value"] - first["market_value"]),
                f"from {money(first['market_value'])} in "
                f"{pd.Timestamp(first['as_of_date']).strftime('%b')}",
            ),
            stat_tile(
                "Largest monthly move",
                signed_money(biggest["mv_change"]),
                pd.Timestamp(biggest["as_of_date"]).strftime("%b %Y"),
            ),
            stat_tile(
                "Positions held",
                f"{int(latest['positions']):,}",
                f"{int(latest['priced_positions'])} with a mark",
            ),
        ],
        className="tile-row",
    )

    driver = (
        "trading"
        if abs(biggest["trading_effect"]) > abs(biggest["market_effect"])
        else "market moves"
    )

    table = ov.assign(
        Month=ov["as_of_date"].dt.strftime("%b %Y"),
        **{
            "Market value": ov["market_value"].map(money),
            "Change": ov["mv_change"].map(signed_money),
            "Change %": ov["mv_change_pct"].map(lambda v: pct(v, 2)),
            "Positions": ov["positions"],
        },
    )[["Month", "Market value", "Change", "Change %", "Positions"]]

    att_table = att.assign(
        Month=att["as_of_date"].dt.strftime("%b %Y"),
        **{
            "Total change": att["mv_change"].map(signed_money),
            "Price": att["price_effect"].map(signed_money),
            "Trading": att["trading_effect"].map(signed_money),
            "Interaction": att["interaction_effect"].map(signed_money),
        },
    )[["Month", "Total change", "Price", "Trading", "Interaction"]]

    rec = snap.reconciliation.assign(
        Month=snap.reconciliation["as_of_date"].dt.strftime("%b %Y"),
        **{
            "From holdings": snap.reconciliation["trading_effect"].map(signed_money),
            "From transactions": snap.reconciliation["net_cash_flow"].map(signed_money),
            "Difference": snap.reconciliation["difference"].map(signed_money),
            "Trades": snap.reconciliation["trade_count"],
        },
    )[["Month", "From holdings", "From transactions", "Difference", "Trades"]]

    return html.Div(
        [
            html.H1("Portfolio overview", className="page-title"),
            tiles,
            card(
                graph(
                    figures.market_value_over_time(ov, mode),
                    figures.H_STANDARD,
                ),
                table_view(table, "Market value by month — table view"),
                wide=True,
            ),
            card(
                graph(
                    figures.mv_change_bars(ov, mode),
                    figures.H_STANDARD,
                ),
            ),
            card(
                graph(
                    figures.attribution_bars(att, mode),
                    figures.H_STANDARD,
                ),
                note(
                    "Each month's change is split exactly into a price effect on the "
                    "opening position, trading valued at the opening price, and the "
                    "interaction between them. The three sum to the total change with "
                    "no residual.",
                ),
                table_view(att_table, "Attribution by month — table view"),
                wide=True,
            ),
            card(
                html.P(
                    [
                        html.Strong(
                            pd.Timestamp(biggest["as_of_date"]).strftime("%B %Y")
                        ),
                        f" saw the largest absolute move at {signed_money(biggest['mv_change'])}, "
                        f"driven by {driver}: ",
                        html.Strong(signed_money(biggest["trading_effect"])),
                        " from trading against ",
                        html.Strong(signed_money(biggest["market_effect"])),
                        " from the market.",
                    ],
                    className="finding",
                ),
                note(
                    "Trading is measured twice — once from the change in par valued at "
                    "the opening price, and again independently from the transactions "
                    "file — and the two are reconciled below rather than assumed to "
                    "agree. Residual differences are the expected consequence of "
                    "month-end prices versus actual trade prices.",
                ),
                data_table(rec, page_size=12),
                title="Q1 — what moved, and why",
                wide=True,
            ),
        ]
    )
