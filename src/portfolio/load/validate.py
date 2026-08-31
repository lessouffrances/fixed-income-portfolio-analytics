"""Validation and cleaning rules for the four CSV extracts.

Design notes
------------
These functions are pure: DataFrame(s) in, cleaned DataFrame + list[Finding] out.
There is no database dependency anywhere in this module, which is what lets the
test suite cover the rules without standing up Postgres. `loader.py` handles all
the I/O and simply calls into here.

Rules are generic by construction. None of them reference a security id, a date,
or a magic value observed in this particular extract; every bound comes from
`Thresholds`. That is the assignment's explicit requirement for the data-quality
page: hand the app a different extract with similar problems and these same rules
must still find them.

Repair philosophy
-----------------
We repair only when the correct value is recoverable from the data itself with
high confidence (a decimal-scale slip, a sign flip contradicted by the adjacent
months, a market value derivable from par x price). Anything requiring a market
judgement is flagged, never silently rewritten — a "corrected" number that is
actually wrong is far more dangerous than a visible gap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .findings import Action, Finding, Severity
from .thresholds import Thresholds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_date(s: pd.Series) -> pd.Series:
    """Parse to datetime, turning unparseable values into NaT rather than raising.

    Bad dates are a data-quality finding, not a crash. Rules detect the NaTs.
    """
    return pd.to_datetime(s, errors="coerce")


def _is_month_end(s: pd.Series) -> pd.Series:
    """True where the date is the last calendar day of its own month.

    Derived from the date itself rather than compared against a hardcoded list of
    2025 month-ends, so the rule survives a differently-dated extract.
    """
    d = _to_date(s)
    return d.notna() & (d == d + pd.offsets.MonthEnd(0))


# ---------------------------------------------------------------------------
# Security master
# ---------------------------------------------------------------------------


def clean_security_master(
    df: pd.DataFrame, th: Thresholds
) -> tuple[pd.DataFrame, list[Finding]]:
    """Clean the reference data. Runs first: other files need it for orphan checks."""
    out = df.copy()
    findings: list[Finding] = []
    table = "security_master"

    # SM001 — duplicate primary key. Keep the first occurrence; a master file with
    # two rows for one id has no defensible tie-break, so we surface it loudly.
    dup_mask = out["security_id"].duplicated(keep="first")
    for _, r in out[dup_mask].iterrows():
        findings.append(
            Finding(
                rule_code="SM001",
                rule_title="Duplicate security_id in security master",
                severity=Severity.ERROR,
                action=Action.DEDUPLICATED,
                source_table=table,
                key={"security_id": r["security_id"]},
                message=(
                    "security_id appears more than once in the master file; kept the "
                    "first row and dropped the rest. Reference data must be unique per id."
                ),
            )
        )
    out = out[~dup_mask].copy()

    # SM002 — missing sector. Defaulted to a visible placeholder rather than
    # guessed from the issuer name, and never dropped: the position is real and
    # must still appear in portfolio totals even if we cannot classify it.
    for _, r in out[out["sector"].isna()].iterrows():
        findings.append(
            Finding(
                rule_code="SM002",
                rule_title="Missing sector on security master",
                severity=Severity.WARNING,
                action=Action.DEFAULTED,
                source_table=table,
                key={"security_id": r["security_id"]},
                column="sector",
                observed=None,
                replacement=th.unknown_sector,
                message=(
                    f"sector is null; set to '{th.unknown_sector}'. The position still "
                    "counts toward portfolio market value, and appears as its own bucket "
                    "in the allocation view so the gap is visible rather than hidden."
                ),
                context={"issuer": r.get("issuer"), "description": r.get("description")},
            )
        )
    out["sector"] = out["sector"].fillna(th.unknown_sector)

    # SM003 — missing rating. Same treatment. 'NR' (not rated) is the standard
    # fixed-income convention, so it reads naturally in the rating allocation chart.
    for _, r in out[out["rating"].isna()].iterrows():
        findings.append(
            Finding(
                rule_code="SM003",
                rule_title="Missing rating on security master",
                severity=Severity.WARNING,
                action=Action.DEFAULTED,
                source_table=table,
                key={"security_id": r["security_id"]},
                column="rating",
                observed=None,
                replacement=th.unknown_rating,
                message=(
                    f"rating is null; set to '{th.unknown_rating}'. Not inferred from the "
                    "issuer's other bonds — rating is instrument-level, and seniority "
                    "differences make that inference unsafe."
                ),
                context={"issuer": r.get("issuer")},
            )
        )
    out["rating"] = out["rating"].fillna(th.unknown_rating)
    out["asset_class"] = out["asset_class"].fillna(th.unknown_asset_class)

    # SM004 — maturity at or before issue. Cannot be repaired without knowing
    # which date is wrong, so flag and leave both in place.
    iss, mat = _to_date(out["issue_date"]), _to_date(out["maturity_date"])
    for _, r in out[mat.notna() & iss.notna() & (mat <= iss)].iterrows():
        findings.append(
            Finding(
                rule_code="SM004",
                rule_title="Maturity date not after issue date",
                severity=Severity.ERROR,
                action=Action.FLAGGED,
                source_table=table,
                key={"security_id": r["security_id"]},
                message=(
                    f"maturity_date {r['maturity_date']} is not after issue_date "
                    f"{r['issue_date']}. Left unchanged: there is no way to tell which of "
                    "the two dates is wrong, and guessing would corrupt any maturity analysis."
                ),
            )
        )

    # SM005 — unparseable dates.
    for col, parsed in (("issue_date", iss), ("maturity_date", mat)):
        bad = out[parsed.isna() & out[col].notna()]
        for _, r in bad.iterrows():
            findings.append(
                Finding(
                    rule_code="SM005",
                    rule_title="Unparseable date on security master",
                    severity=Severity.ERROR,
                    action=Action.FLAGGED,
                    source_table=table,
                    key={"security_id": r["security_id"]},
                    column=col,
                    observed=r[col],
                    message=f"{col} could not be parsed as a date.",
                )
            )

    # SM006 — coupon outside a plausible band. Flagged rather than clipped; a
    # wrong coupon would quietly distort the weighted-average-coupon bonus metric.
    cpn = pd.to_numeric(out["coupon_pct"], errors="coerce")
    bad_cpn = out[cpn.notna() & ((cpn < th.coupon_min_pct) | (cpn > th.coupon_max_pct))]
    for _, r in bad_cpn.iterrows():
        findings.append(
            Finding(
                rule_code="SM006",
                rule_title="Coupon outside plausible range",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={"security_id": r["security_id"]},
                column="coupon_pct",
                observed=r["coupon_pct"],
                message=(
                    f"coupon_pct {r['coupon_pct']} is outside "
                    f"[{th.coupon_min_pct}, {th.coupon_max_pct}]."
                ),
            )
        )

    out["issue_date"], out["maturity_date"] = iss, mat
    out["coupon_pct"] = cpn
    return out.reset_index(drop=True), findings


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


def clean_marks(
    df: pd.DataFrame, valid_ids: set[str], th: Thresholds
) -> tuple[pd.DataFrame, list[Finding]]:
    """Clean prices and spreads. Runs before holdings, which impute from prices."""
    out = df.copy()
    findings: list[Finding] = []
    table = "marks_monthly"

    out["as_of_date"] = _to_date(out["as_of_date"])
    out["clean_price"] = pd.to_numeric(out["clean_price"], errors="coerce")
    out["oas_bps"] = pd.to_numeric(out["oas_bps"], errors="coerce")

    # MK001 — duplicate (security_id, as_of_date). Unlike holdings there is no
    # database_date to arbitrate, so keep the last and say so.
    dup = out.duplicated(["security_id", "as_of_date"], keep="last")
    for _, r in out[dup].iterrows():
        findings.append(
            Finding(
                rule_code="MK001",
                rule_title="Duplicate mark for security and date",
                severity=Severity.ERROR,
                action=Action.DEDUPLICATED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                message=(
                    "More than one mark for this security and month-end; kept the last "
                    "row in file order. No load timestamp exists here to break the tie."
                ),
            )
        )
    out = out[~dup].copy()

    # MK002 — decimal-scale error. This is the highest-value repair in the file.
    # A "price" of 0.9750 is not a per-100 quote, it is par-fraction notation that
    # leaked in from another system. We repair only when multiplying by 100 lands
    # the value back inside the plausible band, so the rule cannot run away on
    # genuinely distressed prices.
    scale_mask = (
        out["clean_price"].notna()
        & (out["clean_price"] < th.price_scale_detect_max)
        & (out["clean_price"] * th.price_scale_factor).between(
            th.price_band_lo, th.price_band_hi
        )
    )
    for idx, r in out[scale_mask].iterrows():
        repaired = r["clean_price"] * th.price_scale_factor
        findings.append(
            Finding(
                rule_code="MK002",
                rule_title="Price quoted as fraction of par instead of per 100",
                severity=Severity.ERROR,
                action=Action.REPAIRED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="clean_price",
                observed=r["clean_price"],
                replacement=round(repaired, 6),
                message=(
                    f"clean_price {r['clean_price']} is below "
                    f"{th.price_scale_detect_max} and therefore not a credible per-100 "
                    f"quote; x{th.price_scale_factor:g} gives {repaired:.4f}, inside the "
                    "plausible band. Left uncorrected this understates the position's "
                    "market value by ~99% and would poison both the sector average price "
                    "and the portfolio total for that month."
                ),
            )
        )
    out.loc[scale_mask, "clean_price"] = (
        out.loc[scale_mask, "clean_price"] * th.price_scale_factor
    )

    # MK003 — price still outside the plausible band after any scale repair.
    band_mask = out["clean_price"].notna() & ~out["clean_price"].between(
        th.price_band_lo, th.price_band_hi
    )
    for _, r in out[band_mask].iterrows():
        findings.append(
            Finding(
                rule_code="MK003",
                rule_title="Price outside plausible band",
                severity=Severity.ERROR,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="clean_price",
                observed=r["clean_price"],
                message=(
                    f"clean_price {r['clean_price']} is outside "
                    f"[{th.price_band_lo}, {th.price_band_hi}] and is not a recoverable "
                    "scale error. Flagged, not altered."
                ),
            )
        )

    # MK004 — missing price. Cannot be invented; downstream MV imputation will
    # fall back to the reported market value.
    for _, r in out[out["clean_price"].isna()].iterrows():
        findings.append(
            Finding(
                rule_code="MK004",
                rule_title="Missing clean price",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()) if pd.notna(r["as_of_date"]) else None,
                },
                column="clean_price",
                message="clean_price is null or unparseable.",
            )
        )

    # MK005 — OAS outside a plausible band.
    oas_mask = out["oas_bps"].notna() & ~out["oas_bps"].between(
        th.oas_min_bps, th.oas_max_bps
    )
    for _, r in out[oas_mask].iterrows():
        findings.append(
            Finding(
                rule_code="MK005",
                rule_title="OAS outside plausible band",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="oas_bps",
                observed=r["oas_bps"],
                message=(
                    f"oas_bps {r['oas_bps']} is outside "
                    f"[{th.oas_min_bps}, {th.oas_max_bps}]."
                ),
            )
        )

    # MK006 — mark references a security absent from the master.
    unknown = out[~out["security_id"].isin(valid_ids)]
    for sid in sorted(unknown["security_id"].unique()):
        findings.append(
            Finding(
                rule_code="MK006",
                rule_title="Mark references unknown security",
                severity=Severity.ERROR,
                action=Action.EXCLUDED,
                source_table=table,
                key={"security_id": sid},
                message=(
                    "security_id is not present in the security master; marks excluded "
                    "from the curated layer as they cannot be attributed to a sector or rating."
                ),
                context={"rows_excluded": int((out["security_id"] == sid).sum())},
            )
        )
    out = out[out["security_id"].isin(valid_ids)].copy()

    # MK007 — large month-over-month price move. Flag-only by design: a genuine
    # credit event produces exactly this signature, and the assignment asks us to
    # *find* one (Q3). The rule's job is to point a human at the month, not to
    # decide whether the move was real.
    out = out.sort_values(["security_id", "as_of_date"])
    prev = out.groupby("security_id")["clean_price"].shift(1)
    pct = (out["clean_price"] - prev) / prev * 100.0
    jump = pct.notna() & (pct.abs() > th.price_jump_pct)
    for idx, r in out[jump].iterrows():
        findings.append(
            Finding(
                rule_code="MK007",
                rule_title="Large month-over-month price move",
                severity=Severity.INFO,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="clean_price",
                observed=r["clean_price"],
                message=(
                    f"clean_price moved {pct.loc[idx]:+.1f}% versus the prior month-end "
                    f"(from {prev.loc[idx]:.4f}). Informational: this rule fires on real "
                    "credit events as well as on errors, so it is never auto-corrected."
                ),
                context={"prior_price": round(float(prev.loc[idx]), 4)},
            )
        )

    return out.reset_index(drop=True), findings


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------


def clean_holdings(
    df: pd.DataFrame,
    marks: pd.DataFrame,
    master: pd.DataFrame,
    th: Thresholds,
) -> tuple[pd.DataFrame, list[Finding]]:
    """Clean position snapshots.

    Takes cleaned marks and master so it can impute missing market values and
    detect orphans / post-maturity positions.
    """
    out = df.copy()
    findings: list[Finding] = []
    table = "holdings_monthly"
    valid_ids = set(master["security_id"])

    out["as_of_date"] = _to_date(out["as_of_date"])
    out["database_date"] = _to_date(out["database_date"])
    for c in ("par_amount", "book_value", "market_value"):
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # HL001 — duplicate snapshot for the same (security, as_of_date).
    #
    # The obvious tie-break is "latest database_date wins", reading the later load
    # as a restatement. We do NOT do that, because in this extract the data
    # contradicts it: for every duplicated pair, the *earlier* row's implied price
    # (market_value / par x 100) reconciles to the independent mark in
    # marks_monthly to four decimal places, while the later row diverges by around
    # a point. The later row is the corrupted one, so "latest wins" would keep the
    # bad value in every portfolio total.
    #
    # So the tie-break is cross-file reconciliation against an independent source,
    # with database_date only as a fallback when no mark exists to arbitrate. This
    # is both more defensible and more portable than a load-order convention: it
    # keeps whichever row the marks corroborate, whatever order they arrived in.
    mk_px = {
        (t.security_id, t.as_of_date): t.clean_price
        for t in marks.itertuples()
        if pd.notna(t.clean_price)
    }
    out = out.sort_values(["security_id", "as_of_date", "database_date"])
    dup_all = out[out.duplicated(["security_id", "as_of_date"], keep=False)]
    drop_idx: list[int] = []
    for (sid, dt), grp in dup_all.groupby(["security_id", "as_of_date"], sort=False):
        price = mk_px.get((sid, dt))
        chosen, basis = None, ""
        if price is not None:
            implied = grp["market_value"] / grp["par_amount"] * 100.0
            dev = (implied - price).abs()
            if dev.notna().any() and dev.min() <= th.mv_price_tolerance_pts:
                chosen = dev.idxmin()
                basis = (
                    f"implied price reconciles to the {dt.date()} mark of {price:.4f} "
                    f"(deviation {dev.min():.4f} points)"
                )
        if chosen is None:
            # No mark, or no candidate reconciles: fall back to the later load.
            valid_db = grp["database_date"].dropna()
            chosen = valid_db.idxmax() if not valid_db.empty else grp.index[-1]
            basis = "latest database_date; no mark available to reconcile against"

        for idx, r in grp.iterrows():
            if idx == chosen:
                continue
            drop_idx.append(idx)
            findings.append(
                Finding(
                    rule_code="HL001",
                    rule_title="Duplicate holdings snapshot for security and month-end",
                    severity=Severity.ERROR,
                    action=Action.DEDUPLICATED,
                    source_table=table,
                    key={"security_id": sid, "as_of_date": str(dt.date())},
                    column="market_value",
                    observed=r["market_value"],
                    replacement=out.loc[chosen, "market_value"],
                    message=(
                        f"{len(grp)} snapshots exist for this security and month-end. "
                        f"Dropped the row loaded on {r['database_date'].date()} and kept the "
                        f"one loaded on {out.loc[chosen, 'database_date'].date()} because its "
                        f"{basis}. Counting both would double the position in every portfolio "
                        "total; keeping the wrong one would carry a market value the marks "
                        "file contradicts."
                    ),
                    context={
                        "dropped_database_date": str(r["database_date"].date()),
                        "kept_database_date": str(out.loc[chosen, "database_date"].date()),
                        "tie_break": basis,
                    },
                )
            )
    out = out.drop(index=drop_idx).copy()

    # HL002 — sign error. A negative par on a long-only bond portfolio, with a
    # *positive* book value on the same row, is a sign flip rather than a short:
    # a genuine short would carry a negative book value too. We additionally
    # require the same security to hold positive par in another month, so the
    # rule cannot mistake a real short book for corruption.
    positive_elsewhere = (
        out[out["par_amount"] > 0].groupby("security_id")["par_amount"].size()
    )
    neg_mask = out["par_amount"].notna() & (out["par_amount"] < 0)
    for idx, r in out[neg_mask].iterrows():
        sid = r["security_id"]
        recoverable = (
            positive_elsewhere.get(sid, 0) > 0
            and pd.notna(r["book_value"])
            and r["book_value"] > 0
        )
        if recoverable:
            findings.append(
                Finding(
                    rule_code="HL002",
                    rule_title="Negative par with positive book value (sign error)",
                    severity=Severity.ERROR,
                    action=Action.REPAIRED,
                    source_table=table,
                    key={"security_id": sid, "as_of_date": str(r["as_of_date"].date())},
                    column="par_amount",
                    observed=r["par_amount"],
                    replacement=abs(r["par_amount"]),
                    message=(
                        "par_amount is negative while book_value on the same row is "
                        "positive, and this security holds positive par in other months. "
                        "Read as a sign flip and corrected via absolute value; market_value "
                        "corrected likewise. A true short position would show a negative "
                        "book value as well."
                    ),
                    context={
                        "book_value": r["book_value"],
                        "market_value_observed": r["market_value"],
                    },
                )
            )
            out.loc[idx, "par_amount"] = abs(r["par_amount"])
            if pd.notna(r["market_value"]):
                out.loc[idx, "market_value"] = abs(r["market_value"])
        else:
            findings.append(
                Finding(
                    rule_code="HL002",
                    rule_title="Negative par amount",
                    severity=Severity.ERROR,
                    action=Action.FLAGGED,
                    source_table=table,
                    key={"security_id": sid, "as_of_date": str(r["as_of_date"].date())},
                    column="par_amount",
                    observed=r["par_amount"],
                    message=(
                        "par_amount is negative and the sign flip is not corroborated by a "
                        "positive book value or by positive par in other months. Flagged "
                        "rather than altered."
                    ),
                )
            )

    # HL003 — orphan holding. Excluded, because an unclassifiable position would
    # silently distort the sector and rating allocation views.
    unknown = out[~out["security_id"].isin(valid_ids)]
    for sid in sorted(unknown["security_id"].unique()):
        findings.append(
            Finding(
                rule_code="HL003",
                rule_title="Holding references unknown security",
                severity=Severity.ERROR,
                action=Action.EXCLUDED,
                source_table=table,
                key={"security_id": sid},
                message=(
                    "security_id is not present in the security master; holdings excluded "
                    "from the curated layer. Note this removes real market value from "
                    "portfolio totals — see the assumptions log."
                ),
                context={"rows_excluded": int((out["security_id"] == sid).sum())},
            )
        )
    out = out[out["security_id"].isin(valid_ids)].copy()

    # HL004 — market value missing. Recoverable from par x clean price when a
    # mark exists for that security and month, which is exactly the identity the
    # source system should have used. Where no mark exists we cannot impute.
    mk = marks[["security_id", "as_of_date", "clean_price"]]
    out = out.merge(mk, on=["security_id", "as_of_date"], how="left")
    missing_mv = out["market_value"].isna()
    imputable = missing_mv & out["clean_price"].notna() & out["par_amount"].notna()
    for idx, r in out[imputable].iterrows():
        imputed = r["par_amount"] * r["clean_price"] / 100.0
        findings.append(
            Finding(
                rule_code="HL004",
                rule_title="Missing market value imputed from par and price",
                severity=Severity.WARNING,
                action=Action.IMPUTED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="market_value",
                observed=None,
                replacement=round(float(imputed), 2),
                message=(
                    f"market_value is null; imputed as par_amount x clean_price / 100 = "
                    f"{imputed:,.2f} using the {r['as_of_date'].date()} mark of "
                    f"{r['clean_price']:.4f}. Dropping these rows instead would understate "
                    "portfolio market value and create fake month-over-month swings."
                ),
            )
        )
    out.loc[imputable, "market_value"] = (
        out.loc[imputable, "par_amount"] * out.loc[imputable, "clean_price"] / 100.0
    )
    out["market_value_imputed"] = False
    out.loc[imputable, "market_value_imputed"] = True

    unimputable = out["market_value"].isna()
    for _, r in out[unimputable].iterrows():
        findings.append(
            Finding(
                rule_code="HL005",
                rule_title="Missing market value with no mark to impute from",
                severity=Severity.ERROR,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="market_value",
                message=(
                    "market_value is null and no clean price exists for this security and "
                    "month-end, so it cannot be imputed. Row is retained with a null market "
                    "value and excluded from market-value aggregates."
                ),
            )
        )

    # HL006 — market value inconsistent with par x price. After HL001 has removed
    # the restatements this should be near-empty; a residual hit means either the
    # price or the market value is wrong and a human needs to decide which.
    both = out["market_value"].notna() & out["clean_price"].notna() & (out["par_amount"] > 0)
    implied = out["market_value"] / out["par_amount"] * 100.0
    delta = implied - out["clean_price"]
    incons = both & (delta.abs() > th.mv_price_tolerance_pts) & ~out["market_value_imputed"]
    for idx, r in out[incons].iterrows():
        findings.append(
            Finding(
                rule_code="HL006",
                rule_title="Market value inconsistent with par x price",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="market_value",
                observed=r["market_value"],
                message=(
                    f"market_value implies a price of {implied.loc[idx]:.4f} but the mark "
                    f"is {r['clean_price']:.4f} (difference {delta.loc[idx]:+.4f} points, "
                    f"tolerance {th.mv_price_tolerance_pts}). Reported market_value kept as "
                    "the book of record; the mark is used only for price analytics."
                ),
                context={"implied_price": round(float(implied.loc[idx]), 4)},
            )
        )

    # HL007 — as_of_date is not a month-end, though the file claims month-end snapshots.
    not_me = out["as_of_date"].notna() & ~_is_month_end(out["as_of_date"])
    for _, r in out[not_me].iterrows():
        findings.append(
            Finding(
                rule_code="HL007",
                rule_title="Snapshot date is not a month-end",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="as_of_date",
                observed=str(r["as_of_date"].date()),
                message=(
                    "as_of_date is not the last calendar day of its month, but this extract "
                    "is documented as month-end snapshots."
                ),
            )
        )

    # HL008 — loaded before the date it describes. Impossible, and a signal that
    # database_date cannot be trusted as the restatement arbiter for that row.
    early = (
        out["database_date"].notna()
        & out["as_of_date"].notna()
        & (out["database_date"] < out["as_of_date"])
    )
    for _, r in out[early].iterrows():
        findings.append(
            Finding(
                rule_code="HL008",
                rule_title="database_date precedes as_of_date",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                column="database_date",
                observed=str(r["database_date"].date()),
                message=(
                    "Row was loaded before the month-end it reports, which is not possible. "
                    "HL001 relies on database_date to pick the surviving snapshot, so this "
                    "undermines that tie-break."
                ),
            )
        )

    # HL009 — position still held after the security matured. Either the maturity
    # date or the position is wrong; both matter for Q1's maturity attribution.
    mat = master.set_index("security_id")["maturity_date"]
    out["_maturity"] = out["security_id"].map(mat)
    post = (
        out["_maturity"].notna()
        & out["as_of_date"].notna()
        & (out["as_of_date"] > out["_maturity"])
        & out["par_amount"].fillna(0).ne(0)
    )
    for _, r in out[post].iterrows():
        findings.append(
            Finding(
                rule_code="HL009",
                rule_title="Position held after maturity date",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                message=(
                    f"Non-zero par at {r['as_of_date'].date()} but the security matured on "
                    f"{pd.Timestamp(r['_maturity']).date()}. A matured bond should redeem to "
                    "zero par; retained and flagged."
                ),
                context={"maturity_date": str(pd.Timestamp(r["_maturity"]).date())},
            )
        )

    # HL010 — held position with no mark. Not an error in itself, but it means the
    # security drill-down will show a price gap, so it belongs on the DQ page.
    no_mark = out["clean_price"].isna()
    for _, r in out[no_mark].iterrows():
        findings.append(
            Finding(
                rule_code="HL010",
                rule_title="Held position has no mark for the month",
                severity=Severity.INFO,
                action=Action.FLAGGED,
                source_table=table,
                key={
                    "security_id": r["security_id"],
                    "as_of_date": str(r["as_of_date"].date()),
                },
                message=(
                    "Position is held at this month-end but marks_monthly has no price or "
                    "OAS for it, so price-based analytics skip this observation."
                ),
            )
        )

    out = out.drop(columns=["_maturity"])
    return out.reset_index(drop=True), findings


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def clean_transactions(
    df: pd.DataFrame,
    master: pd.DataFrame,
    th: Thresholds,
    valid_types: tuple[str, ...] = ("BUY", "SELL", "MATURITY"),
) -> tuple[pd.DataFrame, list[Finding]]:
    """Clean the activity file. Feeds Q1's trading-versus-market decomposition."""
    out = df.copy()
    findings: list[Finding] = []
    table = "transactions"
    valid_ids = set(master["security_id"])

    out["trade_date"] = _to_date(out["trade_date"])
    out["settlement_date"] = _to_date(out["settlement_date"])
    out["par_amount"] = pd.to_numeric(out["par_amount"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["trade_type"] = out["trade_type"].astype(str).str.strip().str.upper()

    # TX001 — duplicate trade_id. Split by whether the duplicate is byte-identical:
    # an identical repeat is a double-send and safe to drop, whereas two different
    # trades sharing an id is a genuine identifier collision needing a human.
    business_cols = [
        c
        for c in ("trade_date", "security_id", "trade_type", "par_amount", "price")
        if c in out.columns
    ]
    dup_ids = out["trade_id"][out["trade_id"].duplicated(keep=False)].unique()
    drop_idx: list[int] = []
    for tid in sorted(dup_ids):
        grp = out[out["trade_id"] == tid]
        identical = grp[business_cols].drop_duplicates().shape[0] == 1
        if identical:
            keep, rest = grp.index[0], grp.index[1:]
            drop_idx.extend(rest)
            findings.append(
                Finding(
                    rule_code="TX001",
                    rule_title="Duplicate trade_id with identical details",
                    severity=Severity.ERROR,
                    action=Action.DEDUPLICATED,
                    source_table=table,
                    key={"trade_id": tid},
                    message=(
                        f"trade_id appears {len(grp)} times with identical trade details; "
                        "kept one. Read as a double-send from the source feed. Counting both "
                        f"would overstate activity by {abs(grp['par_amount'].iloc[0]):,.0f} "
                        "par and corrupt the trading-versus-market decomposition."
                    ),
                    context={"occurrences": int(len(grp))},
                )
            )
        else:
            findings.append(
                Finding(
                    rule_code="TX002",
                    rule_title="Duplicate trade_id with conflicting details",
                    severity=Severity.ERROR,
                    action=Action.FLAGGED,
                    source_table=table,
                    key={"trade_id": tid},
                    message=(
                        f"trade_id is reused across {len(grp)} rows with different trade "
                        "details, so neither can be dismissed as a duplicate. All rows "
                        "retained and flagged."
                    ),
                    context={"occurrences": int(len(grp))},
                )
            )
    out = out.drop(index=drop_idx)

    # TX003 — trade references a security absent from the master. Excluded from
    # the curated layer but called out prominently: these are real cash flows, so
    # dropping them leaves a hole in the Q1 attribution that must be disclosed.
    unknown = out[~out["security_id"].isin(valid_ids)]
    for sid in sorted(unknown["security_id"].unique()):
        grp = out[out["security_id"] == sid]
        findings.append(
            Finding(
                rule_code="TX003",
                rule_title="Trade references unknown security",
                severity=Severity.ERROR,
                action=Action.EXCLUDED,
                source_table=table,
                key={"security_id": sid},
                message=(
                    "security_id is not present in the security master and never appears in "
                    "holdings, so the trade cannot be attributed to a sector, rating, or "
                    "position. Excluded from the curated layer and reported here; the "
                    "notional is disclosed in the Q1 attribution residual."
                ),
                context={
                    "trade_ids": ", ".join(sorted(grp["trade_id"].astype(str))),
                    "total_par": float(grp["par_amount"].sum()),
                },
            )
        )
    out = out[out["security_id"].isin(valid_ids)].copy()

    # TX004 — settlement before trade.
    bad_settle = (
        out["settlement_date"].notna()
        & out["trade_date"].notna()
        & (out["settlement_date"] < out["trade_date"])
    )
    for _, r in out[bad_settle].iterrows():
        findings.append(
            Finding(
                rule_code="TX004",
                rule_title="Settlement date precedes trade date",
                severity=Severity.ERROR,
                action=Action.FLAGGED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="settlement_date",
                observed=str(r["settlement_date"].date()),
                message=(
                    f"settlement_date {r['settlement_date'].date()} is before trade_date "
                    f"{r['trade_date'].date()}."
                ),
            )
        )

    # TX005 — unrecognised trade type. Excluded: we cannot sign the cash flow
    # without knowing whether it increases or decreases the position.
    bad_type = ~out["trade_type"].isin(valid_types)
    for _, r in out[bad_type].iterrows():
        findings.append(
            Finding(
                rule_code="TX005",
                rule_title="Unrecognised trade type",
                severity=Severity.ERROR,
                action=Action.EXCLUDED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="trade_type",
                observed=r["trade_type"],
                message=(
                    f"trade_type is not one of {valid_types}; excluded because the direction "
                    "of the par change is undefined."
                ),
            )
        )
    out = out[~bad_type].copy()

    # TX006 — non-positive par. Direction is carried by trade_type in this
    # extract, so par should always be a positive magnitude.
    bad_par = out["par_amount"].isna() | (out["par_amount"] <= 0)
    for _, r in out[bad_par].iterrows():
        findings.append(
            Finding(
                rule_code="TX006",
                rule_title="Non-positive trade par amount",
                severity=Severity.ERROR,
                action=Action.FLAGGED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="par_amount",
                observed=r["par_amount"],
                message=(
                    "par_amount is null or non-positive. Direction is expressed by "
                    "trade_type in this extract, so par is expected to be a positive magnitude."
                ),
            )
        )

    # TX007 — trade price outside the plausible band.
    bad_px = out["price"].notna() & ~out["price"].between(th.price_band_lo, th.price_band_hi)
    for _, r in out[bad_px].iterrows():
        findings.append(
            Finding(
                rule_code="TX007",
                rule_title="Trade price outside plausible band",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="price",
                observed=r["price"],
                message=(
                    f"price {r['price']} is outside "
                    f"[{th.price_band_lo}, {th.price_band_hi}]."
                ),
            )
        )

    # TX008 — a redemption that did not settle at par.
    mat_rows = out["trade_type"].eq("MATURITY") & out["price"].notna()
    off_par = mat_rows & (
        (out["price"] - th.maturity_expected_price).abs() > th.maturity_price_tolerance_pts
    )
    for _, r in out[off_par].iterrows():
        findings.append(
            Finding(
                rule_code="TX008",
                rule_title="Maturity did not redeem at par",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="price",
                observed=r["price"],
                message=(
                    f"MATURITY priced at {r['price']} rather than "
                    f"{th.maturity_expected_price}. A redemption at anything other than par "
                    "suggests a default, a partial call, or a mislabelled sale."
                ),
            )
        )

    # TX009 — maturity event on a date that disagrees with the master's maturity date.
    mat_map = master.set_index("security_id")["maturity_date"]
    out["_maturity"] = out["security_id"].map(mat_map)
    mism = (
        out["trade_type"].eq("MATURITY")
        & out["_maturity"].notna()
        & out["trade_date"].notna()
        & (out["trade_date"] != out["_maturity"])
    )
    for _, r in out[mism].iterrows():
        findings.append(
            Finding(
                rule_code="TX009",
                rule_title="Maturity event date disagrees with security master",
                severity=Severity.WARNING,
                action=Action.FLAGGED,
                source_table=table,
                key={"trade_id": r["trade_id"]},
                column="trade_date",
                observed=str(r["trade_date"].date()),
                message=(
                    f"MATURITY dated {r['trade_date'].date()} but the master records a "
                    f"maturity_date of {pd.Timestamp(r['_maturity']).date()}."
                ),
                context={"maturity_date": str(pd.Timestamp(r["_maturity"]).date())},
            )
        )
    out = out.drop(columns=["_maturity"])

    return out.reset_index(drop=True), findings
