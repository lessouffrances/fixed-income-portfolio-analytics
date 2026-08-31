"""Weighted-average portfolio coupon and OAS. The assignment's bonus question.

Weighting basis, and why
------------------------
The bonus asks us to state and justify a weighting basis. There is no single
correct answer, so both are computed and the default is chosen per metric.

Coupon — weighted by PAR, by default.
    Coupon is contractually paid on par, not on market value. Annual coupon
    income is exactly sum(par x coupon), so par weighting is the only basis under
    which the weighted-average coupon multiplied by total par reproduces the
    portfolio's actual income. Market-value weighting would overstate the
    contribution of bonds trading above par and understate discount bonds,
    producing a number that describes no real cash flow.

OAS — weighted by MARKET VALUE, by default.
    OAS is a valuation measure: the spread an investor earns on capital actually
    at risk. Market-value weighting reflects the portfolio's real spread
    exposure, and is the market convention for aggregating spreads. Par
    weighting would treat a deeply discounted position as though the full par
    were exposed, which overstates the risk being carried.

Both bases are returned for each metric so the choice can be inspected rather
than taken on trust, and so a reviewer who prefers the other convention can read
the number they expect.

Coverage
--------
Every month reports the share of market value that actually carried a value for
the metric. A weighted average over 80% of the portfolio is a different claim
from one over 100%, and silently averaging what happens to be present is how a
plausible-looking wrong number gets published.
"""

from __future__ import annotations

import pandas as pd


def _weighted(values: pd.Series, weights: pd.Series) -> float:
    """Weighted mean over rows where both value and weight are usable.

    Returns NaN rather than 0.0 for an empty basis: zero is a claim about the
    portfolio, NaN is an admission that the number is unknown.
    """
    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return float("nan")
    w = weights[mask]
    return float((values[mask] * w).sum() / w.sum())


def monthly_weighted_metrics(positions: pd.DataFrame) -> pd.DataFrame:
    """Weighted-average coupon and OAS per month-end, on both bases."""
    rows = []
    for date, g in positions.groupby("as_of_date"):
        total_mv = g["market_value"].sum()
        coupon_mv = g.loc[g["coupon_pct"].notna(), "market_value"].sum()
        oas_mv = g.loc[g["oas_bps"].notna(), "market_value"].sum()

        rows.append(
            {
                "as_of_date": date,
                # Defaults, per the reasoning in the module docstring.
                "coupon_pct_wavg": _weighted(g["coupon_pct"], g["par_amount"]),
                "oas_bps_wavg": _weighted(g["oas_bps"], g["market_value"]),
                # Alternative bases, for comparison.
                "coupon_pct_wavg_by_mv": _weighted(g["coupon_pct"], g["market_value"]),
                "oas_bps_wavg_by_par": _weighted(g["oas_bps"], g["par_amount"]),
                # Coverage: what fraction of the portfolio each average speaks for.
                "coupon_coverage_pct": (coupon_mv / total_mv * 100.0) if total_mv else float("nan"),
                "oas_coverage_pct": (oas_mv / total_mv * 100.0) if total_mv else float("nan"),
                "market_value": total_mv,
                "par_amount": g["par_amount"].sum(),
                "positions": g["security_id"].nunique(),
            }
        )
    return pd.DataFrame(rows).sort_values("as_of_date").reset_index(drop=True)


def weighted_metrics_by_dimension(
    positions: pd.DataFrame, dimension: str = "sector"
) -> pd.DataFrame:
    """The same metrics grouped by sector or rating, for the allocation view."""
    rows = []
    for (date, key), g in positions.groupby(["as_of_date", dimension]):
        rows.append(
            {
                "as_of_date": date,
                dimension: key,
                "coupon_pct_wavg": _weighted(g["coupon_pct"], g["par_amount"]),
                "oas_bps_wavg": _weighted(g["oas_bps"], g["market_value"]),
                "market_value": g["market_value"].sum(),
                "positions": g["security_id"].nunique(),
            }
        )
    return pd.DataFrame(rows).sort_values(["as_of_date", dimension]).reset_index(drop=True)


def security_history(positions: pd.DataFrame, security_id: str) -> pd.DataFrame:
    """One security's full month-end history. Backs the drill-down page.

    Carries the provenance flags alongside the values, so the page can mark a
    repaired price or an imputed market value rather than presenting either as if
    it had been delivered that way.
    """
    cols = [
        "as_of_date",
        "par_amount",
        "book_value",
        "market_value",
        "market_value_imputed",
        "clean_price",
        "price",
        "price_source",
        "oas_bps",
    ]
    df = positions[positions["security_id"] == security_id]
    present = [c for c in cols if c in df.columns]
    out = df[present].sort_values("as_of_date").reset_index(drop=True)

    if "price" in out.columns:
        out["price_change_pct"] = out["price"].pct_change() * 100.0
    if "oas_bps" in out.columns:
        out["oas_change_bps"] = out["oas_bps"].diff()
    return out
