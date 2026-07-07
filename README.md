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

Phase 1 (baseline pipeline) in progress. See the implementation plan for the phased
roadmap and definitions of done.

## Development

```bash
uv sync                 # create env from pyproject.toml
uv run pytest           # unit + integration tests (no API key needed)
```

Real-LLM runs go only through `evals/` scripts behind an explicit `--live` flag.
