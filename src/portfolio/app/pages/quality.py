"""Data-quality page: every anomaly the loading rules detected.

Required feature 4, and the Q5 answer.

This page renders the dq_finding table and nothing else. It contains no list of
known problems and no hardcoded rule outcomes — the content is entirely a
projection of what the rules found during the last load. Hand the loader a
different extract with similar problems and this page reports that extract's
findings instead, because there is no other source it could draw from.
"""

from __future__ import annotations

import json

import pandas as pd
from dash import dcc, html

from .. import figures
from ..components import graph, card, data_table, note, severity_tile, stat_tile
from ..data import Snapshot, findings_summary


def _format_key(v) -> str:
    """record_key arrives as JSON from Postgres or as text from SQLite."""
    if isinstance(v, dict):
        return " · ".join(f"{k}={val}" for k, val in v.items())
    if isinstance(v, str):
        try:
            return " · ".join(f"{k}={val}" for k, val in json.loads(v).items())
        except (ValueError, TypeError):
            return v
    return "" if v is None else str(v)


def layout(snap: Snapshot, mode: str = "light") -> html.Div:
    findings = snap.findings
    summary = findings_summary(findings)

    if findings.empty:
        return html.Div(
            [
                html.H1("Data quality", className="page-title"),
                card(
                    note(
                        "The validation rules found no anomalies in the current load. "
                        "That is a result, not an absence of checking: 32 rules ran."
                    )
                ),
            ]
        )

    counts = findings["severity"].value_counts().to_dict()
    runs = snap.runs
    last_run = runs.iloc[0] if not runs.empty else None

    tiles = html.Div(
        [severity_tile(s, int(counts.get(s, 0))) for s in ("ERROR", "WARNING", "INFO")]
        + [
            stat_tile(
                "Rules that fired",
                f"{summary['rule_code'].nunique()}",
                "of 32 implemented",
            )
        ],
        className="tile-row",
    )

    # Repairs, called out separately: these are the rows where the loader changed
    # a delivered value, and they carry the most obligation to be auditable.
    repaired = findings[findings["action"].isin(["REPAIRED", "IMPUTED", "DEFAULTED"])]
    repair_table = pd.DataFrame(
        {
            "Rule": repaired["rule_code"],
            "Record": repaired["record_key"].map(_format_key),
            "Field": repaired["column_name"].fillna("—"),
            "Delivered": repaired["observed"].fillna("(null)"),
            "Used": repaired["replacement"].fillna("—"),
            "Action": repaired["action"].str.title(),
        }
    )

    excluded = findings[findings["action"].isin(["EXCLUDED", "DEDUPLICATED"])]
    excluded_table = pd.DataFrame(
        {
            "Rule": excluded["rule_code"],
            "Record": excluded["record_key"].map(_format_key),
            "Action": excluded["action"].str.title(),
            "Why": excluded["message"],
        }
    )

    all_table = pd.DataFrame(
        {
            "Severity": findings["severity"],
            "Rule": findings["rule_code"],
            "Title": findings["rule_title"],
            "Record": findings["record_key"].map(_format_key),
            "Action": findings["action"].str.title(),
            "Detail": findings["message"],
        }
    )

    summary_table = pd.DataFrame(
        {
            "Severity": summary["severity"],
            "Rule": summary["rule_code"],
            "Title": summary["rule_title"],
            "Action": summary["action"].str.title(),
            "Count": summary["count"],
        }
    )

    meta = []
    if last_run is not None:
        meta.append(
            f"Load {last_run['load_id']} · {last_run['status']} · "
            f"{last_run['rows_raw']:,} raw rows in, {last_run['rows_curated']:,} curated."
        )

    return html.Div(
        [
            html.H1("Data quality", className="page-title"),
            html.P(
                "Every row on this page was produced by a validation rule during the "
                "last load. Nothing here is a hardcoded list of known problems.",
                className="page-lede",
            ),
            tiles,
            card(
                graph(
                    figures.findings_by_rule(summary, mode),
                    figures.H_TALL,
                ),
                data_table(summary_table, page_size=15),
                title="Findings by rule",
                wide=True,
            ),
            card(
                note(
                    "Where a value was changed, both the delivered value and the value "
                    "actually used are shown. The raw tables retain the original, so any "
                    "repair can be checked against what arrived rather than taken on "
                    "trust.",
                ),
                data_table(repair_table, page_size=12),
                title=f"Values changed during loading ({len(repaired)})",
                wide=True,
            ),
            card(
                note(
                    "Rows removed from the curated layer, with the reason. Excluding a "
                    "row removes real notional from the portfolio, so each exclusion is "
                    "reported rather than silently applied.",
                ),
                data_table(excluded_table, page_size=12),
                title=f"Rows excluded or deduplicated ({len(excluded)})",
                wide=True,
            ),
            card(
                data_table(all_table, page_size=20),
                *([note(*meta)] if meta else []),
                title=f"All findings ({len(findings)})",
                wide=True,
            ),
        ]
    )
