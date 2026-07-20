# Live run artifacts — real Qwen, not simulated

`evals/out/` is gitignored and every simulated run overwrites it, so the one **live** run's
outputs are preserved here as evidence.

## What produced these

```bash
DASHSCOPE_API_KEY=... python -m evals.ablation --config A3 --live --samples-per-format 15 --seed 0
```

Real calls against `qwen-turbo` (extraction) and `qwen-coder-plus` (synthesis) through the
OpenAI-compatible endpoint `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, on the
14-format synthetic corpus (210 documents). Dated 2026-07-20.

| | |
| --- | --- |
| documents | 210 (179 LLM-served, 31 skill-served) |
| mean field F1 | 0.9012 |
| validation pass rate | 0.5048 |
| skills | 7 trial · 4 candidate · 0 active · 4 rejections |
| tokens | 48,424 in / 16,920 out = **65,344** (311.2 per doc) |

`*_cost_usd` fields read 0.0 because `configs/default.yaml`'s `costs:` table is empty. That is
not an oversight: Qwen Cloud bills a **credits subscription**, not per-token USD, so there is no
$/Mtok rate to fill in. Token counts are the honest unit here.

## Why 0 skills reached `active`

Promotion needs `trial_docs` clean production documents *after* admission, and synthesis itself
needs `synthesis_trigger` pooled samples first. At 15 documents per format there is not enough
traffic left after synthesis for any skill to finish its trial. Run scale, not a defect.

## The result worth reading — see the root README

Live A3 shows **11 silent failures** (35.5% of skill-served docs) where the simulated arms show
zero. All 11 are exactly one wrong field out of eight: `invoice_number` (9) and
`line_item_count` (2) — precisely the two fields with no cross-field validator rule.

Nine of them are a normalization mismatch this benchmark created: `banner.html` is the only
template that renders `#{{ invoice_number }}` while ground truth omits the `#`, so the LLM copying
the page verbatim scores as wrong on 100% of those formats' pool samples, and the skill reproduces
it. Low severity; the *mechanism* (pool-vs-ground-truth divergence surviving admission) is the
point. The other two are a real generalization defect on 5-line-item documents.

`cost_usd: 0.0` here is a null artifact of the empty `costs:` table, **not** a measurement.

**Do not quote "skills beat the LLM."** Skills only exist on the 7 formats where the LLM was good
enough to build a pool, so the raw 0.9556-vs-0.8918 comparison is a selection artifact. Like for
like on those same 7 formats the LLM scores **0.9628** and the skills **0.9556** — the skills are
marginally *worse* than the teacher they were distilled from.
