"""Tests for the cleaning rules.

Two properties are being defended here, and they matter more than line coverage:

1. Each rule fires on the problem shape it targets, and takes the documented
   action (repair / exclude / impute / flag) rather than silently mutating data.
2. Clean input produces no findings. A rule that cries wolf on good data makes
   the data-quality page useless, so every rule has a negative case.
"""

from __future__ import annotations

import pandas as pd
import pytest

from portfolio.load.findings import Action, Severity
from portfolio.load.pipeline import clean_extract
from portfolio.load.validate import (
    clean_holdings,
    clean_marks,
    clean_security_master,
    clean_transactions,
)

from conftest import codes, only, px

# ---------------------------------------------------------------------------
# Baseline: clean data must produce silence
# ---------------------------------------------------------------------------


def test_clean_extract_on_clean_data_emits_nothing(
    master, holdings, marks, transactions, th
):
    res = clean_extract(master, holdings, marks, transactions, th)
    assert res.findings == [], f"unexpected findings: {[str(f) for f in res.findings]}"
    assert len(res.holdings) == len(holdings)
    assert len(res.marks) == len(marks)
    assert len(res.transactions) == len(transactions)


def test_clean_extract_preserves_total_market_value_when_clean(
    master, holdings, marks, transactions, th
):
    res = clean_extract(master, holdings, marks, transactions, th)
    assert res.holdings["market_value"].sum() == pytest.approx(
        holdings["market_value"].sum()
    )


# ---------------------------------------------------------------------------
# Security master
# ---------------------------------------------------------------------------


def test_sm002_missing_sector_defaults_and_keeps_row(master, th):
    master.loc[0, "sector"] = None
    out, f = clean_security_master(master, th)
    finding = only(f, "SM002")
    assert finding.action is Action.DEFAULTED
    assert finding.replacement == th.unknown_sector
    # The position must survive: it still belongs in portfolio market value.
    assert len(out) == 2
    assert out.loc[0, "sector"] == th.unknown_sector


def test_sm003_missing_rating_defaults_to_nr(master, th):
    master.loc[1, "rating"] = None
    out, f = clean_security_master(master, th)
    assert only(f, "SM003").replacement == th.unknown_rating
    assert out.loc[1, "rating"] == "NR"


def test_sm001_duplicate_security_id_deduplicated(master, th):
    dup = pd.concat([master, master.iloc[[0]]], ignore_index=True)
    out, f = clean_security_master(dup, th)
    assert only(f, "SM001").action is Action.DEDUPLICATED
    assert out["security_id"].is_unique


def test_sm004_maturity_before_issue_is_flagged_not_repaired(master, th):
    master.loc[0, "maturity_date"] = "2019-01-01"  # before its 2020 issue date
    out, f = clean_security_master(master, th)
    finding = only(f, "SM004")
    assert finding.action is Action.FLAGGED
    # Neither date may be silently rewritten — we cannot know which one is wrong.
    assert out.loc[0, "maturity_date"] == pd.Timestamp("2019-01-01")


def test_sm006_implausible_coupon_flagged(master, th):
    master.loc[0, "coupon_pct"] = 45.0
    _, f = clean_security_master(master, th)
    assert only(f, "SM006").action is Action.FLAGGED


# ---------------------------------------------------------------------------
# Marks — scale error is the highest-value repair, so it gets the most cases
# ---------------------------------------------------------------------------


def test_mk002_par_fraction_price_repaired_by_factor_100(marks, master, th):
    marks.loc[2, "clean_price"] = 0.9875  # AAA111 / 2031-02-28; should be 98.75
    out, f = clean_marks(marks, set(master["security_id"]), th)
    finding = only(f, "MK002")
    assert finding.action is Action.REPAIRED
    assert finding.severity is Severity.ERROR
    assert px(out, "AAA111", "2031-02-28") == pytest.approx(98.75)


def test_mk002_does_not_fire_when_x100_would_leave_the_band(marks, master, th):
    """A price of 1.6 x100 = 160, outside the plausible band, so this is not a
    recoverable scale error. It must be flagged (MK003), never multiplied."""
    marks.loc[2, "clean_price"] = 1.6
    out, f = clean_marks(marks, set(master["security_id"]), th)
    assert "MK002" not in codes(f)
    assert "MK003" in codes(f)
    assert px(out, "AAA111", "2031-02-28") == pytest.approx(1.6)


def test_mk002_leaves_a_genuinely_distressed_price_alone(marks, master, th):
    """25.0 is a plausible distressed price, not a scale error."""
    marks.loc[2, "clean_price"] = 25.0
    out, f = clean_marks(marks, set(master["security_id"]), th)
    assert "MK002" not in codes(f)
    assert px(out, "AAA111", "2031-02-28") == pytest.approx(25.0)


def test_mk003_price_outside_band_flagged_not_altered(marks, master, th):
    marks.loc[0, "clean_price"] = 400.0
    out, f = clean_marks(marks, set(master["security_id"]), th)
    assert only(f, "MK003").action is Action.FLAGGED
    assert px(out, "AAA111", "2031-01-31") == pytest.approx(400.0)


def test_mk005_negative_oas_flagged(marks, master, th):
    marks.loc[0, "oas_bps"] = -50.0
    _, f = clean_marks(marks, set(master["security_id"]), th)
    assert only(f, "MK005").action is Action.FLAGGED


def test_mk006_mark_for_unknown_security_excluded(marks, master, th):
    orphan = pd.DataFrame(
        [{"security_id": "ZZZ999", "as_of_date": "2031-01-31", "clean_price": 99.0, "oas_bps": 100.0}]
    )
    out, f = clean_marks(
        pd.concat([marks, orphan], ignore_index=True), set(master["security_id"]), th
    )
    assert only(f, "MK006").action is Action.EXCLUDED
    assert "ZZZ999" not in set(out["security_id"])


def test_mk007_large_move_is_informational_only(marks, master, th):
    """A big move must be surfaced but never corrected — real credit events look
    exactly like this, and Q3 depends on the genuine one surviving intact."""
    marks.loc[marks.index[-1], "clean_price"] = 80.0  # ~-20% on BBB222
    out, f = clean_marks(marks, set(master["security_id"]), th)
    finding = only(f, "MK007")
    assert finding.severity is Severity.INFO
    assert finding.action is Action.FLAGGED
    assert out["clean_price"].max() == pytest.approx(101.0)
    assert 80.0 in out["clean_price"].values


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------


def _dupe_snapshot(holdings: pd.DataFrame, *, corrupt_value: float, later: bool):
    """Add a second snapshot for row 0's key, with a market value that does NOT
    reconcile to the mark. `later` controls whether the corrupt row is the newer load.
    """
    row = holdings.iloc[0].copy()
    base_db = pd.Timestamp(row["database_date"])
    row["database_date"] = (
        base_db + pd.Timedelta(days=10) if later else base_db - pd.Timedelta(days=1)
    ).strftime("%Y-%m-%d")
    row["market_value"] = corrupt_value
    return pd.concat([holdings, pd.DataFrame([row])], ignore_index=True)


def test_hl001_keeps_the_snapshot_that_reconciles_to_the_mark(
    holdings, marks, master, th
):
    """The core tie-break. The corrupt row is the LATER load, so a naive
    'latest database_date wins' rule would keep the wrong value."""
    df = _dupe_snapshot(holdings, corrupt_value=1_005_000.0, later=True)
    out, f = clean_holdings(df, *_cleaned_inputs(marks, master, th), th)
    finding = only(f, "HL001")
    assert finding.action is Action.DEDUPLICATED
    assert "reconciles" in finding.context["tie_break"]
    # The surviving value is the one consistent with par x price, not the newer one.
    kept = out[(out["security_id"] == "AAA111") & (out["as_of_date"] == pd.Timestamp("2031-01-31"))]
    assert len(kept) == 1
    assert kept["market_value"].iloc[0] == pytest.approx(990_000.0)


def test_hl001_reconciliation_wins_regardless_of_load_order(
    holdings, marks, master, th
):
    """Same corruption, but as the EARLIER load. The rule must still keep the
    reconciling row — proving it keys off the mark, not off arrival order."""
    df = _dupe_snapshot(holdings, corrupt_value=1_005_000.0, later=False)
    out, f = clean_holdings(df, *_cleaned_inputs(marks, master, th), th)
    only(f, "HL001")
    kept = out[(out["security_id"] == "AAA111") & (out["as_of_date"] == pd.Timestamp("2031-01-31"))]
    assert kept["market_value"].iloc[0] == pytest.approx(990_000.0)


def test_hl001_falls_back_to_latest_load_when_no_mark_exists(
    holdings, marks, master, th
):
    marks = marks[~((marks["security_id"] == "AAA111") & (marks["as_of_date"] == "2031-01-31"))]
    df = _dupe_snapshot(holdings, corrupt_value=1_005_000.0, later=True)
    out, f = clean_holdings(df, *_cleaned_inputs(marks, master, th), th)
    finding = only(f, "HL001")
    assert "latest database_date" in finding.context["tie_break"]
    kept = out[(out["security_id"] == "AAA111") & (out["as_of_date"] == pd.Timestamp("2031-01-31"))]
    assert kept["market_value"].iloc[0] == pytest.approx(1_005_000.0)


def test_hl001_no_duplicates_survive(holdings, marks, master, th):
    df = _dupe_snapshot(holdings, corrupt_value=1_005_000.0, later=True)
    out, _ = clean_holdings(df, *_cleaned_inputs(marks, master, th), th)
    assert not out.duplicated(["security_id", "as_of_date"]).any()


def test_hl002_sign_flip_repaired_when_book_value_positive(holdings, marks, master, th):
    holdings.loc[0, "par_amount"] = -1_000_000
    holdings.loc[0, "market_value"] = -990_000.0
    out, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    finding = only(f, "HL002")
    assert finding.action is Action.REPAIRED
    assert (out["par_amount"] > 0).all()
    assert (out["market_value"].dropna() > 0).all()


def test_hl002_genuine_short_is_flagged_not_repaired(holdings, marks, master, th):
    """Negative par AND negative book, with no positive par elsewhere for that
    security, reads as a real short position — we must not abs() it away."""
    holdings = holdings[holdings["security_id"] == "AAA111"].copy()
    holdings["par_amount"] = -1_000_000
    holdings["book_value"] = -985_000.0
    holdings["market_value"] = -990_000.0
    out, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    hl002 = [x for x in f if x.rule_code == "HL002"]
    assert hl002 and all(x.action is Action.FLAGGED for x in hl002)
    assert (out["par_amount"] < 0).all()


def test_hl003_orphan_holding_excluded(holdings, marks, master, th):
    row = holdings.iloc[0].copy()
    row["security_id"] = "ZZZ999"
    df = pd.concat([holdings, pd.DataFrame([row])], ignore_index=True)
    out, f = clean_holdings(df, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL003").action is Action.EXCLUDED
    assert "ZZZ999" not in set(out["security_id"])


def test_hl004_null_market_value_imputed_from_par_and_price(
    holdings, marks, master, th
):
    holdings.loc[0, "market_value"] = None
    out, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    finding = only(f, "HL004")
    assert finding.action is Action.IMPUTED
    assert out.loc[0, "market_value"] == pytest.approx(990_000.0)
    assert bool(out.loc[0, "market_value_imputed"]) is True


def test_hl004_imputation_is_not_double_flagged_as_inconsistent(
    holdings, marks, master, th
):
    """An imputed value is by construction equal to par x price, so HL006 must
    not then report it as inconsistent. Guards against a rule-ordering bug."""
    holdings.loc[0, "market_value"] = None
    _, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert "HL006" not in codes(f)


def test_hl005_null_market_value_with_no_mark_cannot_be_imputed(
    holdings, marks, master, th
):
    holdings.loc[0, "market_value"] = None
    marks = marks[~((marks["security_id"] == "AAA111") & (marks["as_of_date"] == "2031-01-31"))]
    out, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL005").action is Action.FLAGGED
    assert pd.isna(out.loc[0, "market_value"])


def test_hl006_inconsistent_market_value_flagged_and_reported_value_kept(
    holdings, marks, master, th
):
    holdings.loc[0, "market_value"] = 950_000.0  # implies 95.0 vs a 99.0 mark
    out, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL006").action is Action.FLAGGED
    assert out.loc[0, "market_value"] == pytest.approx(950_000.0)


def test_hl007_non_month_end_snapshot_flagged(holdings, marks, master, th):
    holdings.loc[0, "as_of_date"] = "2031-01-15"
    _, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL007").action is Action.FLAGGED


def test_hl008_database_date_before_as_of_date_flagged(holdings, marks, master, th):
    holdings.loc[0, "database_date"] = "2030-12-01"
    _, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL008").action is Action.FLAGGED


def test_hl009_position_after_maturity_flagged(holdings, marks, master, th):
    master.loc[master["security_id"] == "AAA111", "maturity_date"] = "2031-02-15"
    _, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    hl009 = [x for x in f if x.rule_code == "HL009"]
    # Feb 28, Mar 31 and Apr 30 all fall after a 15 Feb maturity.
    assert len(hl009) == 3
    assert {x.key["as_of_date"] for x in hl009} == {"2031-02-28", "2031-03-31", "2031-04-30"}


def test_hl010_held_position_without_a_mark_is_reported(holdings, marks, master, th):
    marks = marks[~((marks["security_id"] == "AAA111") & (marks["as_of_date"] == "2031-01-31"))]
    _, f = clean_holdings(holdings, *_cleaned_inputs(marks, master, th), th)
    assert only(f, "HL010").severity is Severity.INFO


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_tx001_identical_duplicate_trade_deduplicated(transactions, master, th):
    df = pd.concat([transactions, transactions.iloc[[0]]], ignore_index=True)
    out, f = clean_transactions(df, _clean_master(master, th), th)
    assert only(f, "TX001").action is Action.DEDUPLICATED
    assert out["trade_id"].is_unique


def test_tx002_conflicting_duplicate_trade_id_flagged_and_both_kept(
    transactions, master, th
):
    """Two different trades sharing an id is an identifier collision, not a
    double-send. Dropping one would silently lose a real cash flow."""
    row = transactions.iloc[0].copy()
    row["par_amount"] = 999_000  # same id, different details
    df = pd.concat([transactions, pd.DataFrame([row])], ignore_index=True)
    out, f = clean_transactions(df, _clean_master(master, th), th)
    assert only(f, "TX002").action is Action.FLAGGED
    assert (out["trade_id"] == "T1").sum() == 2


def test_tx003_orphan_trade_excluded_and_notional_disclosed(
    transactions, master, th
):
    row = transactions.iloc[0].copy()
    row["trade_id"], row["security_id"] = "T9", "ZZZ999"
    df = pd.concat([transactions, pd.DataFrame([row])], ignore_index=True)
    out, f = clean_transactions(df, _clean_master(master, th), th)
    finding = only(f, "TX003")
    assert finding.action is Action.EXCLUDED
    # The excluded notional must be reported, not just dropped.
    assert finding.context["total_par"] == pytest.approx(500_000.0)
    assert "ZZZ999" not in set(out["security_id"])


def test_tx004_settlement_before_trade_flagged(transactions, master, th):
    transactions.loc[0, "settlement_date"] = "2031-02-08"
    _, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert only(f, "TX004").action is Action.FLAGGED


def test_tx005_unknown_trade_type_excluded(transactions, master, th):
    transactions.loc[0, "trade_type"] = "TRANSFER"
    out, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert only(f, "TX005").action is Action.EXCLUDED
    assert len(out) == 1


def test_tx005_trade_type_is_normalised_before_validation(transactions, master, th):
    """Whitespace and case are formatting noise, not a data-quality problem."""
    transactions.loc[0, "trade_type"] = "  buy "
    out, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert "TX005" not in codes(f)
    assert out.loc[0, "trade_type"] == "BUY"


def test_tx006_non_positive_par_flagged(transactions, master, th):
    transactions.loc[0, "par_amount"] = 0
    _, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert only(f, "TX006").action is Action.FLAGGED


def test_tx008_maturity_not_at_par_flagged(transactions, master, th):
    transactions.loc[0, "trade_type"] = "MATURITY"
    transactions.loc[0, "trade_date"] = "2040-01-15"   # matches master maturity
    transactions.loc[0, "settlement_date"] = "2040-01-17"
    transactions.loc[0, "price"] = 97.0
    _, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert only(f, "TX008").action is Action.FLAGGED


def test_tx009_maturity_date_disagreeing_with_master_flagged(
    transactions, master, th
):
    transactions.loc[0, "trade_type"] = "MATURITY"
    transactions.loc[0, "price"] = 100.0
    _, f = clean_transactions(transactions, _clean_master(master, th), th)
    assert only(f, "TX009").action is Action.FLAGGED


# ---------------------------------------------------------------------------
# Portability: no rule may key off the delivered extract's ids or calendar
# ---------------------------------------------------------------------------


def test_rules_are_not_specific_to_the_delivered_extract(
    master, holdings, marks, transactions, th
):
    """Same problem shapes, different ids and a different year. Every rule must
    still fire — this is the assignment's 'hand your app a different extract'
    requirement, asserted rather than asserted-about."""
    marks.loc[2, "clean_price"] = 0.9875
    holdings.loc[0, "market_value"] = None
    row = transactions.iloc[0].copy()
    row["trade_id"], row["security_id"] = "T9", "QQQ000"
    transactions = pd.concat([transactions, pd.DataFrame([row])], ignore_index=True)
    master.loc[0, "sector"] = None

    res = clean_extract(master, holdings, marks, transactions, th)
    found = set(codes(res.findings))
    for expected in ("SM002", "MK002", "HL004", "TX003"):
        assert expected in found, f"{expected} did not fire on a renamed extract"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _clean_master(master: pd.DataFrame, th) -> pd.DataFrame:
    out, _ = clean_security_master(master, th)
    return out


def _cleaned_inputs(marks: pd.DataFrame, master: pd.DataFrame, th):
    """clean_holdings expects already-cleaned marks and master, in that order."""
    sec = _clean_master(master, th)
    mk, _ = clean_marks(marks, set(sec["security_id"]), th)
    return mk, sec
