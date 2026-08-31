"""Decomposition of market-value change into trading and market components.

Answers Q1 (which month moved most, and why) and Q4 (worst full-year price
returns, and the dollar impact of price alone). Both rest on the same identity,
so they are implemented once here.

The method
----------
A position's market value is par x price / 100. Between two month-ends both
factors move, so the change in market value is:

    MV_t - MV_(t-1)
      = [par_t x P_t - par_(t-1) x P_(t-1)] / 100

Expanding around the opening position gives an exact three-way split:

    price effect       = par_(t-1) x (P_t - P_(t-1)) / 100
    trading effect     = (par_t - par_(t-1)) x P_(t-1) / 100
    interaction        = (par_t - par_(t-1)) x (P_t - P_(t-1)) / 100

    ΔMV = price + trading + interaction        (exactly, no residual)

Why this split rather than a simpler two-way one: the cross term is real. A bond
bought during a month in which prices moved has a market-value change that is
genuinely attributable to neither price alone nor trading alone. Folding it
silently into one side is a common source of attribution that does not add up.
Reporting it separately keeps the identity exact and the choice visible. It is
normally small; when it is not, that itself is worth seeing.

Trading is measured two independent ways
----------------------------------------
1. From holdings: (par_t - par_(t-1)) x P_(t-1). Reconciles to ΔMV by construction.
2. From the transactions file: signed par x trade price, summed over the month.

These should agree. Where they do not, par moved without a corresponding trade —
unexplained drift, which is a data-quality signal rather than an analytical one.
`reconcile_trading` reports the gap instead of hiding it, because the assignment
asks how much of a change was trading, and a silent mismatch would make that
answer unverifiable.
"""

from __future__ import annotations

import pandas as pd

# Positions are held per 100 of par throughout.
PAR_BASIS = 100.0


def _panel(positions: pd.DataFrame) -> pd.DataFrame:
    """Pivot to a (security, month) panel carrying prior-month par and price.

    Securities absent in a month are filled with zero par and a null price, so a
    position that enters or leaves the portfolio is handled by the same arithmetic
    as one that persists — no special cases.
    """
    cols = ["as_of_date", "security_id", "par_amount", "price", "market_value"]
    p = positions[cols].copy()

    dates = sorted(p["as_of_date"].unique())
    ids = sorted(p["security_id"].unique())
    full = pd.MultiIndex.from_product([dates, ids], names=["as_of_date", "security_id"])

    p = p.set_index(["as_of_date", "security_id"]).reindex(full).reset_index()
    p["par_amount"] = p["par_amount"].fillna(0.0)
    p["market_value"] = p["market_value"].fillna(0.0)

    p = p.sort_values(["security_id", "as_of_date"])
    g = p.groupby("security_id", sort=False)
    p["prev_par"] = g["par_amount"].shift(1)
    p["prev_price"] = g["price"].shift(1)
    p["prev_mv"] = g["market_value"].shift(1)

    # A position entering the portfolio has no prior price. Use the current price
    # as the reference so the whole change is attributed to trading rather than
    # producing a null that would silently drop out of the sum.
    p["prev_price"] = p["prev_price"].fillna(p["price"])
    return p


def decompose_positions(positions: pd.DataFrame) -> pd.DataFrame:
    """Per-security, per-month attribution. The building block for Q1 and Q4."""
    p = _panel(positions)
    p = p[p["prev_par"].notna()].copy()  # drop the first month: nothing to compare

    d_par = p["par_amount"] - p["prev_par"]
    d_price = p["price"] - p["prev_price"]

    p["par_change"] = d_par
    p["price_change"] = d_price
    p["price_effect"] = p["prev_par"] * d_price / PAR_BASIS
    p["trading_effect"] = d_par * p["prev_price"] / PAR_BASIS
    p["interaction_effect"] = d_par * d_price / PAR_BASIS
    p["mv_change"] = p["market_value"] - p["prev_mv"]

    # Where a price is missing entirely the effects are undefined; treat them as
    # zero so sums remain finite, and surface the gap via `unexplained`.
    for c in ("price_effect", "trading_effect", "interaction_effect"):
        p[c] = p[c].fillna(0.0)

    p["unexplained"] = p["mv_change"] - (
        p["price_effect"] + p["trading_effect"] + p["interaction_effect"]
    )
    return p


def portfolio_overview(positions: pd.DataFrame) -> pd.DataFrame:
    """Total market value by month-end with month-over-month change. Q1, part one."""
    out = (
        positions.groupby("as_of_date")
        .agg(
            market_value=("market_value", "sum"),
            par_amount=("par_amount", "sum"),
            positions=("security_id", "nunique"),
            priced_positions=("clean_price", "count"),
        )
        .reset_index()
        .sort_values("as_of_date")
    )
    out["mv_change"] = out["market_value"].diff()
    out["mv_change_pct"] = out["market_value"].pct_change() * 100.0
    return out


def monthly_attribution(positions: pd.DataFrame) -> pd.DataFrame:
    """Portfolio-level attribution by month. Q1, part two."""
    detail = decompose_positions(positions)
    out = (
        detail.groupby("as_of_date")
        .agg(
            mv_change=("mv_change", "sum"),
            price_effect=("price_effect", "sum"),
            trading_effect=("trading_effect", "sum"),
            interaction_effect=("interaction_effect", "sum"),
            unexplained=("unexplained", "sum"),
        )
        .reset_index()
        .sort_values("as_of_date")
    )
    out["market_effect"] = out["price_effect"] + out["interaction_effect"]
    out["abs_mv_change"] = out["mv_change"].abs()
    return out


def largest_move(positions: pd.DataFrame) -> pd.Series:
    """The month with the largest absolute market-value change. Q1's headline."""
    att = monthly_attribution(positions)
    return att.loc[att["abs_mv_change"].idxmax()]


def trading_from_transactions(trades: pd.DataFrame) -> pd.DataFrame:
    """Net trading cash flow per month, straight from the activity file."""
    if trades.empty:
        return pd.DataFrame(
            columns=["as_of_date", "net_cash_flow", "buys", "sells", "maturities", "trade_count"]
        )
    g = trades.groupby("as_of_date")
    out = pd.DataFrame(
        {
            "net_cash_flow": g["cash_flow"].sum(),
            "buys": g.apply(
                lambda d: d.loc[d["trade_type"] == "BUY", "cash_flow"].sum(),
                include_groups=False,
            ),
            "sells": g.apply(
                lambda d: d.loc[d["trade_type"] == "SELL", "cash_flow"].sum(),
                include_groups=False,
            ),
            "maturities": g.apply(
                lambda d: d.loc[d["trade_type"] == "MATURITY", "cash_flow"].sum(),
                include_groups=False,
            ),
            "trade_count": g.size(),
        }
    ).reset_index()
    return out


def reconcile_trading(positions: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """Compare holdings-derived trading against the transactions file.

    Two independent measurements of the same quantity. A non-zero difference means
    par moved without a matching trade (or vice versa) and is reported rather than
    absorbed — the assignment asks how much of a move was trading, and an
    unverified answer to that is worth less than a verified one with a stated gap.
    """
    att = monthly_attribution(positions)[["as_of_date", "trading_effect"]]
    tx = trading_from_transactions(trades)
    out = att.merge(tx, on="as_of_date", how="left")
    out[["net_cash_flow", "buys", "sells", "maturities"]] = out[
        ["net_cash_flow", "buys", "sells", "maturities"]
    ].fillna(0.0)
    out["trade_count"] = out["trade_count"].fillna(0).astype(int)
    out["difference"] = out["trading_effect"] - out["net_cash_flow"]
    return out


def security_price_returns(
    positions: pd.DataFrame, *, held_all_year: bool = True, top_n: int = 10
) -> pd.DataFrame:
    """Worst full-year price returns and the dollar impact of price alone. Q4.

    The dollar impact is the sum of each month's price effect —
    par_(t-1) x ΔP_t / 100 — accumulated over the year. That isolates price from
    trading while still respecting the par actually held in each month, which a
    naive "January par x full-year price change" would not: a position halved in
    June did not suffer the second half of the year's move at full size.

    Using the same identity as Q1 also means the per-security impacts sum back to
    the portfolio's total price effect.
    """
    detail = decompose_positions(positions)
    n_months = positions["as_of_date"].nunique()

    if held_all_year:
        held = positions[positions["par_amount"].fillna(0) > 0]
        counts = held.groupby("security_id")["as_of_date"].nunique()
        eligible = set(counts[counts == n_months].index)
        detail = detail[detail["security_id"].isin(eligible)]

    firsts = positions.sort_values("as_of_date").groupby("security_id").first()
    lasts = positions.sort_values("as_of_date").groupby("security_id").last()

    agg = (
        detail.groupby("security_id")
        .agg(price_impact=("price_effect", "sum"))
        .reset_index()
    )
    agg["start_price"] = agg["security_id"].map(firsts["price"])
    agg["end_price"] = agg["security_id"].map(lasts["price"])
    agg["price_return_pct"] = (agg["end_price"] / agg["start_price"] - 1.0) * 100.0
    for col in ("sector", "rating", "description"):
        agg[col] = agg["security_id"].map(firsts[col])
    agg["start_par"] = agg["security_id"].map(firsts["par_amount"])

    return agg.sort_values("price_return_pct").head(top_n).reset_index(drop=True)
