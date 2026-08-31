"""Database schema, defined with SQLAlchemy Core.

Three layers, and the split is the main architectural decision in the project:

  raw_*        The CSVs exactly as delivered. Every column is TEXT and nothing is
               constrained, so a malformed value can never fail the insert. This
               is the point of a raw layer: it must accept whatever arrives.

  curated      Typed, constrained, cleaned. This is what the application queries.

  dq_finding   One row per anomaly detected during the load, plus load_run as the
               audit header.

Why keep the raw layer at all, given the CSVs are in the repo? Because it makes
every repair auditable: the data-quality page can show the original value beside
the corrected one, and a reviewer can verify a repair without re-reading a CSV.
It also means the data-quality page is a projection of the load rather than a
separate artifact that can silently drift out of agreement with it.

Money is NUMERIC, never float. Binary floating point cannot represent 0.01, and
summing 800-odd market values into a portfolio total is precisely where that
starts to show.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# JSONB on Postgres for indexable containment queries; plain JSON elsewhere so the
# test suite can exercise the same schema on SQLite without a server.
JSONType = JSON().with_variant(JSONB, "postgresql")

# Money and price precision. Par amounts reach eight figures, so 20 digits leaves
# headroom; prices are quoted to four decimals but stored to six.
MONEY = Numeric(20, 2)
PRICE = Numeric(14, 6)
BPS = Numeric(12, 4)
PCT = Numeric(9, 6)

# Autoincrementing surrogate key.
#
# Postgres renders BigInteger + autoincrement as BIGSERIAL, which is what we want
# in production. SQLite only aliases the rowid for a column declared exactly
# INTEGER PRIMARY KEY, so a BIGINT key there is not autoincrementing and every
# insert fails the NOT NULL check. The variant keeps one schema definition working
# on both, so the test suite exercises the real tables rather than a stand-in.
PK_BIG = BigInteger().with_variant(Integer, "sqlite")


# ---------------------------------------------------------------------------
# Load audit
# ---------------------------------------------------------------------------

load_run = Table(
    "load_run",
    metadata,
    Column("load_id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("source_dir", Text, nullable=False),
    Column("status", String(16), nullable=False, default="RUNNING"),
    Column("rows_raw", Integer),
    Column("rows_curated", Integer),
    Column("findings_error", Integer),
    Column("findings_warning", Integer),
    Column("findings_info", Integer),
    Column("notes", Text),
    CheckConstraint(
        "status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_load_run_status"
    ),
    comment="One row per invocation of the loader; the audit header for a load.",
)


# ---------------------------------------------------------------------------
# Raw layer — everything TEXT, nothing constrained
# ---------------------------------------------------------------------------


def _raw_table(name: str, *cols: str) -> Table:
    """Build a raw landing table: all TEXT, plus load provenance.

    source_row_num preserves the row's position in the delivered file, which is
    the only way to point a human back at a specific line of the original CSV.
    """
    return Table(
        name,
        metadata,
        Column("id", PK_BIG, primary_key=True, autoincrement=True),
        Column("load_id", Integer, ForeignKey("load_run.load_id"), nullable=False, index=True),
        Column("source_row_num", Integer, nullable=False),
        *(Column(c, Text) for c in cols),
        comment="Landing table: the delivered CSV verbatim, untyped and unconstrained.",
    )


raw_security_master = _raw_table(
    "raw_security_master",
    "security_id", "description", "issuer", "sector", "rating",
    "coupon_pct", "issue_date", "maturity_date", "asset_class",
)

raw_holdings_monthly = _raw_table(
    "raw_holdings_monthly",
    "as_of_date", "security_id", "par_amount", "book_value",
    "market_value", "database_date",
)

raw_marks_monthly = _raw_table(
    "raw_marks_monthly",
    "as_of_date", "security_id", "clean_price", "oas_bps",
)

raw_transactions = _raw_table(
    "raw_transactions",
    "trade_id", "trade_date", "settlement_date", "security_id",
    "trade_type", "par_amount", "price",
)


# ---------------------------------------------------------------------------
# Curated layer — what the application reads
# ---------------------------------------------------------------------------

security = Table(
    "security",
    metadata,
    Column("security_id", String(32), primary_key=True),
    Column("description", Text),
    Column("issuer", Text, index=True),
    # NOT NULL: the cleaning layer guarantees a value by defaulting unclassified
    # rows to a visible placeholder, so the allocation views never see a null.
    Column("sector", String(64), nullable=False, index=True),
    Column("rating", String(16), nullable=False, index=True),
    Column("coupon_pct", PCT),
    Column("issue_date", Date),
    Column("maturity_date", Date, index=True),
    Column("asset_class", String(64), nullable=False),
    comment="Cleaned reference data, one row per security.",
)

holding = Table(
    "holding",
    metadata,
    Column("as_of_date", Date, nullable=False),
    Column("security_id", String(32), ForeignKey("security.security_id"), nullable=False),
    Column("par_amount", MONEY),
    Column("book_value", MONEY),
    # Nullable: a position whose market value could not be imputed is retained
    # with a null rather than dropped, so par and book are not lost. Market-value
    # aggregates must therefore be null-aware.
    Column("market_value", MONEY),
    Column("market_value_imputed", Boolean, nullable=False, default=False),
    Column("database_date", Date),
    # The curated grain. Duplicate snapshots are resolved during cleaning, so this
    # constraint is what proves the resolution actually worked.
    UniqueConstraint("as_of_date", "security_id", name="uq_holding_grain"),
    Index("ix_holding_as_of_date", "as_of_date"),
    Index("ix_holding_security", "security_id"),
    comment="Cleaned month-end position snapshots, one row per security and month-end.",
)

mark = Table(
    "mark",
    metadata,
    Column("as_of_date", Date, nullable=False),
    Column("security_id", String(32), ForeignKey("security.security_id"), nullable=False),
    Column("clean_price", PRICE),
    Column("oas_bps", BPS),
    Column("clean_price_repaired", Boolean, nullable=False, default=False),
    UniqueConstraint("as_of_date", "security_id", name="uq_mark_grain"),
    Index("ix_mark_as_of_date", "as_of_date"),
    Index("ix_mark_security", "security_id"),
    comment="Cleaned month-end prices and option-adjusted spreads.",
)

trade = Table(
    "trade",
    metadata,
    # Surrogate key, not trade_id. The cleaning rules deliberately retain rows
    # that share a trade_id with conflicting details (an identifier collision is
    # not a duplicate, and dropping one would lose a real cash flow), so trade_id
    # cannot carry a uniqueness constraint.
    Column("id", PK_BIG, primary_key=True, autoincrement=True),
    Column("trade_id", String(32), nullable=False, index=True),
    Column("trade_date", Date, nullable=False, index=True),
    Column("settlement_date", Date),
    Column("security_id", String(32), ForeignKey("security.security_id"), nullable=False),
    Column("trade_type", String(16), nullable=False),
    Column("par_amount", MONEY),
    Column("price", PRICE),
    CheckConstraint(
        "trade_type IN ('BUY','SELL','MATURITY')", name="ck_trade_type"
    ),
    comment="Cleaned BUY / SELL / MATURITY activity.",
)


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

dq_finding = Table(
    "dq_finding",
    metadata,
    Column("id", PK_BIG, primary_key=True, autoincrement=True),
    Column("load_id", Integer, ForeignKey("load_run.load_id"), nullable=False, index=True),
    Column("rule_code", String(16), nullable=False, index=True),
    Column("rule_title", Text, nullable=False),
    Column("severity", String(8), nullable=False, index=True),
    Column("action", String(16), nullable=False),
    Column("source_table", String(64), nullable=False, index=True),
    # The offending record's business key. JSON because the key differs per table
    # (security_id, or security_id + as_of_date, or trade_id) and a single typed
    # column set would be mostly nulls.
    Column("record_key", JSONType, nullable=False),
    # 'column' is a reserved word in several dialects; named explicitly to avoid
    # needing quoting everywhere it appears.
    Column("column_name", String(64)),
    Column("observed", Text),
    Column("replacement", Text),
    Column("message", Text, nullable=False),
    Column("context", JSONType),
    CheckConstraint(
        "severity IN ('ERROR','WARNING','INFO')", name="ck_dq_severity"
    ),
    CheckConstraint(
        "action IN ('REPAIRED','DEDUPLICATED','EXCLUDED','IMPUTED','DEFAULTED','FLAGGED')",
        name="ck_dq_action",
    ),
    comment="One row per anomaly found during a load. The data-quality page reads this.",
)


# Convenience groupings for the loader.
RAW_TABLES = {
    "security_master": raw_security_master,
    "holdings_monthly": raw_holdings_monthly,
    "marks_monthly": raw_marks_monthly,
    "transactions": raw_transactions,
}

CURATED_TABLES = (security, holding, mark, trade)
