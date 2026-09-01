"""Tests for the load pipeline.

Runs against in-memory / temp-file SQLite, so the suite needs no server and no
credentials. The schema is defined once with dialect variants where SQLite and
Postgres genuinely differ, so these tests exercise the real tables rather than a
simplified stand-in.

What is deliberately NOT tested here: Postgres-specific behaviour (JSONB
containment, BIGSERIAL). Those are covered by running the loader against the real
database, which is a deployment step rather than a unit test.
"""

from __future__ import annotations

import json
import sys
import types

import pandas as pd
import pytest
from sqlalchemy import func, select

from portfolio.config import ConfigError, Settings, redact, resolve_database_url
from portfolio.db import create_schema, healthcheck, make_engine
from portfolio.load import loader as loader_mod
from portfolio.load.loader import read_csvs, run_load
from portfolio.models import (
    dq_finding,
    holding,
    load_run,
    mark,
    raw_marks_monthly,
    security,
    trade,
)


@pytest.fixture
def engine(tmp_path):
    """A throwaway file-backed SQLite database.

    File-backed rather than :memory: because the loader opens several successive
    connections (the load_run header is committed separately from the data), and
    each new connection to :memory: would get a different empty database.
    """
    eng = make_engine(url=f"sqlite:///{tmp_path / 'test.db'}")
    create_schema(eng)
    return eng


@pytest.fixture
def csv_dir(tmp_path, master, holdings, marks, transactions):
    """Write the synthetic fixtures out as CSVs for the loader to read."""
    d = tmp_path / "extracts"
    d.mkdir()
    master.to_csv(d / "security_master.csv", index=False)
    holdings.to_csv(d / "holdings_monthly.csv", index=False)
    marks.to_csv(d / "marks_monthly.csv", index=False)
    transactions.to_csv(d / "transactions.csv", index=False)
    return d


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def test_read_csvs_reads_everything_as_string(csv_dir):
    """Types must not be inferred at read time.

    pandas would happily coerce a malformed number to NaN before any rule sees
    it, so the defect would disappear on the way in and never reach the
    data-quality page.
    """
    frames = read_csvs(csv_dir)
    assert set(frames) == {
        "security_master",
        "holdings_monthly",
        "marks_monthly",
        "transactions",
    }
    for name, df in frames.items():
        non_string = [
            c for c in df.columns if df[c].dropna().map(lambda v: not isinstance(v, str)).any()
        ]
        assert not non_string, f"{name} inferred types for {non_string}"


def test_read_csvs_names_the_missing_file(tmp_path):
    (tmp_path / "security_master.csv").write_text("security_id\nAAA111\n")
    with pytest.raises(FileNotFoundError, match="holdings_monthly.csv"):
        read_csvs(tmp_path)


# ---------------------------------------------------------------------------
# Load round trip
# ---------------------------------------------------------------------------


def test_load_populates_every_layer(engine, csv_dir):
    load_id, result = run_load(engine, csv_dir)

    assert count(engine, security) == 2
    assert count(engine, holding) == 8
    assert count(engine, mark) == 8
    assert count(engine, trade) == 2
    # Clean fixtures, so there should be nothing to report.
    assert count(engine, dq_finding) == 0
    assert result.findings == []

    with engine.connect() as conn:
        row = conn.execute(
            select(load_run).where(load_run.c.load_id == load_id)
        ).mappings().one()
    assert row["status"] == "SUCCEEDED"
    assert row["finished_at"] is not None
    assert row["rows_raw"] == 20   # 2 + 8 + 8 + 2
    assert row["rows_curated"] == 20


def test_raw_layer_preserves_the_delivered_value_verbatim(engine, csv_dir, marks):
    """The audit trail depends on this: a repair must remain checkable against
    what actually arrived."""
    bad = marks.copy()
    bad.loc[2, "clean_price"] = 0.9875           # scale error -> repaired to 98.75
    bad.to_csv(csv_dir / "marks_monthly.csv", index=False)

    run_load(engine, csv_dir)

    with engine.connect() as conn:
        raw = conn.execute(
            select(raw_marks_monthly.c.clean_price).where(
                raw_marks_monthly.c.security_id == "AAA111",
                raw_marks_monthly.c.as_of_date == "2031-02-28",
            )
        ).scalar_one()
        cur, repaired = conn.execute(
            select(mark.c.clean_price, mark.c.clean_price_repaired).where(
                mark.c.security_id == "AAA111",
                mark.c.as_of_date == pd.Timestamp("2031-02-28").date(),
            )
        ).one()

    assert float(raw) == pytest.approx(0.9875)   # untouched
    assert float(cur) == pytest.approx(98.75)    # corrected
    assert bool(repaired) is True                # and marked as corrected


def test_source_row_num_points_at_the_line_in_the_file(engine, csv_dir):
    """Row 1 is the header, so the first data row must be 2 — the number a human
    sees when opening the CSV."""
    run_load(engine, csv_dir)
    with engine.connect() as conn:
        lo, hi = conn.execute(
            select(
                func.min(raw_marks_monthly.c.source_row_num),
                func.max(raw_marks_monthly.c.source_row_num),
            )
        ).one()
    assert lo == 2
    assert hi == 9  # 8 data rows


def test_findings_are_persisted_with_their_before_and_after(engine, csv_dir, marks):
    bad = marks.copy()
    bad.loc[2, "clean_price"] = 0.9875
    bad.to_csv(csv_dir / "marks_monthly.csv", index=False)

    run_load(engine, csv_dir)

    with engine.connect() as conn:
        row = conn.execute(
            select(dq_finding).where(dq_finding.c.rule_code == "MK002")
        ).mappings().one()
    assert row["severity"] == "ERROR"
    assert row["action"] == "REPAIRED"
    assert row["observed"] == "0.9875"
    assert row["replacement"] == "98.75"
    assert row["record_key"]["security_id"] == "AAA111"


def test_finding_count_in_the_table_matches_the_pipeline(engine, csv_dir, master):
    bad = master.copy()
    bad.loc[0, "sector"] = None
    bad.loc[1, "rating"] = None
    bad.to_csv(csv_dir / "security_master.csv", index=False)

    _, result = run_load(engine, csv_dir)
    assert count(engine, dq_finding) == len(result.findings) == 2


# ---------------------------------------------------------------------------
# Idempotency and integrity
# ---------------------------------------------------------------------------


def test_reloading_is_a_full_refresh_not_an_append(engine, csv_dir):
    """The extracts are a complete twelve-month snapshot, not an incremental
    feed, so a second load must replace rather than accumulate."""
    run_load(engine, csv_dir)
    first = {t: count(engine, t) for t in (security, holding, mark, trade)}

    run_load(engine, csv_dir)
    second = {t: count(engine, t) for t in (security, holding, mark, trade)}

    assert first == second
    # But the audit trail keeps both attempts.
    assert count(engine, load_run) == 2


def test_curated_grain_is_enforced_by_the_database(engine, csv_dir, holdings):
    """Belt and braces: the cleaning rules resolve duplicates, and the unique
    constraint is what proves they actually did."""
    dupe = pd.concat([holdings, holdings.iloc[[0]]], ignore_index=True)
    dupe.to_csv(csv_dir / "holdings_monthly.csv", index=False)

    run_load(engine, csv_dir)

    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(holding)
        ).scalar_one()
        distinct = conn.execute(
            select(func.count()).select_from(
                select(holding.c.as_of_date, holding.c.security_id).distinct().subquery()
            )
        ).scalar_one()
    assert n == distinct


def test_conflicting_trade_ids_both_survive_the_surrogate_key(
    engine, csv_dir, transactions
):
    """trade_id cannot be unique in the curated layer: an identifier collision is
    retained deliberately, so the table needs a surrogate key."""
    clash = transactions.iloc[0].copy()
    clash["par_amount"] = 999_000
    pd.concat([transactions, pd.DataFrame([clash])], ignore_index=True).to_csv(
        csv_dir / "transactions.csv", index=False
    )

    run_load(engine, csv_dir)

    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(trade).where(trade.c.trade_id == "T1")
        ).scalar_one()
    assert n == 2


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failed_load_commits_nothing_but_records_the_attempt(
    engine, csv_dir, monkeypatch
):
    """A half-loaded warehouse is worse than an empty one — the dashboard would
    serve numbers that look plausible and are wrong."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure mid-load")

    monkeypatch.setattr(loader_mod, "_load_findings", boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        run_load(engine, csv_dir)

    # No data committed...
    assert count(engine, security) == 0
    assert count(engine, holding) == 0
    # ...but the attempt is on the record.
    with engine.connect() as conn:
        row = conn.execute(select(load_run)).mappings().one()
    assert row["status"] == "FAILED"
    assert "simulated failure" in row["notes"]


def test_latest_load_id_ignores_failed_loads(engine, csv_dir, monkeypatch):
    good_id, _ = run_load(engine, csv_dir)

    monkeypatch.setattr(
        loader_mod, "_load_findings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        run_load(engine, csv_dir)

    assert loader_mod.latest_load_id(engine) == good_id


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


RDS_HOST = "fixed-income-db.abc123.us-east-1.rds.amazonaws.com"


def test_no_database_configuration_fails_loudly(monkeypatch):
    """No default and no fallback connection string: the assignment forbids
    credentials in the repo, so there must be nothing to fall back to."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_SECRET_ARN", raising=False)
    with pytest.raises(ConfigError, match="No database configuration"):
        Settings.from_env()


def test_database_url_takes_precedence_over_the_secret(monkeypatch):
    """Documented precedence. A stale DB_SECRET_ARN left in the environment must
    not silently override an explicit local URL."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///explicit.db")
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:unused")
    assert resolve_database_url() == "sqlite:///explicit.db"


def test_secret_arn_without_a_host_is_rejected(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.delenv("DB_HOST", raising=False)
    with pytest.raises(ConfigError, match="DB_HOST"):
        resolve_database_url()


def test_url_assembled_from_an_rds_managed_secret(monkeypatch):
    """RDS generates the master password, so it can contain URL metacharacters.
    Unquoted, an '@' or '/' silently corrupts the host or database name."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.setenv("DB_HOST", RDS_HOST)
    monkeypatch.setenv("DB_NAME", "portfolio")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    fake_secret = json.dumps({"username": "dbadmin", "password": "p@ss/w:rd"})

    class FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803 — boto3's own signature
            assert SecretId == "arn:aws:secretsmanager:::secret:rds!db-1"
            return {"SecretString": fake_secret}

    fake_boto3 = types.SimpleNamespace(client=lambda _svc, region_name=None: FakeClient())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    url = resolve_database_url()
    assert url == (
        "postgresql+psycopg://dbadmin:p%40ss%2Fw%3Ard"
        f"@{RDS_HOST}:5432/portfolio?sslmode=require"
    )
    # The raw password must not survive into the URL unescaped.
    assert "p@ss/w:rd" not in url


def test_a_malformed_secret_is_reported_as_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.setenv("DB_HOST", RDS_HOST)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    class FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": json.dumps({"user": "wrong-key"})}

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda _svc, region_name=None: FakeClient()),
    )
    with pytest.raises(ConfigError, match="RDS-managed credential"):
        resolve_database_url()


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            f"postgresql+psycopg://dbadmin:sup3rs3cret@{RDS_HOST}:5432/portfolio",
            f"postgresql+psycopg://dbadmin:***@{RDS_HOST}:5432/portfolio",
        ),
        ("sqlite:///local.db", "sqlite:///local.db"),
        ("postgresql://nopassword@host:5432/db", "postgresql://nopassword@host:5432/db"),
    ],
)
def test_redact_never_leaks_the_password(url, expected):
    """Connection targets get logged, pasted into tickets and screenshotted."""
    assert redact(url) == expected
    assert "sup3rs3cret" not in redact(url)


def test_healthcheck_reports_false_instead_of_raising():
    """The app's status endpoint must degrade, not crash.

    Uses an unopenable SQLite path rather than an unreachable Postgres host, so
    the test exercises healthcheck's error handling without requiring the psycopg
    driver to be installed just to construct the engine.
    """
    dead = make_engine(url="sqlite:////nonexistent-directory/unwritable.db")
    assert healthcheck(dead) is False


def test_healthcheck_reports_true_on_a_live_database(engine):
    assert healthcheck(engine) is True


def test_secret_resolution_requires_a_region(monkeypatch):
    """The bug that broke the first real deployment.

    boto3 does not infer the region from instance metadata, and a systemd unit
    inherits nothing from a shell — so a missing AWS_REGION surfaced as a bare
    botocore NoRegionError from deep inside endpoint resolution, giving no hint
    that one environment variable was the entire problem. It must fail as a
    ConfigError that says so.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.setenv("DB_HOST", RDS_HOST)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    with pytest.raises(ConfigError, match="region"):
        resolve_database_url()


def test_secret_resolution_uses_the_configured_region(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.setenv("DB_HOST", RDS_HOST)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    monkeypatch.setenv("DB_NAME", "portfolio")

    seen = {}

    class FakeClient:
        def get_secret_value(self, SecretId):  # noqa: N803
            return {"SecretString": json.dumps({"username": "u", "password": "p"})}

    def fake_client(service, region_name=None):
        seen["service"], seen["region"] = service, region_name
        return FakeClient()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=fake_client))
    url = resolve_database_url()
    assert seen == {"service": "secretsmanager", "region": "eu-west-2"}
    assert url.startswith("postgresql+psycopg://u:p@")


def test_a_boto_failure_becomes_a_config_error(monkeypatch):
    """Anything boto raises must arrive as ConfigError, so the WSGI entry point
    can serve a 503 naming the cause instead of gunicorn crash-looping."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_SECRET_ARN", "arn:aws:secretsmanager:::secret:rds!db-1")
    monkeypatch.setenv("DB_HOST", RDS_HOST)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def exploding_client(service, region_name=None):
        raise RuntimeError("simulated botocore failure")

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=exploding_client)
    )
    with pytest.raises(ConfigError, match="simulated botocore failure"):
        resolve_database_url()
