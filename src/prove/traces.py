"""Structured trace store (SQLite) — one row per document processed (Hard Design Rule 4).

The trace is the attribution module's ONLY data source, so it is written from day one and
carries everything a later root-cause analysis needs: the route decision + confidence, the
executor identity, per-field correctness (eval mode), the full validation verdict, and cost.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schemas import Trace, ValidationVerdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id           TEXT NOT NULL,
    ts               REAL NOT NULL,
    route_format_id  TEXT,
    route_confidence REAL,
    route_method     TEXT,
    route_fingerprint TEXT,
    skill_id         TEXT,
    skill_version    INTEGER,
    extraction_source TEXT,
    field_results    TEXT,   -- json: {field: bool}
    validation       TEXT,   -- json: ValidationVerdict
    cost_usd         REAL,
    tokens_in        INTEGER,
    tokens_out       INTEGER
);
"""


class TraceStore:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def write(self, trace: Trace) -> None:
        self._conn.execute(
            """INSERT INTO traces (doc_id, ts, route_format_id, route_confidence,
                route_method, route_fingerprint, skill_id, skill_version, extraction_source,
                field_results, validation, cost_usd, tokens_in, tokens_out)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trace.doc_id,
                trace.ts,
                trace.route_format_id,
                trace.route_confidence,
                trace.route_method,
                trace.route_fingerprint,
                trace.skill_id,
                trace.skill_version,
                trace.extraction_source,
                json.dumps(trace.field_results),
                trace.validation.model_dump_json(),
                trace.cost_usd,
                trace.tokens_in,
                trace.tokens_out,
            ),
        )
        self._conn.commit()

    def _row_to_trace(self, row: sqlite3.Row) -> Trace:
        return Trace(
            doc_id=row["doc_id"],
            ts=row["ts"],
            route_format_id=row["route_format_id"],
            route_confidence=row["route_confidence"],
            route_method=row["route_method"],
            route_fingerprint=row["route_fingerprint"],
            skill_id=row["skill_id"],
            skill_version=row["skill_version"],
            extraction_source=row["extraction_source"],
            field_results=json.loads(row["field_results"]) if row["field_results"] else {},
            validation=ValidationVerdict.model_validate_json(row["validation"]),
            cost_usd=row["cost_usd"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
        )

    def all(self) -> list[Trace]:
        rows = self._conn.execute("SELECT * FROM traces ORDER BY id").fetchall()
        return [self._row_to_trace(r) for r in rows]

    def recent(self, n: int = 50) -> list[Trace]:
        rows = self._conn.execute(
            "SELECT * FROM traces ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [self._row_to_trace(r) for r in reversed(rows)]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
