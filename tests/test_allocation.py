"""Tests for allocation mix, shift attribution, and the weighted metrics.

The trap this file is mostly written to guard against: reading a weight change as
if it were a value change. A sector can lose weight while gaining value, purely
because the rest of the portfolio grew faster. Several tests below construct
exactly that situation and assert the two are reported separately.
"""

from __future__ import annotations

import pandas as pd
import pytest

from portfolio.analytics.allocation import (
    allocation_shift,
    rating_mix,
    sector_mix,
    sector_month_detail,
    sector_month_impact,
    worst_sector_month,
)
from portfolio.analytics.metrics import (
    monthly_weighted_metrics,
    security_history,
    weighted_metrics_by_dimension,
)

JAN = pd.Timestamp("2031-01-31")
FEB = pd.Timestamp("2031-02-28")


def row(date, sid, par, price, sector="Industrials", rating="BBB", oas=100.0, coupon=5.0):
    return {
        "as_of_date": date,
        "security_id": sid,
        "par_amount": float(par),
        "book_value": float(par) * 0.99,
        "market_value": float(par) * float(price) / 100.0,
        "clean_price": float(price),
        "price": float(price),
        "price_source": "mark",
        "market_value_imputed": False,
        "oas_bps": oas,
        "coupon_pct": coupon,
        "sector": sector,
        "rating": rating,
        "description": f"{sid} bond",
    }


# ---------------------------------------------------------------------------
# Mix
# ---------------------------------------------------------------------------


def test_sector_weights_sum_to_one_hundred_per_month():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "B", 3_000_000, 100.0, sector="Utilities"),
            row(FEB, "A", 1_000_000, 100.0, sector="Energy"),
            row(FEB, "B", 1_000_000, 100.0, sector="Utilities"),
        ]
    )
    mix = sector_mix(pos)
    for _, g in mix.groupby("as_of_date"):
        assert g["weight_pct"].sum() == pytest.approx(100.0)

    jan_energy = mix[(mix["as_of_date"] == JAN) & (mix["sector"] == "Energy")].iloc[0]
    assert jan_energy["weight_pct"] == pytest.approx(25.0)


def test_rating_mix_groups_by_rating():
    pos = pd.DataFrame(
        [row(JAN, "A", 1_000_000, 100.0, rating="AAA"), row(JAN, "B", 1_000_000, 100.0, rating="BB")]
    )
    mix = rating_mix(pos)
    assert set(mix["rating"]) == {"AAA", "BB"}
    assert mix["weight_pct"].tolist() == pytest.approx([50.0, 50.0])


# ---------------------------------------------------------------------------
# Shift attribution — weight change vs value change
# ---------------------------------------------------------------------------


def test_a_sector_can_lose_weight_while_gaining_value():
    """The central trap. Energy's value rises 10%, but Utilities triples, so
    Energy's weight falls. Reporting only the weight change would suggest Energy
    shrank, which is false."""
    pos = pd.DataFrame(
        [
            row(JAN, "E", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "U", 1_000_000, 100.0, sector="Utilities"),
            row(FEB, "E", 1_100_000, 100.0, sector="Energy"),
            row(FEB, "U", 3_000_000, 100.0, sector="Utilities"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=None).set_index("sector")
    energy = shift.loc["Energy"]

    assert energy["weight_change_pct"] < 0        # weight fell
    assert energy["mv_change"] > 0                # value rose
    assert energy["mv_change"] == pytest.approx(100_000.0)


def test_shift_driver_is_trading_when_par_moved():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy"),
            row(FEB, "A", 2_000_000, 100.0, sector="Energy"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=None).set_index("sector")
    assert shift.loc["Energy", "driver"] == "trading"
    assert shift.loc["Energy", "trading_effect"] == pytest.approx(1_000_000.0)


def test_shift_driver_is_market_when_only_price_moved():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "B", 1_000_000, 100.0, sector="Utilities"),
            row(FEB, "A", 1_000_000, 80.0, sector="Energy"),
            row(FEB, "B", 1_000_000, 100.0, sector="Utilities"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=None).set_index("sector")
    assert shift.loc["Energy", "driver"] == "market"
    assert shift.loc["Energy", "market_effect"] == pytest.approx(-200_000.0)


def test_shift_effects_reconcile_to_the_value_change():
    """Same additivity guarantee as Q1, at sector level."""
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "B", 2_000_000, 95.0, sector="Utilities"),
            row(FEB, "A", 1_500_000, 92.0, sector="Energy"),
            row(FEB, "B", 1_000_000, 97.0, sector="Utilities"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=None)
    total = shift["price_effect"] + shift["trading_effect"] + shift["interaction_effect"]
    assert (shift["mv_change"] - total).abs().max() == pytest.approx(0.0, abs=1e-6)


def test_a_sector_entering_the_portfolio_is_handled_as_zero_not_missing():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy"),
            row(FEB, "A", 1_000_000, 100.0, sector="Energy"),
            row(FEB, "N", 1_000_000, 100.0, sector="CMBS"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=None).set_index("sector")
    assert shift.loc["CMBS", "start_weight_pct"] == pytest.approx(0.0)
    assert shift.loc["CMBS", "start_mv"] == pytest.approx(0.0)
    assert shift.loc["CMBS", "mv_change"] == pytest.approx(1_000_000.0)


def test_shift_ranks_by_absolute_weight_change():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Big"),
            row(JAN, "B", 1_000_000, 100.0, sector="Small"),
            row(FEB, "A", 5_000_000, 100.0, sector="Big"),
            row(FEB, "B", 1_100_000, 100.0, sector="Small"),
        ]
    )
    shift = allocation_shift(pos, "sector", top_n=1)
    assert len(shift) == 1
    assert shift.iloc[0]["sector"] == "Big"


# ---------------------------------------------------------------------------
# Q3: worst sector-month
# ---------------------------------------------------------------------------


def test_worst_sector_month_finds_the_price_collapse():
    pos = pd.DataFrame(
        [
            row(JAN, "E1", 1_000_000, 100.0, sector="Energy", oas=200.0),
            row(JAN, "E2", 1_000_000, 100.0, sector="Energy", oas=200.0),
            row(JAN, "U1", 1_000_000, 100.0, sector="Utilities"),
            row(JAN, "U2", 1_000_000, 100.0, sector="Utilities"),
            row(FEB, "E1", 1_000_000, 90.0, sector="Energy", oas=350.0),
            row(FEB, "E2", 1_000_000, 92.0, sector="Energy", oas=340.0),
            row(FEB, "U1", 1_000_000, 100.5, sector="Utilities"),
            row(FEB, "U2", 1_000_000, 100.5, sector="Utilities"),
        ]
    )
    worst = worst_sector_month(pos).iloc[0]
    assert worst["sector"] == "Energy"
    assert worst["as_of_date"] == FEB
    assert worst["price_change_pct"] == pytest.approx(-9.0)
    # Spread widening is what marks this as credit rather than rates.
    # Market-value weighted, so NOT the equal-weighted mean of 350 and 340:
    #   Feb = (350 x 900,000 + 340 x 920,000) / 1,820,000 = 344.945
    #   Jan = 200.0  (equal market values)
    assert worst["oas_change_bps"] == pytest.approx(144.945, abs=1e-3)


def test_single_security_buckets_are_kept_out_of_the_ranking():
    """A one-bond 'sector average' is that bond's own price move. It must not
    outrank a real sector on idiosyncratic noise — but it stays in the detail."""
    pos = pd.DataFrame(
        [
            row(JAN, "SOLO", 1_000_000, 100.0, sector="Unclassified"),
            row(FEB, "SOLO", 1_000_000, 50.0, sector="Unclassified"),   # -50%
            row(JAN, "E1", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "E2", 1_000_000, 100.0, sector="Energy"),
            row(FEB, "E1", 1_000_000, 95.0, sector="Energy"),
            row(FEB, "E2", 1_000_000, 95.0, sector="Energy"),
        ]
    )
    ranked = worst_sector_month(pos)
    assert ranked.iloc[0]["sector"] == "Energy"
    assert "Unclassified" not in set(ranked["sector"])

    # Nothing is hidden: the detail still carries it.
    assert "Unclassified" in set(sector_month_detail(pos)["sector"])
    # And it can be ranked on request.
    assert worst_sector_month(pos, min_positions=1).iloc[0]["sector"] == "Unclassified"


def test_oas_is_market_value_weighted_not_equal_weighted():
    """A large position's spread should dominate the sector's OAS."""
    pos = pd.DataFrame(
        [
            row(JAN, "BIG", 9_000_000, 100.0, sector="Energy", oas=100.0),
            row(JAN, "SMALL", 1_000_000, 100.0, sector="Energy", oas=1_100.0),
        ]
    )
    detail = sector_month_detail(pos).iloc[0]
    # MV-weighted: (9 x 100 + 1 x 1100) / 10 = 200. Equal-weighted would be 600.
    assert detail["oas_bps_wavg"] == pytest.approx(200.0)
    assert detail["avg_price"] == pytest.approx(100.0)


def test_sector_month_impact_reports_share_of_the_portfolio_move():
    pos = pd.DataFrame(
        [
            row(JAN, "E", 1_000_000, 100.0, sector="Energy"),
            row(JAN, "U", 1_000_000, 100.0, sector="Utilities"),
            row(FEB, "E", 1_000_000, 80.0, sector="Energy"),      # -200,000
            row(FEB, "U", 1_000_000, 100.0, sector="Utilities"),  #        0
        ]
    )
    imp = sector_month_impact(pos, "Energy", FEB)
    assert imp["sector_mv_change"] == pytest.approx(-200_000.0)
    assert imp["portfolio_mv_change"] == pytest.approx(-200_000.0)
    assert imp["share_of_portfolio_change_pct"] == pytest.approx(100.0)
    assert imp["securities_affected"] == 1


# ---------------------------------------------------------------------------
# Bonus: weighted metrics
# ---------------------------------------------------------------------------


def test_coupon_is_par_weighted_by_default():
    """Coupon is paid on par, so par weighting is the only basis under which the
    weighted average times total par reproduces actual income.

    Par: equal par, so (2 + 6) / 2                              = 4.00%
    MV:  the 2% bond trades at 80 and the 6% at 120, so
         (2 x 800,000 + 6 x 1,200,000) / 2,000,000               = 4.40%

    Market-value weighting pulls the average toward the high-coupon premium bond,
    describing no actual cash flow: par weighting times total par reproduces real
    income, market-value weighting does not.
    """
    pos = pd.DataFrame(
        [
            row(JAN, "LOW", 1_000_000, 80.0, coupon=2.0),
            row(JAN, "HIGH", 1_000_000, 120.0, coupon=6.0),
        ]
    )
    m = monthly_weighted_metrics(pos).iloc[0]
    assert m["coupon_pct_wavg"] == pytest.approx(4.0)
    assert m["coupon_pct_wavg_by_mv"] == pytest.approx(4.4)


def test_oas_is_market_value_weighted_by_default():
    """OAS is a spread on capital at risk, so market value is the right basis.

    MV:  80 x 500 + 120 x 100, over 200 -> 260bp
    Par: equal par -> 300bp
    """
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 80.0, oas=500.0),
            row(JAN, "B", 1_000_000, 120.0, oas=100.0),
        ]
    )
    m = monthly_weighted_metrics(pos).iloc[0]
    assert m["oas_bps_wavg"] == pytest.approx(260.0)
    assert m["oas_bps_wavg_by_par"] == pytest.approx(300.0)


def test_coverage_reports_how_much_of_the_portfolio_the_average_speaks_for():
    """An average over 50% of the portfolio is a different claim from one over
    100%, and the difference must be visible rather than implied."""
    pos = pd.DataFrame(
        [row(JAN, "A", 1_000_000, 100.0, oas=200.0), row(JAN, "B", 1_000_000, 100.0)]
    )
    pos.loc[1, "oas_bps"] = None
    m = monthly_weighted_metrics(pos).iloc[0]
    assert m["oas_coverage_pct"] == pytest.approx(50.0)
    assert m["coupon_coverage_pct"] == pytest.approx(100.0)
    assert m["oas_bps_wavg"] == pytest.approx(200.0)


def test_an_empty_basis_yields_nan_not_zero():
    """Zero is a claim about the portfolio; NaN admits the number is unknown."""
    pos = pd.DataFrame([row(JAN, "A", 1_000_000, 100.0)])
    pos.loc[0, "oas_bps"] = None
    m = monthly_weighted_metrics(pos).iloc[0]
    assert pd.isna(m["oas_bps_wavg"])
    assert m["oas_coverage_pct"] == pytest.approx(0.0)


def test_weighted_metrics_by_dimension_splits_by_sector():
    pos = pd.DataFrame(
        [
            row(JAN, "A", 1_000_000, 100.0, sector="Energy", coupon=6.0),
            row(JAN, "B", 1_000_000, 100.0, sector="Utilities", coupon=3.0),
        ]
    )
    out = weighted_metrics_by_dimension(pos, "sector").set_index("sector")
    assert out.loc["Energy", "coupon_pct_wavg"] == pytest.approx(6.0)
    assert out.loc["Utilities", "coupon_pct_wavg"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


def test_security_history_returns_a_chronological_series_with_changes():
    pos = pd.DataFrame(
        [
            row(FEB, "A", 1_000_000, 90.0, oas=150.0),
            row(JAN, "A", 1_000_000, 100.0, oas=100.0),
            row(JAN, "OTHER", 1_000_000, 100.0),
        ]
    )
    hist = security_history(pos, "A")
    assert list(hist["as_of_date"]) == [JAN, FEB]
    assert hist.iloc[1]["price_change_pct"] == pytest.approx(-10.0)
    assert hist.iloc[1]["oas_change_bps"] == pytest.approx(50.0)
    assert pd.isna(hist.iloc[0]["price_change_pct"])
