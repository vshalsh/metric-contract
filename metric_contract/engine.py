"""Execute metric contracts against SQLite and evaluate their declared tests.

Every result carries provenance: the contract file, the metric name, the exact
SQL that was executed and the value it returned.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import ContractPack, Metric, Tests

PASS = "PASS"
FAIL = "FAIL"


@dataclass
class Result:
    metric: str
    source: str
    status: str
    value: Any = None
    executed_sql: str | None = None
    reasons: list[str] = field(default_factory=list)
    returned: bool = False  # True once the query returned exactly one row
    tests: dict[str, str] = field(default_factory=dict)  # test -> PASS/FAIL

    def provenance(self) -> dict[str, Any]:
        return {
            "contract_file": self.source,
            "metric": self.metric,
            "executed_sql": self.executed_sql,
            "value": self.value,
            "status": self.status,
            "tests": dict(self.tests),
            "reasons": list(self.reasons),
        }


def build_sql(metric: Metric) -> str:
    """The exact statement executed for a metric."""
    sql = f'SELECT {metric.sql} AS value FROM "{metric.table}"'
    if metric.filters:
        sql += " WHERE " + " AND ".join(f"({f})" for f in metric.filters)
    return sql


class InvalidDatabaseError(Exception):
    """The --db file exists but is not a readable SQLite database."""


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        # sqlite3 connects lazily; touch the schema so a non-SQLite file fails here.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise InvalidDatabaseError(f"not a readable SQLite database: {path} ({exc})") from exc
    return conn


# Only reads are permitted while a metric executes (defence in depth on top of
# the read-only connection).
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def table_columns(conn: sqlite3.Connection, table: str) -> list[str] | None:
    """Column names of a table or view, or None if it does not exist."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (table,)
    ).fetchone()
    if row is None:
        return None
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def evaluate_tests(value: Any, tests: Tests) -> tuple[dict[str, str], list[str]]:
    """Run the declared tests on a returned value. Returns (per-test status, failure reasons)."""
    status: dict[str, str] = {}
    reasons: list[str] = []
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)

    if tests.not_null:
        if value is None:
            status["not_null"] = FAIL
            reasons.append("not_null: value is NULL")
        else:
            status["not_null"] = PASS
    if tests.non_negative:
        if not numeric:
            status["non_negative"] = FAIL
            reasons.append(f"non_negative: value {_fmt(value)} is not a number")
        elif value < 0:
            status["non_negative"] = FAIL
            reasons.append(f"non_negative: value {_fmt(value)} is below 0")
        else:
            status["non_negative"] = PASS
    if tests.between is not None:
        lo, hi = tests.between
        key = "between"
        if not numeric:
            status[key] = FAIL
            reasons.append(f"between: value {_fmt(value)} is not a number")
        elif not (lo <= value <= hi):
            status[key] = FAIL
            reasons.append(f"between: value {_fmt(value)} is outside [{_fmt(lo)}, {_fmt(hi)}]")
        else:
            status[key] = PASS
    return status, reasons


def run_metric(conn: sqlite3.Connection, metric: Metric) -> Result:
    res = Result(metric=metric.name, source=metric.source, status=FAIL)

    cols = table_columns(conn, metric.table)
    if cols is None:
        res.reasons.append(f"table '{metric.table}' does not exist in the database")
        return res
    colset = set(cols)
    for kind, names in (("grain", metric.grain), ("dimensions", metric.dimensions)):
        for c in names:
            if c not in colset:
                res.reasons.append(f"{kind} column '{c}' does not exist in table '{metric.table}'")

    res.executed_sql = build_sql(metric)
    conn.set_authorizer(_authorizer)
    try:
        rows = conn.execute(res.executed_sql).fetchall()
    except sqlite3.Error as exc:
        res.reasons.append(f"SQL error: {exc}")
        return res
    finally:
        conn.set_authorizer(None)

    if len(rows) != 1:
        res.reasons.append(f"sql must be an aggregate returning one row; got {len(rows)} rows")
        return res
    res.value = rows[0][0]
    res.returned = True

    res.tests, test_reasons = evaluate_tests(res.value, metric.tests)
    res.reasons.extend(test_reasons)
    if not res.reasons:
        res.status = PASS
    return res


def validate(pack: ContractPack, db_path: str | Path) -> list[Result]:
    """Schema failures first (in discovery order), then executed metrics."""
    results: list[Result] = []
    for (source, name), errs in pack.failed.items():
        results.append(
            Result(
                metric=name,
                source=source,
                status=FAIL,
                reasons=[f"schema: {e.field + ': ' if e.field else ''}{e.message}" for e in errs],
            )
        )
    conn = connect_readonly(db_path)
    try:
        for m in pack.metrics:
            results.append(run_metric(conn, m))
    finally:
        conn.close()
    return results


def _fmt(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return f"{value:.1f}"
        return f"{value:.6f}".rstrip("0").rstrip(".") if abs(value) >= 1e-6 else repr(value)
    return str(value)


format_value = _fmt
