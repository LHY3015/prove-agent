"""Render the explainer's pipeline board as a still, so README, animation and video share one look.

The board inside `docs/prove_explainer.html` is plain SVG — the animation only moves a token over
it — so the still is the same artwork rather than a redrawing of it, and it cannot drift from the
animation as the diagram evolves.

    python scenarios/board_diagram.py     # -> docs/pipeline_board.png (+ evals/out/)

Needs a Chrome/Chromium binary for headless rendering; there is no SVG rasteriser in the project's
dependency set and adding one for a single figure is not worth the install.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "docs" / "prove_explainer.html"
_SCALE = 2  # 1120x560 board -> 2240x1120


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no Chrome/Chromium found — needed to rasterise the SVG board")


def extract() -> tuple[str, str]:
    """Return (svg markup, the page's whole stylesheet).

    The stylesheet is carried over wholesale rather than filtered down to the board's selectors:
    node fills are `color-mix()` over custom properties, so dropping any part of the variable
    block silently renders every node black instead of failing loudly.
    """
    html = _SRC.read_text(encoding="utf-8")
    start = html.index('<svg class="board"')
    svg = html[start:html.index("</svg>", start) + 6]
    css = html[html.index("<style") : html.index("</style>")]
    css = css[css.index(">") + 1:]
    return _for_print(svg), css


def _for_print(svg: str) -> str:
    """Adapt the animated board for a still.

    Three differences from the animation: the skill version is a live counter there (it ticks to
    v2 when a drifted skill is relearned) and would freeze at an arbitrary number here; the closing
    line belongs to the animation's narration, not to a figure; and a still has no motion to show
    which node kinds differ, so the colour coding needs a legend.
    """
    svg = svg.replace('skill v<tspan id="ver">1</tspan>', "skill vN")
    svg = re.sub(r'<text[^>]*y="548"[^>]*>.*?</text>\s*', "", svg, flags=re.DOTALL)

    swatches = [
        ("n-llm", "LLM component"),
        ("n-det", "deterministic component"),
        ("n-state", "shared state"),
    ]
    parts, x = [], 262
    for cls, label in swatches:
        parts.append(
            f'<g class="node {cls}" transform="translate({x},512)">'
            f'<rect width="26" height="17" rx="5"/></g>'
            f'<text class="elabel board-legend" x="{x + 34}" y="525">{label}</text>'
        )
        x += 230
    return svg.replace("</svg>", "".join(parts) + "</svg>")


def _shoot(svg: str, css: str, out: Path) -> Path:
    page = (
        "<!doctype html><meta charset='utf-8'><style>\n"
        "html,body{margin:0;padding:0;background:#ffffff}\n"
        f"svg.board{{width:{1120 * _SCALE}px;height:{560 * _SCALE}px;display:block}}\n"
        f"{css}\n</style>\n{svg}\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "board.html"
        src.write_text(page, encoding="utf-8")
        subprocess.run(
            [_chrome(), "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             f"--window-size={1120 * _SCALE},{560 * _SCALE}",
             f"--screenshot={out}", f"file://{src}"],
            check=True, capture_output=True, timeout=120,
        )
    return out


def render(out: Path) -> Path:
    svg, css = extract()
    return _shoot(svg, css, out)


# reveal order: the path a document actually takes, so the board assembles as the story is told
_REVEAL = [
    "Document", "Router", "Extraction Agent", "Validator", "Sample Pool",
    "Synthesis Agent", "Admission", "Registry", "Sandbox", "Result", "Monitor +",
]


def _stage(svg: str, shown: int) -> str:
    """Hide every node past the first `shown` of the reveal order, and any edge that would
    dangle from one. Edges carry no identity, so they follow the node count rather than being
    matched individually — close enough at a glance, and it keeps this to one pass."""
    visible = set(_REVEAL[:shown])
    out, hidden_nodes = [], 0
    for chunk in re.split(r'(?=<g class="node )', svg):
        label = re.search(r"<text[^>]*>([^<]+)", chunk)
        name = label.group(1).strip() if label else ""
        is_node = chunk.startswith('<g class="node ')
        legend = 'y="525"' in chunk or "component" in name or name == "shared state"
        if is_node and not legend and not any(name.startswith(v) for v in visible):
            chunk = chunk.replace('<g class="node ', '<g opacity="0" class="node ', 1)
            hidden_nodes += 1
        out.append(chunk)
    svg = "".join(out)

    if hidden_nodes:
        # Edges have no identity to match against nodes, so they fade in as a layer — but only
        # once half the board is up. Ramping them from the first stage draws lines into empty
        # space, which reads as a rendering fault rather than a build-up.
        progress = shown / len(_REVEAL)
        keep = max(0.0, (progress - 0.5) * 2)
        # inline style, not the opacity attribute: `.edge.miss{opacity:.75}` and friends are CSS
        # rules, and a class rule outranks a presentation attribute — setting the attribute hides
        # only the edges that have no colour rule of their own.
        svg = svg.replace('<path class="edge', f'<path style="opacity:{keep:.2f}" class="edge')
        # only edge labels ramp; the legend is chrome, not part of the flow
        # every elabel variant (`elabel hot`, `elabel miss`, …) ramps with its edge; the legend
        # carries `elabel board-legend` and is excluded, since it is chrome rather than part of the flow
        svg = re.sub(r'<text class="(elabel(?![^"]*board-legend)[^"]*)"',
                     lambda m: f'<text style="opacity:{keep:.2f}" class="{m.group(1)}"', svg)
    return svg


def reveal_frames(out_dir: Path, stages: int = 12) -> list[Path]:
    """Render the board assembling itself, one still per stage."""
    svg, css = extract()
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("stage*.png"):
        stale.unlink()      # a cached set from an older reveal silently survives a code change
    frames = []
    for i in range(stages):
        shown = round((i + 1) * len(_REVEAL) / stages)
        dest = out_dir / f"stage{i:02d}.png"
        _shoot(_stage(svg, shown), css, dest)
        frames.append(dest)
    return frames


def main() -> None:
    out = _ROOT / "docs" / "pipeline_board.png"
    render(out)
    mirror = _ROOT / "evals" / "out" / "pipeline_board.png"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out, mirror)
    print(f"wrote {out}\nwrote {mirror}")


if __name__ == "__main__":
    sys.exit(main())
