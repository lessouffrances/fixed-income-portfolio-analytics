"""Allocation mix over time, and the drivers behind a shift.

Answers Q2 (sector and rating allocation in January versus December, the three
largest shifts, and whether each was trading or market) and Q3 (the sector that
had a clearly bad month).

The interesting part is not computing the mix — that is a group-by — but
attributing a *change* in mix to a cause. A sector's weight can fall while its
market value rises, simply because the rest of the portfolio grew faster. So the
analysis reports both the weight change and the value change, and decomposes the
value change using the same price/trading/interaction identity as Q1. Reporting
weight movement alone would invite exactly the wrong conclusion.
"""

from __future__ import annotations

import pandas as pd

from .attribution import decompose_positions


def _mix(positions: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """Market value and portfolio weight by dimension and month-end."""
    grouped = (
        positions.groupby(["as_of_date", dimension])["market_value"]
        .sum()
        .reset_index(name="market_value")
    )
    totals = grouped.groupby("as_of_date")["market_value"].transform("sum")
    grouped["weight_pct"] = grouped["market_value"] / totals * 100.0
    return grouped


def sector_mix(positions: pd.DataFrame) -> pd.DataFrame:
    return _mix(positions, "sector")


def rating_mix(positions: pd.DataFrame) -> pd.DataFrame:
    return _mix(positions, "rating")


def allocation_shift(
    positions: pd.DataFrame,
    dimension: str = "sector",
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    top_n: int | None = 3,
) -> pd.DataFrame:
    """Change in allocation between two month-ends, with its cause. Q2.

    Defaults to the first and last month-ends present, so the function does not
    assume a January-to-December window.

    Each row carries:
      weight_change_pct  — movement in portfolio weight (percentage points)
      mv_change          — movement in market value (dollars)
      price_effect / trading_effect / interaction_effect
                         — the value change decomposed, summed over the interval
      driver             — which effect dominates in magnitude

    Ranked by absolute weight change, because Q2 asks about allocation shifts
    rather than the largest dollar movements.
    """
    mix = _mix(positions, dimension)
    dates = sorted(mix["as_of_date"].unique())
    start = start or dates[0]
    end = end or dates[-1]

    a = mix[mix["as_of_date"] == start].set_index(dimension)
    b = mix[mix["as_of_date"] == end].set_index(dimension)

    keys = sorted(set(a.index) | set(b.index))
    out = pd.DataFrame(index=pd.Index(keys, name=dimension))
    # Absent from a month means zero exposure, not missing data.
    out["start_mv"] = a["market_value"].reindex(keys).fillna(0.0)
    out["end_mv"] = b["market_value"].reindex(keys).fillna(0.0)
    out["start_weight_pct"] = a["weight_pct"].reindex(keys).fillna(0.0)
    out["end_weight_pct"] = b["weight_pct"].reindex(keys).fillna(0.0)
    out["mv_change"] = out["end_mv"] - out["start_mv"]
    out["weight_change_pct"] = out["end_weight_pct"] - out["start_weight_pct"]

    # Attribute the value change over every month strictly after `start` up to
    # and including `end`, so the effects sum to the interval's total change.
    detail = decompose_positions(positions)
    window = detail[(detail["as_of_date"] > start) & (detail["as_of_date"] <= end)]
    dim_map = positions.drop_duplicates("security_id").set_index("security_id")[dimension]
    window = window.assign(**{dimension: window["security_id"].map(dim_map)})
    effects = window.groupby(dimension)[
        ["price_effect", "trading_effect", "interaction_effect"]
    ].sum()
    for c in effects.columns:
        out[c] = effects[c].reindex(keys).fillna(0.0)

    # Market moves comprise price plus the cross term: the cross term only exists
    # because prices moved, so grouping it with price is the honest reading.
    out["market_effect"] = out["price_effect"] + out["interaction_effect"]
    out["driver"] = out.apply(
        lambda r: "trading"
        if abs(r["trading_effect"]) >= abs(r["market_effect"])
        else "market",
        axis=1,
    )
    out["abs_weight_change"] = out["weight_change_pct"].abs()

    out = out.sort_values("abs_weight_change", ascending=False).reset_index()
    return out.head(top_n) if top_n else out


def sector_month_detail(positions: pd.DataFrame) -> pd.DataFrame:
    """Per-sector, per-month price and spread summary. The basis for Q3.

    Prices are equal-weighted across the sector's securities, and OAS is
    market-value weighted.

    Those weightings differ deliberately. An equal-weighted average price answers
    "what happened to bonds in this sector", which is the question Q3 asks, and is
    not distorted by one large holding. OAS is a valuation spread, so weighting it
    by market value reflects the portfolio's actual spread exposure. Both are
    reported per sector so either can be inspected.
    """
    df = positions.copy()
    df["_mv_for_oas"] = df["market_value"].where(df["oas_bps"].notna())

    grouped = df.groupby(["sector", "as_of_date"])
    out = grouped.apply(
        lambda g: pd.Series(
            {
                "avg_price": g["clean_price"].mean(),
                "oas_bps_wavg": (
                    (g["oas_bps"] * g["_mv_for_oas"]).sum() / g["_mv_for_oas"].sum()
                    if g["_mv_for_oas"].sum() > 0
                    else float("nan")
                ),
                "market_value": g["market_value"].sum(),
                "positions": g["security_id"].nunique(),
                "priced": g["clean_price"].notna().sum(),
            }
        ),
        include_groups=False,
    ).reset_index()

    out = out.sort_values(["sector", "as_of_date"])
    g = out.groupby("sector", sort=False)
    out["price_change_pct"] = g["avg_price"].pct_change() * 100.0
    out["oas_change_bps"] = g["oas_bps_wavg"].diff()
    return out


def worst_sector_month(
    positions: pd.DataFrame, min_positions: int = 2
) -> pd.DataFrame:
    """Rank sector-months by price deterioration. Q3's answer, ranked.

    Sorted by average price change so the worst month surfaces first. The
    accompanying OAS change is what distinguishes a credit event (price down and
    spread wider) from a rates move (price down and spread flat).

    min_positions excludes buckets too small for a "sector average" to mean
    anything. In this extract the placeholder bucket that holds securities with a
    missing sector contains a single bond, and a one-bond average is that bond's
    own price move — it would compete with genuine sectors in the ranking on the
    strength of idiosyncratic noise. The rows remain in sector_month_detail, so
    nothing is hidden; they are only kept out of the ranking. Set to 1 to rank
    everything.
    """
    detail = sector_month_detail(positions)
    ranked = detail[
        detail["price_change_pct"].notna() & (detail["positions"] >= min_positions)
    ].sort_values("price_change_pct")
    return ranked.reset_index(drop=True)


def sector_month_impact(
    positions: pd.DataFrame, sector: str, as_of_date: pd.Timestamp
) -> pd.Series:
    """What one bad sector-month did to the whole portfolio. Q3's final clause."""
    detail = decompose_positions(positions)
    dim_map = positions.drop_duplicates("security_id").set_index("security_id")["sector"]
    detail = detail.assign(sector=detail["security_id"].map(dim_map))

    month = detail[detail["as_of_date"] == as_of_date]
    in_sector = month[month["sector"] == sector]

    portfolio_change = month["mv_change"].sum()
    sector_change = in_sector["mv_change"].sum()
    return pd.Series(
        {
            "sector": sector,
            "as_of_date": as_of_date,
            "sector_mv_change": sector_change,
            "sector_price_effect": in_sector["price_effect"].sum(),
            "sector_trading_effect": in_sector["trading_effect"].sum(),
            "portfolio_mv_change": portfolio_change,
            "share_of_portfolio_change_pct": (
                sector_change / portfolio_change * 100.0 if portfolio_change else float("nan")
            ),
            "securities_affected": in_sector["security_id"].nunique(),
        }
    )
