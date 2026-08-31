"""Data-quality finding model.

Every validation rule emits Findings rather than logging or raising. The loader
persists them to the `dq_finding` table and the Data Quality page renders that
table directly, so the page is a projection of the rules and cannot drift out of
sync with them. Hand the app a different extract and the same rules run again.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How much the finding should worry a reader.

    ERROR   — the row cannot be trusted as delivered and we changed or excluded it.
    WARNING — the row is usable but something is off and a human should know.
    INFO    — expected-but-notable; recorded for completeness/auditability.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class Action(str, Enum):
    """What the loader did about it. Stated per-finding so the audit trail is explicit."""

    REPAIRED = "REPAIRED"          # value corrected in the curated layer
    DEDUPLICATED = "DEDUPLICATED"  # duplicate row dropped, survivor kept
    EXCLUDED = "EXCLUDED"          # row quarantined out of the curated layer
    IMPUTED = "IMPUTED"            # missing value filled from another source
    DEFAULTED = "DEFAULTED"        # missing attribute set to a placeholder
    FLAGGED = "FLAGGED"            # left as-is, surfaced for a human


@dataclass(frozen=True)
class Finding:
    """One anomaly, in one place, detected by one rule.

    `key` identifies the offending record in business terms (not by row offset,
    which is meaningless once the CSV is reloaded in a different order).
    `observed` / `replacement` carry the before/after so the DQ page can show the
    repair rather than just assert that one happened.
    """

    rule_code: str
    rule_title: str
    severity: Severity
    action: Action
    source_table: str
    key: dict[str, Any]
    message: str
    column: str | None = None
    observed: Any = None
    replacement: Any = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Flatten for DB insert. dict/enum fields are JSON- and text-safe."""
        d = asdict(self)
        d["severity"] = self.severity.value
        d["action"] = self.action.value
        # Stringify so the column stays portable and human-readable in the UI.
        d["observed"] = None if self.observed is None else str(self.observed)
        d["replacement"] = None if self.replacement is None else str(self.replacement)
        return d

    def __str__(self) -> str:
        keypart = ", ".join(f"{k}={v}" for k, v in self.key.items())
        return f"[{self.rule_code}/{self.severity.value}] {keypart}: {self.message}"
