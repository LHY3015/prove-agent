# PROVE

**PROVE** (Procedural Reuse via Outcome-Verified Executables) — Outcome-Verified Skill
Lifecycle Governance for Multi-Agent LLM Pipelines.

> *Skills must prove themselves — before admission, and every day after.*

A document field-extraction pipeline where agents compile their extraction experience
into **executable skills (Python parser code)**. Skill admission, retention, and
deprecation are driven entirely by **downstream objective outcomes** (regression tests,
rule validation, production pass/fail) — never by LLM subjective scoring. An
**attribution module** performs credit assignment on production failures across four
root causes: skill defect / routing error / validation-rule defect / data drift.

## Hard design rules

1. No LLM component ever issues a quality verdict. Verdicts come only from deterministic
   downstream checks. Attribution reads objective signals + traces and assigns *blame
   accounts*; it never judges output quality.
2. Skills are pure executors — no format-detection logic. Routing is a separate component.
3. All synthesized code executes only inside the sandbox.
4. Every execution writes a structured trace from day one.

## Layout

```
prove-agent/
├── configs/default.yaml        # thresholds, model names, paths, ablation flags
├── src/prove/                  # pipeline components (see IMPLEMENTATION_PLAN §2)
├── evals/                      # ablation runner, plots, scenarios
└── tests/                      # pytest unit + integration tests
```

## Status

**Phase 3 complete — continuous monitoring + self-healing.** On top of the Phase-2 core
loop, a live monitor watches every skill's validation outcomes and self-heals under drift:

```
skill serving a format → template drift → skill's validation failures accumulate
  → monitor deprecates (sliding-window failure rate, or confidence floor) → traffic falls back to the LLM
    → pool re-accumulates fresh post-drift samples → resynthesis → new skill admitted → serving again
```

- **Monitor** (`monitor.py`): a per-`skill_id` sliding window (a resynthesized skill starts
  clean) plus the discounted-Beta confidence floor. Fast path (window failure rate over threshold,
  with an absolute failure floor so routing noise can't kill a young skill) or slow path (ledger
  floor) → deprecate. Covers trial and active skills.
- **Self-healing** (`pipeline.py`): deprecation tombstones the stale pool (rows kept for
  attribution/audit, excluded from synthesis), drops the frozen admission holdout, and opens a
  fresh synthesis *campaign* (rejection counter reset, campaign id logged). Resynthesis re-fires
  only once the pool re-reaches the trigger with fresh post-drift samples.
- **Drift demo** (`scenarios/drift_demo.py`): injects a mid-stream date-format drift and produces
  the self-healing timeline with zero manual steps:

  ```
  v1 admitted → drift at doc 30 → deprecated at doc 35 (failure_batch:window 5/20)
    → LLM fallback → v2 admitted at doc 45 → healed   (key-free; --live carries real figures)
  ```

  ![self-healing timeline](docs/drift_demo_timeline.png)
  <br>*(regenerate with `python scenarios/drift_demo.py` → `evals/out/drift_demo_timeline.png`)*

<details><summary>Phase 2 — skill synthesis + admission gate (the core loop)</summary>

A format's verified samples compile into an executable skill and the pipeline learns to serve
that format with cheap deterministic code instead of the LLM:

```
pool reaches synthesis_trigger → synthesis agent writes extract(text_layout) (self-repair in sandbox)
  → admission holds out 30% (never shown to synthesis), runs the candidate in the sandbox, scores field F1
      ├─ pass → trial (fingerprint registered in the router) → 10 clean docs → active
      └─ fail → resynthesize (max_rejections → flag for meta-review)
route hit on an active/trial skill → sandbox executes the code → validator checks → trace
```

- **Sandbox** (`sandbox.py`): every synthesized skill runs in an isolated `python -I` subprocess
  — CPU/memory rlimits, import whitelist (`re, json, datetime, decimal, math`), no I/O or network
  path, wall-timeout. Security-tested (import/open blocked, timeouts + memory caps enforced).
- **Confidence ledger** (`registry.py`): discounted-Beta counters per skill; admission seeds
  pseudo-counts from the held-out result (a skill is never born at 1.0); only attributed outcomes
  update it.
- **Ablations A0–A3** (`evals/ablation.py`): A0 baseline · A1 synthesis with **no** gate · A2 gate
  on · A3 = A2 + attribution (Phase 4). The A1-vs-A2 contrast is the headline result:

  | config | tokens/doc | skill docs | silent failures | skills |
  |---|---|---|---|---|
  | A0 (no skills) | 242 | 0 | 0 | — |
  | A2 (gate on) | **97** | 120 | 0 | 8 active |
  | A1 (no gate, overfit) | 97 | 120 | **15** — validation passes, fields wrong | 8 active |
  | A2 (same overfit stream) | 98 | 119 | **0** | overfit rejected → good skill admitted |

  Cost-per-doc drops as skills come online; **without the held-out gate an overfit skill is
  admitted and emits silent, confident, deterministic wrong fields — the gate catches the exact
  same candidate.** (Numbers above are simulated/key-free; `--live` runs carry the real figures.)

</details>

60 tests pass with no API key. Phase 4 (attribution + fault-injection evals + full ablations)
is next. See `local/IMPLEMENTATION_PLAN.md` for the roadmap.

## Development

```bash
uv sync --extra dev     # create env from pyproject.toml (Python 3.12, incl. pytest/ruff)
uv run pytest           # unit + integration tests (no API key needed)
uv run ruff check .     # lint

# ablations (simulated LLM, no API key):
python -m evals.ablation --config A0                          # baseline (pure LLM)
python -m evals.ablation --config A2                          # synthesis + admission gate
python -m evals.ablation --config A1 --overfit-first-k 1      # no gate → silent failures
python -m evals.ablation --config A2 --overfit-first-k 1      # gate rejects the overfit skill
python -m evals.ablation --config A0 --live                  # real Qwen run (spends tokens)

python scenarios/drift_demo.py                               # self-healing timeline → evals/out/
```

Real-LLM runs go only through `evals/` scripts behind an explicit `--live` flag.
