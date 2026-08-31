"""Allocation view: sector and rating mix over time.

Required feature 2, plus the Q2 and Q3 answers.
"""

from __future__ import annotations

import pandas as pd
from dash import dcc, html

from .. import figures
from ..components import graph, card, data_table, money, note, pct, signed_money, stat_tile, table_view
from ..data import Snapshot


def _shift_table(shift: pd.DataFrame, dimension: str) -> pd.DataFrame:
    d = shift.sort_values("abs_weight_change", ascending=False)
    return d.assign(
        **{
            dimension.title(): d[dimension],
            "Jan weight": d["start_weight_pct"].map(lambda v: pct(v, 1)),
            "Dec weight": d["end_weight_pct"].map(lambda v: pct(v, 1)),
            "Shift (pp)": d["weight_change_pct"].map(lambda v: f"{v:+.2f}"),
            "Value change": d["mv_change"].map(signed_money),
            "Market": d["market_effect"].map(signed_money),
            "Trading": d["trading_effect"].map(signed_money),
            "Driver": d["driver"].str.title(),
        }
    )[
        [
            dimension.title(), "Jan weight", "Dec weight", "Shift (pp)",
            "Value change", "Market", "Trading", "Driver",
        ]
    ]


def layout(snap: Snapshot, mode: str = "light") -> html.Div:
    # Each of these can legitimately be empty on a different extract, so nothing
    # below indexes into a frame without checking first. A single-month extract
    # produces no shifts at all, and an extract whose sectors each hold one
    # security produces no rankable sector-month (see worst_sector_month's
    # min_positions). Crashing the whole page in either case would be worse than
    # omitting the section that has nothing to say.
    has_worst = not snap.worst_sector_months.empty
    has_sector_shift = not snap.sector_shift.empty
    has_rating_shift = not snap.rating_shift.empty

    worst = snap.worst_sector_months.iloc[0] if has_worst else None
    worst_month = pd.Timestamp(worst["as_of_date"]) if has_worst else None
    top_sector = snap.sector_shift.iloc[0] if has_sector_shift else None
    top_rating = snap.rating_shift.iloc[0] if has_rating_shift else None

    tile_list = []
    if top_sector is not None:
        tile_list.append(
            stat_tile(
                "Largest sector shift",
                f"{top_sector['sector']} {top_sector['weight_change_pct']:+.2f}pp",
                f"driven by {top_sector['driver']}",
            )
        )
    if top_rating is not None:
        tile_list.append(
            stat_tile(
                "Largest rating shift",
                f"{top_rating['rating']} {top_rating['weight_change_pct']:+.2f}pp",
                f"driven by {top_rating['driver']}",
            )
        )
    if worst is not None:
        tile_list.append(
            stat_tile(
                "Worst sector-month",
                f"{worst['sector']}, {worst_month.strftime('%b')}",
                f"price {worst['price_change_pct']:+.1f}%, "
                f"OAS {worst['oas_change_bps']:+.0f}bp",
                tone="critical",
            )
        )
    tiles = html.Div(tile_list, className="tile-row")

    sector_detail = snap.sector_detail
    worst_sector_series = (
        sector_detail[sector_detail["sector"] == worst["sector"]]
        if has_worst
        else sector_detail.iloc[0:0]
    )
    ws_table = worst_sector_series.assign(
        Month=worst_sector_series["as_of_date"].dt.strftime("%b %Y"),
        **{
            "Avg price": worst_sector_series["avg_price"].map(lambda v: f"{v:,.2f}"),
            "Price change": worst_sector_series["price_change_pct"].map(
                lambda v: "—" if pd.isna(v) else f"{v:+.2f}%"
            ),
            "Wtd OAS": worst_sector_series["oas_bps_wavg"].map(lambda v: f"{v:,.0f}bp"),
            "OAS change": worst_sector_series["oas_change_bps"].map(
                lambda v: "—" if pd.isna(v) else f"{v:+.0f}bp"
            ),
            "Market value": worst_sector_series["market_value"].map(money),
        },
    )[["Month", "Avg price", "Price change", "Wtd OAS", "OAS change", "Market value"]]

    return html.Div(
        [
            html.H1("Allocation", className="page-title"),
            tiles,
            card(
                graph(
                    figures.allocation_area(snap.sector_mix, "sector", mode),
                    figures.H_TALL,
                ),
                note(
                    "Sector count exceeds the eight validated colour slots, so the "
                    "smallest sectors fold into a single \"Other\" band rather than "
                    "being given a generated ninth hue. The table view carries every "
                    "sector individually.",
                ),
                table_view(
                    snap.sector_mix.assign(
                        Month=snap.sector_mix["as_of_date"].dt.strftime("%b %Y"),
                        Sector=snap.sector_mix["sector"],
                        **{
                            "Market value": snap.sector_mix["market_value"].map(money),
                            "Weight": snap.sector_mix["weight_pct"].map(lambda v: pct(v, 1)),
                        },
                    )[["Month", "Sector", "Market value", "Weight"]],
                    "Sector allocation — table view",
                    page_size=15,
                ),
                wide=True,
            ),
            card(
                graph(
                    figures.allocation_area(snap.rating_mix, "rating", mode),
                    figures.H_TALL,
                ),
                note(
                    "Ratings are stacked in credit-quality order, so the stack itself "
                    "carries the ordering rather than relying on hue to imply it.",
                ),
                table_view(
                    snap.rating_mix.assign(
                        Month=snap.rating_mix["as_of_date"].dt.strftime("%b %Y"),
                        Rating=snap.rating_mix["rating"],
                        **{
                            "Market value": snap.rating_mix["market_value"].map(money),
                            "Weight": snap.rating_mix["weight_pct"].map(lambda v: pct(v, 1)),
                        },
                    )[["Month", "Rating", "Market value", "Weight"]],
                    "Rating allocation — table view",
                    page_size=15,
                ),
                wide=True,
            ),
            *([card(
                graph(
                    figures.shift_bars(snap.sector_shift.head(6), "sector", mode),
                    figures.H_STANDARD,
                ),
                note(
                    "A weight change is not a value change. A sector can lose weight "
                    "while gaining value, simply because the rest of the portfolio grew "
                    "faster — so both are reported, and the value change is decomposed "
                    "into market and trading effects.",
                ),
                data_table(_shift_table(snap.sector_shift, "sector"), page_size=12),
                title="Q2 — sector shifts, first to last month-end",
                wide=True,
            )] if has_sector_shift else []),
            *([card(
                data_table(_shift_table(snap.rating_shift, "rating"), page_size=10),
                title="Q2 — rating shifts, first to last month-end",
                wide=True,
            )] if has_rating_shift else []),
            *([card(
                html.P(
                    [
                        html.Strong(f"{worst['sector']}"),
                        f" in {worst_month.strftime('%B %Y')}: average price fell ",
                        html.Strong(f"{worst['price_change_pct']:.2f}%"),
                        " while weighted-average OAS widened ",
                        html.Strong(f"{worst['oas_change_bps']:+.0f}bp"),
                        f" across all {int(worst['positions'])} holdings in the sector. "
                        "Price down with spread sharply wider, across every name, is a "
                        "credit event rather than a rates move.",
                    ],
                    className="finding",
                ),
                data_table(ws_table, page_size=12),
                title=f"Q3 — {worst['sector']}'s bad month",
                wide=True,
            )] if has_worst else [
                card(
                    note(
                        "No sector-month could be ranked: ranking requires at least "
                        "two securities in a sector so that an average price move "
                        "means something, and no sector in this extract meets that."
                    ),
                    title="Q3 — worst sector-month",
                    wide=True,
                )
            ]),
        ]
    )
