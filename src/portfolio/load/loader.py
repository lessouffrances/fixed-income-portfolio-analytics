"""The load pipeline: CSV -> raw tables -> cleaning rules -> curated tables.

This is the only module in the project that performs both filesystem and database
I/O. The rules themselves stay pure (see validate.py), which is what keeps them
testable without infrastructure.

The whole load runs in a single transaction. A partially-loaded warehouse is
worse than an empty one: the dashboard would serve numbers that look plausible
and are wrong. If anything fails, the load_run row records FAILED and nothing
else is committed.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, delete, insert, select, update

from ..config import ConfigError, Settings
from ..db import create_schema, make_engine
from ..models import (
    CURATED_TABLES,
    RAW_TABLES,
    dq_finding,
    holding,
    load_run,
    mark,
    security,
    trade,
)
from .findings import Finding
from .pipeline import CleanResult, clean_extract
from .thresholds import Thresholds

log = logging.getLogger(__name__)

CSV_FILES = {
    "security_master": "security_master.csv",
    "holdings_monthly": "holdings_monthly.csv",
    "marks_monthly": "marks_monthly.csv",
    "transactions": "transactions.csv",
}


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def read_csvs(data_dir: Path) -> dict[str, pd.DataFrame]:
    """Read the four extracts as strings, deferring all typing to the rules.

    dtype=str matters: letting pandas infer types here would silently coerce or
    NaN-out malformed values before any rule can see them, so a data-quality
    problem would vanish on the way in. Typing happens in the cleaning layer,
    where a failure to parse becomes a finding instead of a shrug.
    """
    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for key, filename in CSV_FILES.items():
        path = data_dir / filename
        if not path.exists():
            missing.append(filename)
            continue
        frames[key] = pd.read_csv(path, dtype=str, keep_default_na=True)
        log.info("read %s (%d rows)", filename, len(frames[key]))
    if missing:
        raise FileNotFoundError(
            f"missing extract(s) in {data_dir}: {', '.join(missing)}"
        )
    return frames


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _insert_frame(conn, table, df: pd.DataFrame) -> int:
    """Insert a DataFrame, mapping NaN/NaT to SQL NULL.

    pandas uses NaN for missing values in object and float columns alike, and
    psycopg would otherwise try to write the float nan into a NUMERIC column.
    """
    if df.empty:
        return 0
    cols = [c.name for c in table.columns if c.name in df.columns]
    payload = df[cols].astype(object).where(pd.notna(df[cols]), None)
    records = payload.to_dict("records")
    conn.execute(insert(table), records)
    return len(records)


def _load_raw(conn, load_id: int, frames: dict[str, pd.DataFrame]) -> int:
    """Land the delivered CSVs verbatim, preserving their original row numbers."""
    total = 0
    for key, table in RAW_TABLES.items():
        df = frames[key].copy()
        # 1-based and offset past the header, so the number matches what a human
        # sees when opening the CSV in a text editor.
        df["source_row_num"] = range(2, len(df) + 2)
        df["load_id"] = load_id
        total += _insert_frame(conn, table, df)
    log.info("raw layer: %d rows", total)
    return total


def _load_curated(conn, result: CleanResult) -> int:
    """Write the cleaned frames into the typed, constrained tables.

    Column renames map the cleaning layer's frame names onto the schema; the
    schema deliberately does not mirror the CSV headers, since `trade` carries a
    surrogate key and the CSVs have no equivalent.
    """
    total = 0

    total += _insert_frame(conn, security, result.securities)

    h = result.holdings.copy()
    for flag in ("market_value_imputed", "post_maturity"):
        if flag not in h.columns:
            h[flag] = False
        h[flag] = h[flag].fillna(False).astype(bool)
    total += _insert_frame(conn, holding, h)

    m = result.marks.copy()
    # Flag which prices the scale-error rule rewrote, so the drill-down can mark
    # a repaired point rather than presenting it as if it were delivered that way.
    repaired = {
        (f.key.get("security_id"), f.key.get("as_of_date"))
        for f in result.findings
        if f.rule_code == "MK002"
    }
    m["clean_price_repaired"] = [
        (sid, str(pd.Timestamp(d).date()) if pd.notna(d) else None) in repaired
        for sid, d in zip(m["security_id"], m["as_of_date"])
    ]
    total += _insert_frame(conn, mark, m)

    total += _insert_frame(conn, trade, result.transactions)

    log.info("curated layer: %d rows", total)
    return total


def _load_findings(conn, load_id: int, findings: list[Finding]) -> None:
    """Persist findings. This table is the data-quality page's only source."""
    if not findings:
        return
    rows = []
    for f in findings:
        r = f.to_row()
        rows.append(
            {
                "load_id": load_id,
                "rule_code": r["rule_code"],
                "rule_title": r["rule_title"],
                "severity": r["severity"],
                "action": r["action"],
                "source_table": r["source_table"],
                "record_key": r["key"],
                "column_name": r["column"],
                "observed": r["observed"],
                "replacement": r["replacement"],
                "message": r["message"],
                "context": r["context"] or None,
            }
        )
    conn.execute(insert(dq_finding), rows)
    log.info("findings: %d rows", len(rows))


def _truncate_existing(conn) -> None:
    """Clear prior data so a load is a full refresh rather than an append.

    The extracts are a complete twelve-month snapshot, not an incremental feed,
    so re-running must be idempotent. Deleted in FK-safe order: children first.
    """
    for table in (dq_finding, trade, mark, holding, security):
        conn.execute(delete(table))
    for table in RAW_TABLES.values():
        conn.execute(delete(table))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_load(
    engine: Engine,
    data_dir: Path,
    *,
    thresholds: Thresholds | None = None,
    full_refresh: bool = True,
) -> tuple[int, CleanResult]:
    """Execute a complete load. Returns the load_id and the cleaning result.

    Everything after the load_run header is one transaction. The header is
    committed separately so that a failure still leaves an auditable record of
    the attempt rather than disappearing without trace.
    """
    frames = read_csvs(data_dir)
    create_schema(engine)

    with engine.begin() as conn:
        load_id = conn.execute(
            insert(load_run).returning(load_run.c.load_id),
            [{"source_dir": str(data_dir), "status": "RUNNING"}],
        ).scalar_one()
    log.info("load_run %d started", load_id)

    try:
        with engine.begin() as conn:
            if full_refresh:
                _truncate_existing(conn)

            rows_raw = _load_raw(conn, load_id, frames)

            result = clean_extract(
                frames["security_master"],
                frames["holdings_monthly"],
                frames["marks_monthly"],
                frames["transactions"],
                thresholds,
            )

            rows_curated = _load_curated(conn, result)
            _load_findings(conn, load_id, result.findings)

            sev = result.counts_by_severity()
            conn.execute(
                update(load_run)
                .where(load_run.c.load_id == load_id)
                .values(
                    finished_at=datetime.now(timezone.utc),
                    status="SUCCEEDED",
                    rows_raw=rows_raw,
                    rows_curated=rows_curated,
                    findings_error=sev["ERROR"],
                    findings_warning=sev["WARNING"],
                    findings_info=sev["INFO"],
                )
            )
    except Exception as exc:
        # Record the failure outside the rolled-back transaction, so the audit
        # trail survives even though no data was committed.
        with engine.begin() as conn:
            conn.execute(
                update(load_run)
                .where(load_run.c.load_id == load_id)
                .values(
                    finished_at=datetime.now(timezone.utc),
                    status="FAILED",
                    notes=f"{type(exc).__name__}: {exc}"[:2000],
                )
            )
        log.exception("load_run %d failed; nothing was committed", load_id)
        raise

    log.info("load_run %d succeeded", load_id)
    return load_id, result


def latest_load_id(engine: Engine) -> int | None:
    """The most recent successful load. The app reads only from this one."""
    with engine.connect() as conn:
        return conn.execute(
            select(load_run.c.load_id)
            .where(load_run.c.status == "SUCCEEDED")
            .order_by(load_run.c.load_id.desc())
            .limit(1)
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m portfolio.load.loader",
        description="Load the CSV extracts into the database.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="defaults to $DATA_DIR or ./data"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override $DATABASE_URL (for a local database; avoid on shared shells)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = Settings.from_env() if args.database_url is None else None
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    engine = make_engine(settings, url=args.database_url)
    data_dir = args.data_dir or (settings.data_dir if settings else Path("data"))

    load_id, result = run_load(engine, data_dir)

    sev = result.counts_by_severity()
    print(f"\nload {load_id} complete")
    print(f"  securities   {len(result.securities):>6}")
    print(f"  holdings     {len(result.holdings):>6}")
    print(f"  marks        {len(result.marks):>6}")
    print(f"  trades       {len(result.transactions):>6}")
    print(
        f"  findings     {sev['ERROR']} error / {sev['WARNING']} warning / {sev['INFO']} info"
    )
    summary = result.summary()
    if not summary.empty:
        print()
        print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
