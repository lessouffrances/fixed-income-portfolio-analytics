"""Database reads. The only place the analytics layer touches SQL.

Everything below returns plain DataFrames, so the analytics functions themselves
are pure and testable against hand-built frames. That separation is what lets the
attribution maths be tested without a database — the arithmetic is the part worth
testing, and it should not need Postgres to exercise.

The application reads exclusively from these functions at runtime, never from the
CSVs (assignment requirement 1).
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import Engine, select

from ..models import dq_finding, holding, load_run, mark, security, trade


def _frame(engine: Engine, stmt) -> pd.DataFrame:
    with engine.connect() as conn:
        rows = conn.execute(stmt)
        return pd.DataFrame(rows.mappings().all(), columns=list(rows.keys()))


def load_positions(
    engine: Engine, *, include_post_maturity: bool = False
) -> pd.DataFrame:
    """Month-end positions joined to reference data and marks.

    One row per (security, month-end) — the grain the whole analytics layer works
    at. A LEFT JOIN to marks because some held positions have no price for their
    month; dropping them would understate portfolio market value, so the null is
    carried forward and handled explicitly downstream.

    include_post_maturity defaults to False. A position reported after its
    security's maturity date is fabricated — the bond has redeemed and cannot
    carry market value — and in this extract one such position contributes
    $13.5M of phantom value across three months and manufactures $4.5M of
    non-existent trading activity in the month it reappears. Including it would
    corrupt the headline market-value series and the attribution alike. The flag
    exists so the exclusion can be inspected and reversed rather than being an
    invisible WHERE clause.
    """
    stmt = (
        select(
            holding.c.as_of_date,
            holding.c.security_id,
            holding.c.par_amount,
            holding.c.book_value,
            holding.c.market_value,
            holding.c.market_value_imputed,
            holding.c.post_maturity,
            mark.c.clean_price,
            mark.c.oas_bps,
            security.c.sector,
            security.c.rating,
            security.c.asset_class,
            security.c.issuer,
            security.c.description,
            security.c.coupon_pct,
            security.c.maturity_date,
        )
        .select_from(
            holding.join(security, holding.c.security_id == security.c.security_id).outerjoin(
                mark,
                (mark.c.security_id == holding.c.security_id)
                & (mark.c.as_of_date == holding.c.as_of_date),
            )
        )
        .order_by(holding.c.as_of_date, holding.c.security_id)
    )
    if not include_post_maturity:
        stmt = stmt.where(holding.c.post_maturity.is_(False))
    df = _frame(engine, stmt)
    if df.empty:
        return df

    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    for c in (
        "par_amount",
        "book_value",
        "market_value",
        "clean_price",
        "oas_bps",
        "coupon_pct",
    ):
        # NUMERIC arrives as Decimal; convert once here so downstream arithmetic
        # never mixes Decimal and float (which raises rather than coercing).
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Effective price used by every price-based calculation. Prefer the mark; fall
    # back to the price implied by the reported market value when no mark exists.
    # Documented in the assumptions log: without the fallback, a position with no
    # mark contributes nothing to price attribution and the decomposition stops
    # reconciling to the market-value change.
    implied = (df["market_value"] / df["par_amount"] * 100.0).where(
        df["par_amount"].ne(0)
    )
    df["price"] = df["clean_price"].fillna(implied)
    df["price_source"] = df["clean_price"].notna().map({True: "mark", False: "implied"})
    df.loc[df["price"].isna(), "price_source"] = "none"
    return df


def load_trades(engine: Engine) -> pd.DataFrame:
    """Activity, with a signed par change and the month-end it belongs to.

    Signing convention: BUY increases par, SELL and MATURITY reduce it. Trades are
    assigned to the month of trade_date rather than settlement_date, so that
    activity lines up with the month-end snapshot it moved.
    """
    stmt = (
        select(
            trade.c.trade_id,
            trade.c.trade_date,
            trade.c.settlement_date,
            trade.c.security_id,
            trade.c.trade_type,
            trade.c.par_amount,
            trade.c.price,
            security.c.sector,
            security.c.rating,
        )
        .select_from(trade.join(security, trade.c.security_id == security.c.security_id))
        .order_by(trade.c.trade_date)
    )
    df = _frame(engine, stmt)
    if df.empty:
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["settlement_date"] = pd.to_datetime(df["settlement_date"])
    df["par_amount"] = pd.to_numeric(df["par_amount"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")

    sign = df["trade_type"].map({"BUY": 1, "SELL": -1, "MATURITY": -1})
    df["signed_par"] = df["par_amount"] * sign
    df["cash_flow"] = df["signed_par"] * df["price"] / 100.0
    # The month-end this trade lands in, derived from the date itself so the
    # function does not assume a 2025 calendar.
    df["as_of_date"] = df["trade_date"] + pd.offsets.MonthEnd(0)
    return df


def load_findings(engine: Engine, load_id: int | None = None) -> pd.DataFrame:
    """Data-quality findings for the data-quality page."""
    stmt = select(dq_finding)
    if load_id is not None:
        stmt = stmt.where(dq_finding.c.load_id == load_id)
    return _frame(engine, stmt.order_by(dq_finding.c.severity, dq_finding.c.rule_code))


def load_runs(engine: Engine) -> pd.DataFrame:
    """Load history, so the app can show when the data was last refreshed."""
    return _frame(engine, select(load_run).order_by(load_run.c.load_id.desc()))


def month_ends(positions: pd.DataFrame) -> list[pd.Timestamp]:
    return sorted(positions["as_of_date"].unique())
