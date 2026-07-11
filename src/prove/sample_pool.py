"""Per-format store of validator-passed samples — the raw material for skill synthesis.

Samples are grouped by *fingerprint*, not by ground-truth format: documents that share a
fingerprint are the same format by construction (see router), so the pool discovers formats
on its own without peeking at ground truth. When a fingerprint's count reaches the synthesis
trigger (Phase 2), its samples become a synthesis job.

In-memory for Phase 1 (a run processes one stream); a persistent backing is added if/when a
long-lived service needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PoolSample:
    doc_id: str
    fingerprint: str
    text_layout: dict
    fields: dict[str, str]  # validator-passed field values
    invalidated: bool = False  # tombstoned on deprecation (kept for P4 attribution/audit)


@dataclass
class SamplePool:
    _by_fp: dict[str, list[PoolSample]] = field(default_factory=dict)

    def add(self, sample: PoolSample) -> None:
        self._by_fp.setdefault(sample.fingerprint, []).append(sample)

    def _live(self, fingerprint: str) -> list[PoolSample]:
        return [s for s in self._by_fp.get(fingerprint, []) if not s.invalidated]

    def count(self, fingerprint: str) -> int:
        return len(self._live(fingerprint))

    def samples_for(self, fingerprint: str) -> list[PoolSample]:
        return self._live(fingerprint)

    def invalidate(self, fingerprint: str) -> int:
        """Tombstone every current sample for a fingerprint (deprecation self-heal): they stop
        counting toward the trigger and are never fed to synthesis, so resynthesis re-accumulates
        from fresh post-deprecation LLM-verified docs. Rows are kept (not deleted) so Phase-4
        attribution/audit can still read the pre-drift distribution. Returns the count tombstoned."""
        n = 0
        for s in self._by_fp.get(fingerprint, []):
            if not s.invalidated:
                s.invalidated = True
                n += 1
        return n

    def fingerprints(self) -> list[str]:
        return list(self._by_fp)

    def ready(self, trigger: int) -> list[str]:
        """Fingerprints whose live sample count has reached the synthesis trigger."""
        return [fp for fp in self._by_fp if self.count(fp) >= trigger]

    def total(self) -> int:
        return sum(self.count(fp) for fp in self._by_fp)
