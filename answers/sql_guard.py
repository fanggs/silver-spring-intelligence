"""
Safety rails for model-generated SQL.

The model writes queries; this module decides whether we are willing to run
them. Everything here is deliberately paranoid — a generated query is
untrusted input, no matter how sensible it looks.
"""
from __future__ import annotations

import re
import sqlite3

BANNED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|"
    r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|GRANT|EXEC)\b",
    re.IGNORECASE,
)


class UnsafeQuery(Exception):
    """Raised when generated SQL fails validation."""


def clean(sql: str) -> str:
    """Strip markdown fences the model sometimes wraps SQL in."""
    sql = sql.strip()
    if sql.startswith("```"):
        sql = re.sub(r"^```(?:sql)?\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)
    return sql.strip().rstrip(";").strip()


def validate(sql: str) -> str:
    """Return the query if it is safe to run, else raise UnsafeQuery."""
    sql = clean(sql)

    if not sql:
        raise UnsafeQuery("empty query")

    # Must be a read. Allow CTEs, which legitimately start with WITH.
    head = sql.lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise UnsafeQuery("only SELECT queries are allowed")

    # One statement only — blocks "SELECT 1; DROP TABLE x"
    if ";" in sql:
        raise UnsafeQuery("multiple statements are not allowed")

    if BANNED.search(sql):
        raise UnsafeQuery("query contains a forbidden keyword")

    return sql


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open the database in a mode where writes are physically impossible."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run(db_path: str, sql: str, timeout_seconds: float = 8.0, max_rows: int = 200):
    """Validate, then execute read-only with a hard time limit."""
    sql = validate(sql)
    conn = connect_readonly(db_path)

    # Abort a runaway query instead of hanging the demo.
    deadline = {"n": 0}
    limit = int(timeout_seconds * 1_000_000 / 10)

    def guard():
        deadline["n"] += 1
        return 1 if deadline["n"] > limit else 0

    conn.set_progress_handler(guard, 10)
    try:
        rows = conn.execute(sql).fetchmany(max_rows)
        return [dict(r) for r in rows], sql
    finally:
        conn.close()
