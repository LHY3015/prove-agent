"""A0-A3 learning + cost curves and the A1-vs-A2 silent-failure figure (Phase 4e, simulated).

Runs the four ablation configs on one shared synthetic dataset (key-free FakeClient) and renders
the §4.2 figure set. A0-A3 clean shows the cost collapse as skills come online (A3 == A2 with no
faults injected); a separate A1/A2 pair with the overfit synthesiser surfaces the silent failures
the admission gate exists to block. Illustrative plumbing — the real numbers come from --live.

    python -m evals.ablation_curves   # -> evals/out/{ablation_curves.png, silent_failure.png}
"""

from __future__ import annotations

import json

from evals.ablation import _OUT_DIR, run_ablation
from evals.plots import plot_learning_and_cost, plot_silent_failure


def _rows(config: str) -> list[dict]:
    path = _OUT_DIR / f"{config}_metrics.jsonl"
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    n, seed = 25, 0

    arms = {}
    for cfg in ("A0", "A1", "A2", "A3"):
        run_ablation(cfg, samples_per_format=n, seed=seed, live=False, error_rate=0.05)
        arms[cfg] = _rows(cfg)
    curves = plot_learning_and_cost(arms, _OUT_DIR / "ablation_curves.png")

    # silent-failure demo: A1 (no gate) vs A2 (gate), overfit synthesiser on the first 2 formats
    a1 = run_ablation("A1", samples_per_format=n, seed=seed, live=False,
                      error_rate=0.05, overfit_first_k=2)
    a2 = run_ablation("A2", samples_per_format=n, seed=seed, live=False,
                      error_rate=0.05, overfit_first_k=2)
    silent = plot_silent_failure(a1, a2, _OUT_DIR / "silent_failure.png")

    print(json.dumps({
        "A0_A3_cost_per_doc_tokens": {c: round(
            sum(r["tokens_in"] + r["tokens_out"] for r in arms[c]) / len(arms[c]), 1)
            for c in arms},
        "silent_failures": {"A1": a1["silent_failure_count"], "A2": a2["silent_failure_count"]},
    }, indent=2))
    print(f"curves -> {curves}\nsilent failure -> {silent}")


if __name__ == "__main__":
    main()
