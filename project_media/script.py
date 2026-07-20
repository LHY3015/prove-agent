"""Narration script for the 3-minute demo video, as data.

Each segment pairs one narration line with the visual that should be on screen while it plays.
`build_video.py` reads this, synthesises the audio through Qwen TTS, and cuts the two together,
so timing follows the spoken length rather than a guessed timestamp.

Narration is ~430 words, which lands near 3:00 at the default speaking rate.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_GALLERY = Path(__file__).resolve().parent / "Image gallery"
_TERM = Path(__file__).resolve().parent / "terminal"
_DOCS = _ROOT / "docs"

# kind:
#   "image"    — show a still (path)
#   "terminal" — play a captured terminal session (path to a .txt captured by capture_terminal.py)
#   "code"     — show a source file, syntax-highlighted (path)
SEGMENTS = [
    {
        "id": "01_problem",
        "kind": "board",
        "asset": None,
        "window": (0.0, 0.5),
        "text": (
            "A document pipeline meets the hundredth copy of the same invoice template, and calls "
            "the language model again. It solved that format ninety-nine times already and kept "
            "nothing it could reuse. Every document pays full price in tokens, and every document "
            "is one sampling accident away from a different answer."
        ),
    },
    {
        "id": "02_thesis",
        "kind": "board",
        "asset": None,
        "window": (0.5, 1.0),
        "text": (
            "PROVE compiles that experience into executable skills — Python parsers that are its "
            "procedural memory. It recalls a skill instead of re-prompting, and every write, "
            "recall and forget is decided by a deterministic downstream check, never by a model's "
            "opinion."
        ),
    },
    {
        "id": "03_loop",
        "kind": "terminal",
        "asset": _TERM / "live_demo.txt",
        "window": (0.0, 0.58),        # the miss phase, up to the first synthesis
        "text": (
            "Here is one format, live against Qwen on Alibaba Cloud. The first ten documents miss "
            "the router and go to the model, each costing about four hundred tokens, and each "
            "validated result joins a verified pool. At ten samples, synthesis fires: "
            "qwen-coder-plus writes a parser, which then has to earn its place against a held-out "
            "split it was never shown."
        ),
    },
    {
        "id": "04_zero_tokens",
        "kind": "terminal",
        "asset": _TERM / "live_demo.txt",
        "mode": "final",              # hold the completed session: rejections, then zero-token rows
        "text": (
            "Watch the gate do its job: the first two candidates miss the threshold and are "
            "rejected and rewritten. The third passes — and from there the route hits, the skill "
            "serves, and the token count is zero. A remembered format stops costing inference."
        ),
    },
    {
        "id": "05_the_memory",
        "kind": "code",
        "asset": _ROOT / "evals" / "out" / "live_demo" / "registry" / "skills",
        "text": (
            "And this is the memory itself — not an embedding, not a prompt fragment, but the "
            "parser the model wrote during that run, stored, versioned, and executed in a sandbox "
            "with no network and an import whitelist."
        ),
    },
    {
        "id": "06_forgetting",
        "kind": "image",
        "asset": _GALLERY / "03_smart_forgetting_A2_vs_A3.png",
        "text": (
            "Storing is the easy half. The hard question is which memory to forget, because a "
            "routing misdelivery, a corrupted validation rule, and a genuinely broken skill all "
            "look identical at the skill's door. Under twenty percent routing noise, charging "
            "every failure to the skill forgets four healthy memories and thrashes traffic back "
            "to the model."
        ),
    },
    {
        "id": "07_attribution",
        "kind": "image",
        # the wide layout: the README's top-down chart is unreadable at 16:9
        "asset": _DOCS / "attribution_peel_wide.png",
        "text": (
            "Attribution peels the batch per document: each deterministic test removes the "
            "failures it can account for, and only the residual may be charged to the skill. Same "
            "noise, zero healthy memories forgotten, and every planted root cause recovered."
        ),
    },
    {
        "id": "08_honest",
        "kind": "image",
        "asset": _GALLERY / "01_memory_lifecycle.png",
        "text": (
            "The live runs also found the limit. A self-supervised gate compares a candidate "
            "against a pool the extractor wrote, so a systematic extraction error survives "
            "admission — on exactly the fields no cross-field rule constrains. The fix was not a "
            "second model, which we measured and removed; it was writing the missing rule."
        ),
    },
    {
        "id": "09_close",
        "kind": "image",
        "asset": _GALLERY / "06_learning_and_cost_curves.png",
        "text": (
            "Cost per document falls as skills come online and each format stops re-invoking the "
            "model. And all of it reproduces from a single command, with or without an API key: a "
            "hundred and twenty-five tests, four live Qwen arms of four hundred and twenty "
            "documents, and the parser code the model wrote, in a public MIT repository."
        ),
    },
]

# Qwen TTS settings — the narration is generated on the same platform the project runs on.
TTS_MODEL = "qwen3-tts-flash"
TTS_VOICE = "Cherry"
TTS_LANGUAGE = "English"
