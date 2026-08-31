"""Tunable bounds for the validation rules.

Every rule reads its limits from here rather than embedding literals, for two
reasons. First, the assignment requires the data-quality page to work on a
*different* extract with similar problems — so no rule may key off a specific
security id, date, or value seen in this file. Second, it makes the rules
testable: a test can tighten a band to force a rule to fire instead of having to
manufacture extreme synthetic data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    # --- Price conventions -------------------------------------------------
    # Prices in this extract are quoted per 100 of par ("clean price"). A bond
    # trading at a plausible level sits inside this band; anything outside is
    # either a data error or a distressed/defaulted name worth a human look.
    price_band_lo: float = 20.0
    price_band_hi: float = 150.0

    # A price below this is not a credible per-100 quote. It is almost always a
    # decimal-scale error: the value was written as a fraction of par (0.9750)
    # instead of per-100 (97.50). We only repair when x100 lands back in band.
    price_scale_detect_max: float = 5.0
    price_scale_factor: float = 100.0

    # Month-over-month price move large enough to be worth surfacing.
    #
    # Calibrated against the extract's own distribution rather than picked round:
    # once scale errors are repaired, the 1st/99th percentile monthly move is
    # -6.6%/+2.3% and the largest genuine move is -9.6%. A 15% bound never fires;
    # 5% isolates the tail without drowning the page in normal noise.
    #
    # NOTE: deliberately flag-only, at INFO severity. A real credit event produces
    # exactly this signature — the 2025 Energy selloff trips it across the whole
    # sector — so the rule points a human at the month and never "repairs" anything.
    price_jump_pct: float = 5.0

    # --- Spreads -----------------------------------------------------------
    # OAS is in basis points. Negative OAS is possible in exotic cases but not
    # in this dataset's asset classes, so treat it as an error.
    oas_min_bps: float = 0.0
    oas_max_bps: float = 2000.0

    # --- Coupons -----------------------------------------------------------
    coupon_min_pct: float = 0.0
    coupon_max_pct: float = 20.0

    # --- Cross-file consistency -------------------------------------------
    # market_value should equal par_amount * clean_price / 100. Allow this much
    # discrepancy in price points before flagging: accrued-interest conventions
    # and rounding in the source system produce small legitimate differences.
    mv_price_tolerance_pts: float = 0.50

    # Redemptions settle at par. Tolerance in price points.
    maturity_price_tolerance_pts: float = 0.01
    maturity_expected_price: float = 100.0

    # --- Sensible defaults for missing reference attributes ---------------
    # Chosen so they are visibly placeholders in the UI. Silently bucketing an
    # unclassified bond into a real sector would corrupt the allocation view,
    # which is one of the required screens.
    unknown_sector: str = "Unclassified"
    unknown_rating: str = "NR"
    unknown_asset_class: str = "Unclassified"
