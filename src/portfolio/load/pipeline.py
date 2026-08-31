"""Orchestrates the cleaning rules in dependency order.

Still pure — no database, no filesystem beyond what the caller hands in — so the
whole pipeline is testable end to end without infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .findings import Finding, Severity
from .thresholds import Thresholds
from .validate import (
    clean_holdings,
    clean_marks,
    clean_security_master,
    clean_transactions,
)


@dataclass
class CleanResult:
    securities: pd.DataFrame
    holdings: pd.DataFrame
    marks: pd.DataFrame
    transactions: pd.DataFrame
    findings: list[Finding] = field(default_factory=list)

    def findings_frame(self) -> pd.DataFrame:
        """Findings as a DataFrame, ready for the DQ table / page."""
        if not self.findings:
            return pd.DataFrame(
                columns=[
                    "rule_code",
                    "rule_title",
                    "severity",
                    "action",
                    "source_table",
                    "key",
                    "message",
                    "column",
                    "observed",
                    "replacement",
                    "context",
                ]
            )
        return pd.DataFrame([f.to_row() for f in self.findings])

    def summary(self) -> pd.DataFrame:
        """Counts per rule — the headline table on the data-quality page."""
        df = self.findings_frame()
        if df.empty:
            return df
        return (
            df.groupby(["rule_code", "rule_title", "severity", "action"])
            .size()
            .reset_index(name="count")
            .sort_values(["severity", "count"], ascending=[True, False])
        )

    def counts_by_severity(self) -> dict[str, int]:
        df = self.findings_frame()
        if df.empty:
            return {s.value: 0 for s in Severity}
        vc = df["severity"].value_counts().to_dict()
        return {s.value: int(vc.get(s.value, 0)) for s in Severity}


def clean_extract(
    securities: pd.DataFrame,
    holdings: pd.DataFrame,
    marks: pd.DataFrame,
    transactions: pd.DataFrame,
    thresholds: Thresholds | None = None,
) -> CleanResult:
    """Run every rule in dependency order and return cleaned frames plus findings.

    Order is not arbitrary:
      1. master  — everything else needs its id set for orphan detection
      2. marks   — holdings impute missing market values from clean prices
      3. holdings
      4. transactions
    """
    th = thresholds or Thresholds()
    findings: list[Finding] = []

    sec, f = clean_security_master(securities, th)
    findings += f

    mk, f = clean_marks(marks, set(sec["security_id"]), th)
    findings += f

    hl, f = clean_holdings(holdings, mk, sec, th)
    findings += f

    tx, f = clean_transactions(transactions, sec, th)
    findings += f

    return CleanResult(
        securities=sec, holdings=hl, marks=mk, transactions=tx, findings=findings
    )
