"""Cached data access for the web layer.

The application reads exclusively from the database at runtime, never from the
CSVs — assignment requirement 1. This module is the single place that happens.

Why cache: the dataset is a fixed twelve-month snapshot that only changes when
the loader runs, and every callback would otherwise re-query and re-derive the
whole panel. Caching turns a page interaction from several round trips into
zero. The cache key is the load_id, so a fresh load invalidates it naturally
rather than needing a manual flush.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import Engine

from ..analytics import allocation, attribution, metrics
from ..analytics.queries import load_findings, load_positions, load_runs, load_trades
from ..load.loader import latest_load_id

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    """Everything the pages need, derived once per load."""

    load_id: int | None
    positions: pd.DataFrame
    trades: pd.DataFrame
    findings: pd.DataFrame
    runs: pd.DataFrame
    overview: pd.DataFrame
    monthly_attribution: pd.DataFrame
    reconciliation: pd.DataFrame
    sector_mix: pd.DataFrame
    rating_mix: pd.DataFrame
    sector_shift: pd.DataFrame
    rating_shift: pd.DataFrame
    sector_detail: pd.DataFrame
    worst_sector_months: pd.DataFrame
    weighted_metrics: pd.DataFrame
    worst_returns: pd.DataFrame

    @property
    def is_empty(self) -> bool:
        return self.positions.empty

    @property
    def securities(self) -> pd.DataFrame:
        """Distinct securities, for the drill-down picker."""
        if self.positions.empty:
            return pd.DataFrame(columns=["security_id", "description", "sector", "rating"])
        return (
            self.positions.drop_duplicates("security_id")[
                ["security_id", "description", "sector", "rating"]
            ]
            .sort_values("description")
            .reset_index(drop=True)
        )


_lock = threading.Lock()
_cache: dict[int | None, Snapshot] = {}


def _build(engine: Engine, load_id: int | None) -> Snapshot:
    positions = load_positions(engine)
    trades = load_trades(engine)

    if positions.empty:
        empty = pd.DataFrame()
        return Snapshot(
            load_id=load_id, positions=positions, trades=trades,
            findings=load_findings(engine, load_id), runs=load_runs(engine),
            overview=empty, monthly_attribution=empty, reconciliation=empty,
            sector_mix=empty, rating_mix=empty, sector_shift=empty,
            rating_shift=empty, sector_detail=empty, worst_sector_months=empty,
            weighted_metrics=empty, worst_returns=empty,
        )

    return Snapshot(
        load_id=load_id,
        positions=positions,
        trades=trades,
        findings=load_findings(engine, load_id),
        runs=load_runs(engine),
        overview=attribution.portfolio_overview(positions),
        monthly_attribution=attribution.monthly_attribution(positions),
        reconciliation=attribution.reconcile_trading(positions, trades),
        sector_mix=allocation.sector_mix(positions),
        rating_mix=allocation.rating_mix(positions),
        sector_shift=allocation.allocation_shift(positions, "sector", top_n=None),
        rating_shift=allocation.allocation_shift(positions, "rating", top_n=None),
        sector_detail=allocation.sector_month_detail(positions),
        worst_sector_months=allocation.worst_sector_month(positions),
        weighted_metrics=metrics.monthly_weighted_metrics(positions),
        worst_returns=attribution.security_price_returns(positions, top_n=10),
    )


def get_snapshot(engine: Engine, *, refresh: bool = False) -> Snapshot:
    """The current snapshot, built on first use and reused thereafter.

    Keyed on the latest successful load_id: running the loader again produces a
    new id and the next request rebuilds, so there is no stale-cache footgun and
    no flush endpoint to remember.
    """
    load_id = latest_load_id(engine)
    with _lock:
        if refresh:
            _cache.clear()
        if load_id in _cache:
            return _cache[load_id]
        log.info("building analytics snapshot for load_id=%s", load_id)
        snap = _build(engine, load_id)
        _cache[load_id] = snap
        return snap


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def security_history(snapshot: Snapshot, security_id: str) -> pd.DataFrame:
    return metrics.security_history(snapshot.positions, security_id)


def findings_summary(findings: pd.DataFrame) -> pd.DataFrame:
    """Per-rule counts — the headline table on the data-quality page."""
    if findings.empty:
        return pd.DataFrame(columns=["rule_code", "rule_title", "severity", "action", "count"])
    severity_rank = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    out = (
        findings.groupby(["rule_code", "rule_title", "severity", "action"])
        .size()
        .reset_index(name="count")
    )
    out["_rank"] = out["severity"].map(severity_rank)
    return out.sort_values(["_rank", "count"], ascending=[True, False]).drop(columns="_rank")
