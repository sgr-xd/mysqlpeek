"""Append-only audit trail of every policy decision.

Off unless `MYSQLPEEK_AUDIT_LOG` names a path. Auditing must never be the reason a
query fails, so every error in here is swallowed after one warning to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_warned = False


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(
        self,
        *,
        tool: str,
        instance: str,
        sql: str,
        query_hash: str,
        allowed: bool,
        violations: list[str] | None = None,
        notes: list[str] | None = None,
        effective_limit: int | None = None,
        row_count: int | None = None,
        rows_read: int | None = None,
        bytes_read: int | None = None,
        elapsed_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        if self._path is None:
            return

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "instance": instance,
            "query_hash": query_hash,
            "verdict": "allowed" if allowed else "blocked",
            # A preview rather than the whole statement: the hash identifies it, and a
            # multi-kilobyte query in every line makes the log unreadable.
            "sql_preview": _preview(sql),
        }
        if violations:
            entry["violations"] = violations
        if notes:
            entry["notes"] = notes
        if effective_limit is not None:
            entry["effective_limit"] = effective_limit
        if row_count is not None:
            entry["row_count"] = row_count
        if rows_read is not None:
            entry["rows_read"] = rows_read
        if bytes_read is not None:
            entry["bytes_read"] = bytes_read
        if elapsed_ms is not None:
            entry["elapsed_ms"] = elapsed_ms
        if error:
            entry["error"] = error

        self._append(entry)

    def _append(self, entry: dict[str, Any]) -> None:
        global _warned
        assert self._path is not None
        try:
            with _lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                # 0600: an audit trail records what was asked of the database and
                # should not be world-readable.
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            if not _warned:
                _warned = True
                print(
                    f"mysqlpeek: audit log disabled, cannot write {self._path}: {exc}",
                    file=sys.stderr,
                )


def _preview(sql: str, width: int = 300) -> str:
    collapsed = " ".join(sql.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"
