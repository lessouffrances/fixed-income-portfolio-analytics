"""Tests for the market-value attribution.

The arithmetic is the asset here, so it is tested against hand-computed figures
rather than against whatever the code currently produces. Every fixture below has
numbers chosen so the expected answer can be worked out on paper.

The property that matters most is additivity: price + trading + interaction must
equal the market-value change exactly, for every month and every security. An
attribution that does not add up is worse than no attribution, because it looks
authoritative.
"""

from __future__ import annotations

import pandas as pd
import pytest

from portfolio.analytics.attribution import (
    decompose_positions,
    largest_move,
    monthly_attribution,
    portfolio_overview,
    reconcile_trading,
    security_price_returns,
    trading_from_transactions,
)

JAN = pd.Timestamp("2031-01-31")
FEB = pd.Timestamp("2031-02-28")
MAR = pd.Timestamp("2031-03-31")


def pos_row(date, sid, par, price, **kw):
    """One position row. market_value is derived so the panel is self-consistent."""
    return {
        "as_of_date": date,
        "security_id": sid,
        "par_amount": float(par),
        "price": float(price),
        "market_value": float(par) * float(price) / 100.0,
        "clean_price": float(price),
        "sector": kw.get("sector", "Industrials"),
        "rating": kw.get("rating", "BBB"),
        "description": kw.get("description", f"{sid} bond"),
        "oas_bps": kw.get("oas_bps", 100.0),
        "coupon_pct": kw.get("coupon_pct", 5.0),
    }


# ---------------------------------------------------------------------------
# Pure price move: par unchanged
# ---------------------------------------------------------------------------


def test_price_only_move_is_attributed_entirely_to_price():
    """1,000,000 par held flat, price 100 -> 98.

    MV: 1,000,000 -> 980,000, so ΔMV = -20,000.
    Price effect = 1,000,000 x (98 - 100) / 100 = -20,000. Trading = 0.
    """
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_000_000, 98.0)])
    att = monthly_attribution(pos)
    row = att[att["as_of_date"] == FEB].iloc[0]

    assert row["mv_change"] == pytest.approx(-20_000.0)
    assert row["price_effect"] == pytest.approx(-20_000.0)
    assert row["trading_effect"] == pytest.approx(0.0)
    assert row["interaction_effect"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Pure trading: price unchanged
# ---------------------------------------------------------------------------


def test_par_only_move_is_attributed_entirely_to_trading():
    """Par 1,000,000 -> 1,500,000 at a flat price of 100.

    ΔMV = +500,000, all trading, no price effect and no interaction.
    """
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_500_000, 100.0)])
    row = monthly_attribution(pos).iloc[0]

    assert row["mv_change"] == pytest.approx(500_000.0)
    assert row["trading_effect"] == pytest.approx(500_000.0)
    assert row["price_effect"] == pytest.approx(0.0)
    assert row["interaction_effect"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Both move: the interaction term is real and must not be swallowed
# ---------------------------------------------------------------------------


def test_simultaneous_moves_produce_an_exact_three_way_split():
    """Par 1,000,000 -> 1,500,000 while price goes 100 -> 98.

    ΔMV        = 1,470,000 - 1,000,000        = +470,000
    price      = 1,000,000 x (-2) / 100       =  -20,000
    trading    =   500,000 x 100  / 100       = +500,000
    interaction=   500,000 x (-2) / 100       =  -10,000
                                                --------
                                                +470,000
    """
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_500_000, 98.0)])
    row = monthly_attribution(pos).iloc[0]

    assert row["mv_change"] == pytest.approx(470_000.0)
    assert row["price_effect"] == pytest.approx(-20_000.0)
    assert row["trading_effect"] == pytest.approx(500_000.0)
    assert row["interaction_effect"] == pytest.approx(-10_000.0)


def test_interaction_is_not_folded_into_either_side():
    """Guards the design choice. If a future change absorbed the cross term into
    trading, trading would read 490,000 and this test would catch it."""
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_500_000, 98.0)])
    row = monthly_attribution(pos).iloc[0]
    assert row["trading_effect"] == pytest.approx(500_000.0)
    assert row["interaction_effect"] != pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Additivity — the property that matters most
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "par2,price2",
    [
        (1_000_000, 100.0),   # nothing moves
        (0, 100.0),           # fully sold
        (2_500_000, 103.5),   # bought into a rally
        (400_000, 88.25),     # sold into a selloff
    ],
)
def test_the_decomposition_always_adds_up(par2, price2):
    pos = pd.DataFrame(
        [pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", par2, price2)]
    )
    d = decompose_positions(pos)
    total = d["price_effect"] + d["trading_effect"] + d["interaction_effect"]
    assert (d["mv_change"] - total).abs().max() == pytest.approx(0.0, abs=1e-6)
    assert d["unexplained"].abs().max() == pytest.approx(0.0, abs=1e-6)


def test_additivity_holds_across_a_multi_security_multi_month_panel():
    pos = pd.DataFrame(
        [
            pos_row(JAN, "A", 1_000_000, 100.0),
            pos_row(JAN, "B", 2_000_000, 95.0),
            pos_row(FEB, "A", 1_500_000, 98.0),
            pos_row(FEB, "B", 2_000_000, 96.5),
            pos_row(MAR, "A", 1_500_000, 99.0),
            pos_row(MAR, "B", 500_000, 94.0),
        ]
    )
    att = monthly_attribution(pos)
    residual = att["mv_change"] - (
        att["price_effect"] + att["trading_effect"] + att["interaction_effect"]
    )
    assert residual.abs().max() == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Positions entering and leaving
# ---------------------------------------------------------------------------


def test_a_new_position_is_entirely_trading():
    """A security absent in January and bought in February has no prior price, so
    the whole change must land in trading rather than vanishing into a null."""
    pos = pd.DataFrame(
        [
            pos_row(JAN, "A", 1_000_000, 100.0),
            pos_row(FEB, "A", 1_000_000, 100.0),
            pos_row(FEB, "NEW", 3_000_000, 99.0),
        ]
    )
    d = decompose_positions(pos)
    new = d[d["security_id"] == "NEW"].iloc[0]

    assert new["trading_effect"] == pytest.approx(2_970_000.0)
    assert new["price_effect"] == pytest.approx(0.0)
    assert new["mv_change"] == pytest.approx(2_970_000.0)


def test_a_fully_sold_position_is_entirely_trading():
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 0, 100.0)])
    row = monthly_attribution(pos).iloc[0]
    assert row["mv_change"] == pytest.approx(-1_000_000.0)
    assert row["trading_effect"] == pytest.approx(-1_000_000.0)


def test_the_first_month_has_no_attribution_row():
    """There is nothing to compare January against; inventing a zero row would
    imply a measured result where none exists."""
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_000_000, 99.0)])
    assert JAN not in set(monthly_attribution(pos)["as_of_date"])


# ---------------------------------------------------------------------------
# Overview and largest move
# ---------------------------------------------------------------------------


def test_portfolio_overview_totals_and_month_over_month_change():
    pos = pd.DataFrame(
        [
            pos_row(JAN, "A", 1_000_000, 100.0),
            pos_row(JAN, "B", 1_000_000, 100.0),
            pos_row(FEB, "A", 1_000_000, 90.0),
            pos_row(FEB, "B", 1_000_000, 100.0),
        ]
    )
    ov = portfolio_overview(pos)
    assert ov.iloc[0]["market_value"] == pytest.approx(2_000_000.0)
    assert ov.iloc[1]["market_value"] == pytest.approx(1_900_000.0)
    assert ov.iloc[1]["mv_change"] == pytest.approx(-100_000.0)
    assert ov.iloc[1]["mv_change_pct"] == pytest.approx(-5.0)
    assert pd.isna(ov.iloc[0]["mv_change"])


def test_largest_move_picks_the_biggest_absolute_change_not_the_biggest_gain():
    """A large loss must beat a small gain — the question asks for the largest
    absolute change."""
    pos = pd.DataFrame(
        [
            pos_row(JAN, "A", 1_000_000, 100.0),
            pos_row(FEB, "A", 1_000_000, 80.0),   # -200,000
            pos_row(MAR, "A", 1_000_000, 82.0),   #  +20,000
        ]
    )
    assert largest_move(pos)["as_of_date"] == FEB


# ---------------------------------------------------------------------------
# Reconciliation against the transactions file
# ---------------------------------------------------------------------------


def _trades(rows):
    df = pd.DataFrame(rows)
    sign = df["trade_type"].map({"BUY": 1, "SELL": -1, "MATURITY": -1})
    df["signed_par"] = df["par_amount"] * sign
    df["cash_flow"] = df["signed_par"] * df["price"] / 100.0
    df["as_of_date"] = df["trade_date"] + pd.offsets.MonthEnd(0)
    return df


def test_trading_from_transactions_signs_each_type_correctly():
    trades = _trades(
        [
            {"trade_date": pd.Timestamp("2031-02-05"), "security_id": "A",
             "trade_type": "BUY", "par_amount": 1_000_000.0, "price": 100.0},
            {"trade_date": pd.Timestamp("2031-02-10"), "security_id": "B",
             "trade_type": "SELL", "par_amount": 500_000.0, "price": 100.0},
            {"trade_date": pd.Timestamp("2031-02-20"), "security_id": "C",
             "trade_type": "MATURITY", "par_amount": 200_000.0, "price": 100.0},
        ]
    )
    out = trading_from_transactions(trades).iloc[0]
    assert out["buys"] == pytest.approx(1_000_000.0)
    assert out["sells"] == pytest.approx(-500_000.0)
    assert out["maturities"] == pytest.approx(-200_000.0)
    assert out["net_cash_flow"] == pytest.approx(300_000.0)
    assert out["trade_count"] == 3


def test_reconciliation_is_clean_when_every_par_move_has_a_trade():
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_500_000, 100.0)])
    trades = _trades(
        [{"trade_date": pd.Timestamp("2031-02-14"), "security_id": "A",
          "trade_type": "BUY", "par_amount": 500_000.0, "price": 100.0}]
    )
    assert reconcile_trading(pos, trades).iloc[0]["difference"] == pytest.approx(0.0)


def test_reconciliation_surfaces_par_that_moved_without_a_trade():
    """The check that caught the phantom position in the real extract: par appears
    with no corresponding trade, so the two measures of trading disagree."""
    pos = pd.DataFrame([pos_row(JAN, "A", 1_000_000, 100.0), pos_row(FEB, "A", 1_500_000, 100.0)])
    empty = trading_from_transactions(pd.DataFrame())
    assert empty.empty

    rec = reconcile_trading(pos, pd.DataFrame())
    assert rec.iloc[0]["trading_effect"] == pytest.approx(500_000.0)
    assert rec.iloc[0]["net_cash_flow"] == pytest.approx(0.0)
    assert rec.iloc[0]["difference"] == pytest.approx(500_000.0)


# ---------------------------------------------------------------------------
# Q4: worst full-year price returns
# ---------------------------------------------------------------------------


def test_price_returns_rank_worst_first_and_isolate_price():
    pos = pd.DataFrame(
        [
            pos_row(JAN, "GOOD", 1_000_000, 100.0),
            pos_row(FEB, "GOOD", 1_000_000, 105.0),
            pos_row(MAR, "GOOD", 1_000_000, 110.0),
            pos_row(JAN, "BAD", 1_000_000, 100.0),
            pos_row(FEB, "BAD", 1_000_000, 90.0),
            pos_row(MAR, "BAD", 1_000_000, 80.0),
        ]
    )
    out = security_price_returns(pos, top_n=2)
    assert list(out["security_id"]) == ["BAD", "GOOD"]

    bad = out.iloc[0]
    assert bad["price_return_pct"] == pytest.approx(-20.0)
    # Price impact = 1,000,000 x (-10)/100 twice = -200,000
    assert bad["price_impact"] == pytest.approx(-200_000.0)


def test_price_impact_respects_the_par_actually_held_each_month():
    """A position halved partway through did not suffer the later move at full
    size. A naive 'January par x full-year price change' would report -200,000
    here; the correct figure is -150,000."""
    pos = pd.DataFrame(
        [
            pos_row(JAN, "A", 1_000_000, 100.0),
            pos_row(FEB, "A", 500_000, 90.0),    # -10 on 1,000,000 = -100,000
            pos_row(MAR, "A", 500_000, 80.0),    # -10 on   500,000 =  -50,000
        ]
    )
    out = security_price_returns(pos, held_all_year=False, top_n=1)
    assert out.iloc[0]["price_impact"] == pytest.approx(-150_000.0)


def test_held_all_year_filter_excludes_partial_year_positions():
    pos = pd.DataFrame(
        [
            pos_row(JAN, "FULL", 1_000_000, 100.0),
            pos_row(FEB, "FULL", 1_000_000, 95.0),
            pos_row(MAR, "FULL", 1_000_000, 90.0),
            pos_row(FEB, "PARTIAL", 1_000_000, 100.0),
            pos_row(MAR, "PARTIAL", 1_000_000, 50.0),   # a far worse return
        ]
    )
    out = security_price_returns(pos, held_all_year=True, top_n=5)
    assert list(out["security_id"]) == ["FULL"]

    # Without the filter the partial-year position dominates the ranking.
    both = security_price_returns(pos, held_all_year=False, top_n=5)
    assert "PARTIAL" in set(both["security_id"])
