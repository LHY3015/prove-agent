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


@dataclass
class SamplePool:
    _by_fp: dict[str, list[PoolSample]] = field(default_factory=dict)

    def add(self, sample: PoolSample) -> None:
        self._by_fp.setdefault(sample.fingerprint, []).append(sample)

    def count(self, fingerprint: str) -> int:
        return len(self._by_fp.get(fingerprint, []))

    def samples_for(self, fingerprint: str) -> list[PoolSample]:
        return list(self._by_fp.get(fingerprint, []))

    def fingerprints(self) -> list[str]:
        return list(self._by_fp)

    def ready(self, trigger: int) -> list[str]:
        """Fingerprints whose sample count has reached the synthesis trigger."""
        return [fp for fp, s in self._by_fp.items() if len(s) >= trigger]

    def total(self) -> int:
        return sum(len(s) for s in self._by_fp.values())
