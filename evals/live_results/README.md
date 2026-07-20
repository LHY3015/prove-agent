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

Arm 2 (`weak_xverify`) is not reproducible from the current tree: the cross-model verifier and
its `--verify-model` flag were removed after this run (see below).

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

`A3_live_*` is an earlier 210-document run at 15 docs/format.

`skills/` and `skills_strong/` hold the **verbatim parser code** the synthesiser wrote. They are
excluded from linting.

## Results

**1. Extraction-model choice, not the pipeline, drove the earlier failures.** Same prompt, same
rules, same synthesiser: validation pass rate goes 0.51 → **1.00** and field F1 0.919 → **1.00**
purely by moving extraction from qwen-turbo to qwen-plus, for 20% more tokens. qwen-turbo's
dominant error is layout-conditioned — a `Tax 8.25% 365.11` line leads it to return the *rate*
where the schema declares a money *amount*.

**2. Cross-field checks held.** Under the weak extractor,
`money_unparseable` fired 180 times and `date_unparseable` 27, keeping every one of those samples
out of the verified pool — so no skill was ever synthesised from them. Skill-served documents
recorded **zero** validation failures in every arm.

**3. Cross-model pool verification, and why it was replaced by a rule.** Arm 2 checked 138
pool-bound samples and rejected 2 (1.45%), for +12% tokens, and shrank pools enough to move
skill-served docs 115 → 76 and active skills 6 → 4. Detection on the one contaminated field was
~20-29%: the second model shares the same layout-induced confusion, so the two extractors agree on
the error. A cross-field rule (field-overlap, rule 6) caught **7/7** of the same contaminations at
zero API cost, and the verifier was removed.

**4. Attribution issued zero verdicts in every arm.** Attribution classifies
*failure batches* raised by the monitor, and the monitor watches validation outcomes. Skill-served
documents had zero validation failures, so no batch ever formed. Silent failures — which pass
validation by definition — are structurally invisible to the monitor and therefore to attribution.
They are caught at admission instead; the bottleneck for attribution is failures, not volume.

## Cost

Spend is reported in tokens. `cost_usd: 0.0` in these files is a **null artifact**, not
a measurement: cost accounting is implemented and provider-agnostic, but Qwen Cloud bills by
credit subscription with no published per-token rate table, so `costs:` in the config is empty.

Synthesis is the larger consumer: arm 1 spent 135,367 input tokens on synthesis against 83,203 on
extraction. Skill-served documents consume **zero** marginal inference tokens, which is what the
synthesis cost amortises against.

## Auditability limits

Traces store extracted field *values* and the registry is persisted per arm, so these runs are
replayable. Two limits: arm 2's per-sample disagreement values were not persisted (only counts),
and the 210-document `A3_live_*` run predates this instrumentation.
