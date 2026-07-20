"""Screen-recording demo: one format, one memory, from cold start to zero-token recall.

Prints one line per document so the whole loop is legible in real time — the moment that matters
is the transition from `source=llm  tokens≈290` to `source=skill  tokens=0`, which is procedural
memory replacing inference.

    export DASHSCOPE_API_KEY=...
    python project_media/live_demo.py --live          # real Qwen, ~40s
    python project_media/live_demo.py                 # key-free rehearsal, ~5s

`--live` is the one worth recording: the skill is written by qwen-coder-plus during the run, and
the file it wrote is printed at the end so it can be opened on camera.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import evals/prove

from evals.ablation import SimulatedLLM  # noqa: E402
from evals.fake_skills import FakeSynthesizer  # noqa: E402

from prove.config import load_config  # noqa: E402
from prove.datagen.generator import FORMATS, generate_dataset  # noqa: E402
from prove.layout import extract_layout  # noqa: E402
from prove.llm_client import FakeClient, build_client  # noqa: E402
from prove.pipeline import Pipeline  # noqa: E402
from prove.registry import Registry  # noqa: E402
from prove.schemas import Document, GroundTruth  # noqa: E402
from prove.traces import TraceStore  # noqa: E402
from prove.validator import field_f1  # noqa: E402

_OUT = Path(__file__).resolve().parent.parent / "evals" / "out"
_DEMO = _OUT / "live_demo"

_BOLD, _DIM, _GREEN, _CYAN, _RESET = "\033[1m", "\033[2m", "\033[32m", "\033[36m", "\033[0m"


def run(n: int, live: bool, seed: int) -> None:
    cfg = load_config()
    cfg["ablation"]["mode"] = "A3"
    fmt = FORMATS[0]

    print(f"\n{_BOLD}PROVE — one format, cold start to zero-token recall{_RESET}")
    print(f"{_DIM}format {fmt.format_id} · {n} documents · synthesis trigger at "
          f"{cfg['synthesis_trigger']} verified samples{_RESET}\n")

    manifest = generate_dataset(_DEMO / f"s{seed}", samples_per_format=n, seed=seed, formats=[fmt])

    if live:
        cfg["llm"]["provider"] = "openai_compat"
        client = build_client(cfg)
        print(f"{_DIM}extraction={cfg['model']['extraction']}  "
              f"synthesis={cfg['model']['synthesis']}  (Alibaba Cloud Model Studio){_RESET}\n")
    else:
        cfg["llm"]["provider"] = "fake"
        sim = SimulatedLLM(manifest, 0.0, seed)
        synth = FakeSynthesizer(overfit_first_k=0, mode="once")
        synth_model = cfg["model"]["synthesis"]
        client = FakeClient(
            lambda s, u, m: synth(s, u, m) if m == synth_model else sim(s, u, m),
            costs=cfg.get("costs"),
        )
        print(f"{_DIM}key-free rehearsal (deterministic doubles){_RESET}\n")

    reg_dir = _DEMO / "registry"
    registry = Registry(reg_dir / "registry.sqlite", skills_dir=reg_dir / "skills")
    pipe = Pipeline(cfg, client, trace_store=TraceStore(":memory:"), registry=registry)

    print(f"  {'doc':>4}  {'route':<7} {'served by':<9} {'tokens':>7}  {'field F1':>8}  pool")
    print(f"  {'-'*4}  {'-'*7} {'-'*9} {'-'*7}  {'-'*8}  {'-'*6}")

    announced = set()
    for i, entry in enumerate(manifest, 1):
        doc = Document(doc_id=entry["doc_id"], format_id_true=entry["format_id_true"],
                       pdf_path=entry["pdf_path"], text_layout=extract_layout(entry["pdf_path"]))
        trace = pipe.process(doc, GroundTruth(doc_id=entry["doc_id"], fields=entry["fields"]))

        tokens = trace.tokens_in + trace.tokens_out
        by_skill = trace.extraction_source == "skill"
        colour, endc = (_GREEN, _RESET) if by_skill else ("", "")
        pool = len(pipe.pool.samples_for(trace.route_fingerprint))
        print(f"  {colour}{i:>4}  {trace.route_method:<7} {trace.extraction_source:<9} "
              f"{tokens:>7}  {field_f1(trace.field_results):>8.2f}  {pool:>2}/"
              f"{cfg['synthesis_trigger']}{endc}")

        # announce lifecycle transitions the moment they happen
        for skill in registry.all_skills():
            if skill.skill_id in announced:
                continue
            announced.add(skill.skill_id)
            report = skill.admission_report or {}
            print(f"        {_CYAN}↳ synthesised {skill.skill_id} from the verified pool "
                  f"({cfg['model']['synthesis'] if live else 'deterministic double'}){_RESET}")
            if report:
                verdict = "admitted" if skill.state != "candidate" else "rejected"
                print(f"        {_CYAN}↳ admission on held-out split: F1 "
                      f"{report.get('holdout_f1', 0):.2f} over {report.get('holdout_n', 0)} "
                      f"documents → {verdict}{_RESET}")

    served = [t for t in pipe.traces.all() if t.extraction_source == "skill"]
    llm_docs = [t for t in pipe.traces.all() if t.extraction_source == "llm"]
    llm_avg = sum(t.tokens_in + t.tokens_out for t in llm_docs) / max(len(llm_docs), 1)

    print(f"\n  {_BOLD}{len(served)} of {n} documents were served from memory at "
          f"0 inference tokens{_RESET}")
    print(f"  {_DIM}the same documents through the LLM averaged {llm_avg:.0f} tokens each; "
          f"synthesis cost {pipe.lifecycle_tokens_in + pipe.lifecycle_tokens_out} tokens once"
          f"{_RESET}")

    for skill in registry.all_skills():
        if skill.state in ("trial", "active"):
            print(f"\n  {_BOLD}The memory itself — written by the model during this run:{_RESET}")
            print(f"  {skill.code_path}\n")
            break


def main() -> None:
    ap = argparse.ArgumentParser(description="PROVE live demo (for screen recording)")
    ap.add_argument("--docs", type=int, default=16)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--live", action="store_true", help="use real Qwen (spends tokens)")
    args = ap.parse_args()
    t0 = time.perf_counter()
    run(args.docs, args.live, args.seed)
    print(f"  {_DIM}elapsed {time.perf_counter() - t0:.0f}s{_RESET}\n")


if __name__ == "__main__":
    main()
