"""The policy engine: one entry point, one verdict."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..config import Limits
from . import limits as limit_rules
from . import rules


@dataclass(frozen=True)
class Decision:
    allowed: bool
    sql: str
    """The statement to execute. Identical to the input except for LIMIT handling."""
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    effective_limit: int | None = None

    @property
    def query_hash(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]

    def rejection_message(self) -> str:
        bullets = "\n".join(f"  - {v}" for v in self.violations)
        return f"Query rejected by mysqlpeek policy:\n{bullets}"


class PolicyEngine:
    """Decides whether a caller-supplied statement may run, and under what LIMIT.

    The engine rejects; it does not rewrite — appending or clamping a LIMIT is the one
    exception. Rewriting that tries to be clever about filters or scoping is
    deployment-specific and fails unpredictably, so it is deliberately absent.
    """

    def __init__(self, caps: Limits) -> None:
        self._caps = caps

    def evaluate(
        self,
        sql: str,
        *,
        enforce_limit: bool = True,
        default_limit: int | None = None,
    ) -> Decision:
        """Check a statement.

        Args:
            sql: The caller's statement.
            enforce_limit: False for EXPLAIN-family wrappers, which read no rows and
                whose output is a plan rather than a result set.
            default_limit: Overrides the configured default for this call only, for a
                caller that asked for a specific row count. Still clamped by max_limit.
        """
        violations = rules.check(sql)
        if violations:
            return Decision(allowed=False, sql=sql.strip(), violations=violations)

        if not enforce_limit:
            return Decision(allowed=True, sql=sql.strip().rstrip(";").rstrip())

        wanted = self._caps.default_limit if default_limit is None else default_limit
        wanted = max(1, min(wanted, self._caps.max_limit))
        outcome = limit_rules.apply(sql, wanted, self._caps.max_limit)
        return Decision(
            allowed=True,
            sql=outcome.sql,
            notes=outcome.notes,
            effective_limit=outcome.effective_limit,
        )
