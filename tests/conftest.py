"""Synthetic fixtures for the cleaning-rule tests.

Deliberately synthetic rather than slices of data/*.csv. The rules must work on
any extract with these problem shapes, so testing against the delivered file
would let an accidentally id-specific or date-specific rule pass unnoticed. Each
builder returns a minimal *clean* frame; a test perturbs exactly one thing and
asserts that exactly the expected rule fires.
"""

from __future__ import annotations

import pandas as pd
import pytest

from portfolio.load.thresholds import Thresholds


@pytest.fixture
def th() -> Thresholds:
    return Thresholds()


# Month-ends in an arbitrary year that is NOT 2025, so any rule that accidentally
# hardcodes the delivered extract's calendar fails loudly here.
MONTH_ENDS = [f"2031-{m:02d}-{d}" for m, d in [(1, 31), (2, 28), (3, 31), (4, 30)]]


@pytest.fixture
def master() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "security_id": "AAA111",
                "description": "Acme 5% 2040",
                "issuer": "Acme",
                "sector": "Industrials",
                "rating": "BBB",
                "coupon_pct": 5.0,
                "issue_date": "2020-01-15",
                "maturity_date": "2040-01-15",
                "asset_class": "Corporate",
            },
            {
                "security_id": "BBB222",
                "description": "Beta 3% 2032",
                "issuer": "Beta",
                "sector": "Utilities",
                "rating": "A",
                "coupon_pct": 3.0,
                "issue_date": "2019-06-01",
                "maturity_date": "2032-06-01",
                "asset_class": "Corporate",
            },
        ]
    )


@pytest.fixture
def marks() -> pd.DataFrame:
    rows = []
    for d in MONTH_ENDS:
        rows.append({"security_id": "AAA111", "as_of_date": d, "clean_price": 99.0, "oas_bps": 120.0})
        rows.append({"security_id": "BBB222", "as_of_date": d, "clean_price": 101.0, "oas_bps": 90.0})
    return pd.DataFrame(rows)


@pytest.fixture
def holdings() -> pd.DataFrame:
    """Market values are exactly par x price / 100 so the consistency rule is silent."""
    rows = []
    for d in MONTH_ENDS:
        db = (pd.Timestamp(d) + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        rows.append(
            {
                "as_of_date": d,
                "security_id": "AAA111",
                "par_amount": 1_000_000,
                "book_value": 985_000.0,
                "market_value": 990_000.0,   # 1,000,000 x 99.00 / 100
                "database_date": db,
            }
        )
        rows.append(
            {
                "as_of_date": d,
                "security_id": "BBB222",
                "par_amount": 2_000_000,
                "book_value": 1_990_000.0,
                "market_value": 2_020_000.0,  # 2,000,000 x 101.00 / 100
                "database_date": db,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def transactions() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_id": "T1",
                "trade_date": "2031-02-10",
                "settlement_date": "2031-02-12",
                "security_id": "AAA111",
                "trade_type": "BUY",
                "par_amount": 500_000,
                "price": 99.5,
            },
            {
                "trade_id": "T2",
                "trade_date": "2031-03-05",
                "settlement_date": "2031-03-07",
                "security_id": "BBB222",
                "trade_type": "SELL",
                "par_amount": 300_000,
                "price": 100.8,
            },
        ]
    )


def codes(findings) -> list[str]:
    """Rule codes emitted, for concise assertions."""
    return [f.rule_code for f in findings]


def only(findings, code: str):
    """The single finding with this code; fails the test if there isn't exactly one."""
    hits = [f for f in findings if f.rule_code == code]
    assert len(hits) == 1, f"expected exactly one {code}, got {codes(findings)}"
    return hits[0]


def px(df: pd.DataFrame, security_id: str, as_of_date: str) -> float:
    """Look up a cleaned price by business key.

    The cleaners sort and reset the index, so positional access to an output frame
    silently reads the wrong row. Always address output rows by their key.
    """
    hit = df[
        (df["security_id"] == security_id)
        & (df["as_of_date"] == pd.Timestamp(as_of_date))
    ]
    assert len(hit) == 1, f"expected one row for {security_id}/{as_of_date}, got {len(hit)}"
    return float(hit["clean_price"].iloc[0])
