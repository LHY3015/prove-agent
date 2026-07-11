"""SQLite-backed skill registry: state machine + discounted-Beta confidence ledger.

Two tables: `skills` (one row per synthesized skill, current state + ledger counters) and
`skill_events` (append-only audit log of every transition / outcome — the lifecycle's
paper trail, distinct from per-document `traces`).

State machine: candidate -> trial -> active -> deprecated (A1 shortcut: candidate -> active,
skipping the admission gate). Skill code lives on disk (`code_path`); the DB holds metadata.

Confidence ledger (discounted Beta, design in PROJECT_RECORD §3.6): per skill two counters,
`alpha`/`beta`, prior 1 each. Admission seeds pseudo-counts from the holdout F1 so a skill is
never born at confidence 1.0. Every *attributed* outcome discounts then increments:
`alpha <- gamma*alpha + success`, `beta <- gamma*beta + failure`; confidence = alpha/(alpha+beta).
The decay bounds effective sample size at ~1/(1-gamma) so old history cannot make the mean inert.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .schemas import Skill

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id          TEXT PRIMARY KEY,
    format_id         TEXT NOT NULL,
    version           INTEGER NOT NULL,
    code_path         TEXT NOT NULL,
    state             TEXT NOT NULL,
    alpha             REAL NOT NULL,
    beta              REAL NOT NULL,
    admission_report  TEXT,
    schema_version    INTEGER,
    created_ts        REAL NOT NULL,
    deprecated_reason TEXT
);
CREATE TABLE IF NOT EXISTS skill_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id   TEXT NOT NULL,
    ts         REAL NOT NULL,
    event_type TEXT NOT NULL,
    detail     TEXT,
    cost_usd   REAL DEFAULT 0.0
);
"""


class Registry:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        skills_dir: Optional[str | Path] = None,
        *,
        prior: float = 1.0,
        decay: float = 0.97,
    ):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self.prior = prior
        self.decay = decay
        self.skills_dir = Path(skills_dir) if skills_dir else Path(self.db_path).parent / "skills"
        if self.db_path == ":memory:" and skills_dir is None:
            import tempfile

            self.skills_dir = Path(tempfile.mkdtemp(prefix="prove_skills_"))
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ---- creation --------------------------------------------------------

    def create_candidate(self, format_id: str, code: str, schema_version: int = 1) -> Skill:
        """Persist a new candidate skill's code and metadata (ledger at prior only)."""
        version = self._next_version(format_id)
        skill_id = f"{format_id}-v{version}"
        code_path = self.skills_dir / f"{skill_id}.py"
        code_path.write_text(code, encoding="utf-8")
        ts = time.time()
        self._conn.execute(
            """INSERT INTO skills (skill_id, format_id, version, code_path, state, alpha, beta,
                admission_report, schema_version, created_ts, deprecated_reason)
               VALUES (?,?,?,?, 'candidate', ?, ?, '{}', ?, ?, NULL)""",
            (skill_id, format_id, version, str(code_path), self.prior, self.prior,
             schema_version, ts),
        )
        self._conn.commit()
        self._log(skill_id, "created", {"format_id": format_id, "version": version})
        return self.get_skill(skill_id)

    def _next_version(self, format_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM skills WHERE format_id = ?", (format_id,)
        ).fetchone()
        return (row["v"] or 0) + 1

    # ---- state transitions ----------------------------------------------

    def admit_to_trial(self, skill_id: str, admission_report: dict) -> Skill:
        """Admission passed: seed the ledger from the holdout result and enter trial.
        alpha = prior + f1*H, beta = prior + (1-f1)*H  (H = holdout doc count)."""
        f1 = float(admission_report.get("holdout_f1", 0.0))
        h = int(admission_report.get("holdout_n", 0))
        alpha = self.prior + f1 * h
        beta = self.prior + (1.0 - f1) * h
        self._conn.execute(
            "UPDATE skills SET state='trial', alpha=?, beta=?, admission_report=? WHERE skill_id=?",
            (alpha, beta, json.dumps(admission_report), skill_id),
        )
        self._conn.commit()
        self._log(skill_id, "admitted_trial", admission_report)
        return self.get_skill(skill_id)

    def activate(self, skill_id: str) -> Skill:
        self._conn.execute("UPDATE skills SET state='active' WHERE skill_id=?", (skill_id,))
        self._conn.commit()
        self._log(skill_id, "activated", {})
        return self.get_skill(skill_id)

    def admit_direct_active(self, skill_id: str, admission_report: Optional[dict] = None) -> Skill:
        """A1 shortcut: no held-out gate — candidate goes straight to active (prior ledger)."""
        self._conn.execute(
            "UPDATE skills SET state='active', admission_report=? WHERE skill_id=?",
            (json.dumps(admission_report or {"gate": "disabled"}), skill_id),
        )
        self._conn.commit()
        self._log(skill_id, "activated_no_gate", admission_report or {})
        return self.get_skill(skill_id)

    def reject(self, skill_id: str, report: dict) -> None:
        """Admission failed: record the rejection event (skill stays candidate for the caller
        to discard / resynthesize)."""
        self._log(skill_id, "rejected", report)

    def deprecate(self, skill_id: str, reason: str) -> Skill:
        self._conn.execute(
            "UPDATE skills SET state='deprecated', deprecated_reason=? WHERE skill_id=?",
            (reason, skill_id),
        )
        self._conn.commit()
        self._log(skill_id, "deprecated", {"reason": reason})
        return self.get_skill(skill_id)

    # ---- confidence ledger ----------------------------------------------

    def record_outcome(self, skill_id: str, success: bool, *, attributed: bool = True) -> Skill:
        """Discounted-Beta update on an *attributed* production/trial outcome. When
        `attributed` is False (routing_error / rule_defect, Phase 4), the ledger is left
        untouched — only the event is logged."""
        skill = self.get_skill(skill_id)
        if attributed:
            s = 1.0 if success else 0.0
            f = 0.0 if success else 1.0
            alpha = self.decay * skill.alpha + s
            beta = self.decay * skill.beta + f
            self._conn.execute(
                "UPDATE skills SET alpha=?, beta=? WHERE skill_id=?", (alpha, beta, skill_id)
            )
            self._conn.commit()
        self._log(
            skill_id,
            "outcome",
            {"success": success, "attributed": attributed},
        )
        return self.get_skill(skill_id)

    # ---- queries ---------------------------------------------------------

    def get_skill(self, skill_id: str) -> Skill:
        row = self._conn.execute(
            "SELECT * FROM skills WHERE skill_id=?", (skill_id,)
        ).fetchone()
        if row is None:
            raise KeyError(skill_id)
        return self._row_to_skill(row)

    def get_code(self, skill_id: str) -> str:
        return Path(self.get_skill(skill_id).code_path).read_text(encoding="utf-8")

    def serving_skill(self, format_id: str) -> Optional[Skill]:
        """The skill that should execute this format's traffic: active preferred, else trial."""
        for state in ("active", "trial"):
            row = self._conn.execute(
                "SELECT * FROM skills WHERE format_id=? AND state=? ORDER BY version DESC LIMIT 1",
                (format_id, state),
            ).fetchone()
            if row:
                return self._row_to_skill(row)
        return None

    def all_skills(self) -> list[Skill]:
        rows = self._conn.execute("SELECT * FROM skills ORDER BY created_ts").fetchall()
        return [self._row_to_skill(r) for r in rows]

    def log_event(self, skill_id: str, event_type: str, detail: dict, cost_usd: float = 0.0) -> None:
        """Public audit-log hook (e.g. synthesis cost/attempts) — distinct from ledger updates."""
        self._log(skill_id, event_type, detail, cost_usd)

    def events(self, skill_id: Optional[str] = None) -> list[dict]:
        if skill_id:
            rows = self._conn.execute(
                "SELECT * FROM skill_events WHERE skill_id=? ORDER BY id", (skill_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM skill_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ---- internals -------------------------------------------------------

    def _row_to_skill(self, row: sqlite3.Row) -> Skill:
        return Skill(
            skill_id=row["skill_id"],
            format_id=row["format_id"],
            version=row["version"],
            code_path=row["code_path"],
            state=row["state"],
            alpha=row["alpha"],
            beta=row["beta"],
            admission_report=json.loads(row["admission_report"] or "{}"),
            created_ts=row["created_ts"],
            deprecated_reason=row["deprecated_reason"],
        )

    def _log(self, skill_id: str, event_type: str, detail: dict, cost_usd: float = 0.0) -> None:
        self._conn.execute(
            "INSERT INTO skill_events (skill_id, ts, event_type, detail, cost_usd) VALUES (?,?,?,?,?)",
            (skill_id, time.time(), event_type, json.dumps(detail), cost_usd),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
