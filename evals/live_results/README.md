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

`A3_live_*` is an earlier 210-document run at 15 docs/format — see the last section.

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

## The first run (210 docs) and the mechanism it exposed

`A3_live_*` is an earlier 210-document run at 15 docs/format, against qwen-turbo extraction. It
produced **11 silent failures over 31 skill-served documents** — a result no simulated arm can
produce, and the reason the field-overlap rule exists.

The simulated arms inject a memorization-overfit candidate and measure whether the gate rejects it;
they do not sample the defect distribution of a real synthesiser. The live defect class is
different, and the gate cannot catch it by construction: admission scores a candidate against its
format's verified pool, and the pool is LLM-produced (`admission.py` compares against pool fields,
never ground truth). Where the extractor is systematically wrong on a field, the pool encodes that
error, the skill faithfully reproduces it, and admission sees agreement.

The boundary followed the rule set exactly. Every money field held; `invoice_number` (no rule at
all) and `line_item_count` (a type-only rule at that time) were the only two fields that failed
silently, and they account for all eleven. Where the validator *had* a cross-check it worked:
systematic tax-rate-vs-amount confusion on six formats and date errors on a seventh were caught by
`money_unparseable` / `date_unparseable`, keeping corrupt samples out of the pool so those seven bad
skills were never synthesised. The gate rejected 4 candidates on real code and admitted 7.

Nine of the eleven are `invoice_number` on the two `banner.html` formats — the only template that
renders `#{{ invoice_number }}` while ground truth stores the value without the `#`. The extractor
copied the page verbatim, as its prompt instructs, so those are a normalization mismatch introduced
by the benchmark's own convention rather than a misread identifier. The other two are a
generalization defect: `line_item_count` on F1_acme, wrong on exactly the documents with 5 line
items, which a 3-document holdout had no power to detect. The mechanism is the same either way — a
systematic divergence between pool and ground truth propagates into an admitted skill, and a
self-supervised oracle cannot see it.

No skill reached `active` at this run scale: promotion needs `synthesis_trigger` (10) +
`trial_docs` (10) ≈ 20+ documents per format, and this run had 15.

This run is not fully post-hoc auditable — the registry ran in-memory so the synthesised code is
gone, and traces stored per-field booleans rather than extracted values. The diagnosis rests on
per-format pool/skill correlation plus one confirming API call. Both limits were fixed for the
420-document arms above.

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
