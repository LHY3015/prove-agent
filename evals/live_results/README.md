# Live run artifacts — real Qwen, not simulated

`evals/out/` is gitignored and every run overwrites it, so the **live** arms are preserved here.
All arms use the same 14-format synthetic corpus and the same synthesiser
(`qwen-coder-plus`); only the extraction model and the verification setup differ.

```bash
DASHSCOPE_API_KEY=... python -m evals.ablation --config A3 --live \
    --samples-per-format 30 --seed 0 --tag <arm> [--extraction-model qwen-plus]
```

Endpoint: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. Dated 2026-07-20.

## The arms

Arm 2 (`weak_xverify`) **cannot be reproduced from the current tree** — the cross-model verifier
and its `--verify-model` flag were deleted after this run, for the reasons under "What these arms
establish" below. Its artifacts are kept as the evidence that justified the deletion.

| | arm 1 `weak` | arm 2 `weak_xverify` † | arm 3 `strong` |
| --- | --- | --- | --- |
| extraction model | qwen-turbo | qwen-turbo | **qwen-plus** |
| pool verification | — | second extractor (qwen-max) | — |
| mean field F1 | 0.9190 | 0.9214 | **1.0000** |
| validation pass rate | 0.5071 | 0.5048 | **1.0000** |
| skill-served docs (of 420) | 115 | 76 | **198** |
| silent failures | 0 | 0 | 0 |
| skills | 6 active · 8 candidate | 4 active · 11 candidate | **10 active** · 14 candidate |
| admission rejections | 8 | 11 | 14 |
| total tokens | 260,669 | 292,289 | 312,788 |

`A3_live_*` is the earlier 210-document run at 15 docs/format that first exposed the
silent-failure mechanism; it is kept because it is the evidence for that finding.

`skills/` and `skills_strong/` hold the **verbatim parser code** the synthesiser wrote, kept as
artifacts. They are excluded from linting — editing them would destroy what they document.

## What these arms establish

**1. Extraction-model choice, not the pipeline, drove the earlier failures.** Same prompt, same
rules, same synthesiser: validation pass rate goes 0.51 → **1.00** and field F1 0.919 → **1.00**
purely by moving extraction from qwen-turbo to qwen-plus, for 20% more tokens. qwen-turbo's
dominant error is layout-conditioned — a `Tax 8.25% 365.11` line leads it to return the *rate*
where the schema declares a money *amount*.

**2. The validator earns its keep where it has a cross-field check.** Under the weak extractor,
`money_unparseable` fired 180 times and `date_unparseable` 27, keeping every one of those samples
out of the verified pool — so no skill was ever synthesised from them. Skill-served documents
recorded **zero** validation failures in every arm.

**3. Cross-model pool verification was measured, found net-negative, and removed.** Arm 2 checked
138 pool-bound samples and rejected 2 (1.45%), for +12% tokens — and it *slowed skill formation*,
since rejecting samples shrinks pools (115 → 76 skill-served docs, 6 → 4 active skills). Its
detection rate on the one genuinely contaminated field was ~20-29%, because the second model
shares the same layout-induced confusion: **correlated errors defeat cross-model agreement**, and
layout ambiguity produces correlated errors by construction. A deterministic cross-field rule
(field-overlap, rule 6) caught **7/7** of the same contaminations at zero API cost, so the module
was deleted rather than kept as unused surface.

**4. Attribution issued zero verdicts in every arm, and that is correct.** Attribution classifies
*failure batches* raised by the monitor, and the monitor watches validation outcomes. Skill-served
documents had zero validation failures, so no batch ever formed. Silent failures — which pass
validation by definition — are structurally invisible to the monitor and therefore to attribution.
They are the admission gate's problem, not the accountant's. Earlier framing that more traffic per
format would make attribution fire was wrong: the bottleneck is failures, not volume.

## Cost

Tokens are the unit here, not dollars. `cost_usd: 0.0` in these files is a **null artifact**, not
a measurement: cost accounting is implemented and provider-agnostic, but Qwen Cloud bills by
credit subscription with no published per-token rate table, so `costs:` in the config is empty.

Synthesis is the larger consumer and was previously invisible — arm 1 spent 135,367 input tokens
on synthesis against 83,203 on extraction. Skill-served documents consume **zero** marginal
inference tokens; amortisation is the mechanism, and the synthesis cost is what it amortises
against.

## Auditability limits

Traces store extracted field *values* and the registry is persisted per arm, so these runs are
replayable — both were added after the first live run proved un-auditable. Two limits remain:
arm 2's per-sample disagreement values were not persisted (only counts), and the 210-document
`A3_live_*` run predates all of this instrumentation.
