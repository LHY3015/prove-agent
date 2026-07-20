"""Static architecture block-diagram of the PROVE system dataflow.

Pure matplotlib (patches + arrows) on a fixed coordinate grid — no prove imports, no
real data. Renders the hot path (document -> router -> skill/LLM -> validator -> result),
the learning loop (pool -> synthesis -> admission -> registry), and the self-healing
loop (monitor+attribution -> registry / extraction agent).

    python scenarios/architecture_diagram.py   # writes docs/architecture.png + evals/out/architecture.png
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent

AMBER, BLUE, GRAY, NEUTRAL, GREEN, RED, DARK = (
    "#f59e0b", "#2563eb", "#9ca3af", "#e5e7eb", "#16a34a", "#dc2626", "#111827",
)
MEM = "#7c3aed"  # violet: the procedural-memory-operation overlay (recall/accumulate/…)
BOX_W, BOX_H, DOC_W = 2.6, 0.9, 0.9  # DOC_W: Document sits close to Router, needs a narrower box


def _box(ax, x, y, label, color, w=BOX_W, text_color="white"):
    """Draw a centered rounded box at (x, y)."""
    ax.add_patch(FancyBboxPatch(
        (x - w / 2, y - BOX_H / 2), w, BOX_H,
        boxstyle="round,pad=0.08,rounding_size=0.12",
        facecolor=color, edgecolor=DARK, linewidth=0.8, zorder=2,
    ))
    ax.text(x, y, label, ha="center", va="center", fontsize=9, color=text_color, zorder=3)


def _edge(cx, cy, w, ox, oy):
    """Point on the boundary of a w x BOX_H box centered at (cx, cy), facing (ox, oy)."""
    vx, vy = ox - cx, oy - cy
    if vx == 0 and vy == 0:
        return (cx, cy)
    hx, hy = w / 2, BOX_H / 2
    scale = min(hx / abs(vx) if vx else float("inf"), hy / abs(vy) if vy else float("inf"))
    return (cx + vx * scale, cy + vy * scale)


def _arrow(ax, p1, p2, color=DARK, lw=1.4, style="-", rad=0.0, label=None, label_color=None):
    ax.add_patch(FancyArrowPatch(
        p1, p2, arrowstyle="-|>", mutation_scale=14, color=color, linewidth=lw,
        linestyle=style, connectionstyle=f"arc3,rad={rad}", zorder=1,
    ))
    if label:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        ax.text(mx, my, label, ha="center", va="center", fontsize=7.5,
                 color=label_color or color, backgroundcolor="white", zorder=4)


def build_figure():
    fig, ax = plt.subplots(figsize=(14, 8))
    ax.set_xlim(-0.2, 16)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.text(8, 9.55,
             "PROVE — a procedural-memory agent: it compiles experience into executable skills,\n"
             "recalls them instead of re-reasoning, and forgets only what deterministic outcomes reject",
             ha="center", va="center", fontsize=12, fontweight="bold", color=DARK)
    ax.text(8, 8.75,
             "procedural memory:   Router = recall     Pool = accumulate     "
             "Synthesis = consolidate     Registry = persist     Monitor + Attribution = smart-forget",
             ha="center", va="center", fontsize=8.5, color=MEM, style="italic")

    TOP, MID, POOL, BOT, SIDE = 7.4, 5.7, 4.2, 2.5, 4.2
    # name -> (x, y, label, color, width, text_color)
    specs = {
        "document": (0.5, TOP, "Document", NEUTRAL, DOC_W, DARK),
        "router": (2.45, TOP, "Router\nformat fingerprint", BLUE, BOX_W, "white"),
        "sandbox": (5.2, TOP, "Sandbox: skill vN\nextract() — pure Python, no net", BLUE, BOX_W, "white"),
        "validator": (8.6, TOP, "Validator\ndeterministic rules", BLUE, BOX_W, "white"),
        "result": (11.4, TOP, "Result + Trace", NEUTRAL, BOX_W, DARK),
        "extraction": (5.2, MID, "Extraction Agent (LLM)", AMBER, BOX_W, "white"),
        "pool": (8.6, POOL, "Sample Pool\n(validator-verified)", GRAY, BOX_W, "white"),
        "synthesis": (5.2, BOT, "Synthesis Agent (LLM)\nwrites extract(), self-repairs", AMBER, BOX_W, "white"),
        "admission": (8.6, BOT, "Admission Gate\nheld-out field-F1 ≥ τ", BLUE, BOX_W, "white"),
        "registry": (11.4, BOT, "Registry\ntrial → active (shared state)", GRAY, BOX_W, "white"),
        "monitor": (13.6, SIDE, "Monitor + Attribution\nroot-cause blame", BLUE, BOX_W, "white"),
    }
    for x, y, label, color, w, tc in specs.values():
        _box(ax, x, y, label, color, w=w, text_color=tc)

    def connect(a, b, **kw):
        ax_, ay_, _, _, aw, _ = specs[a]
        bx_, by_, _, _, bw, _ = specs[b]
        _arrow(ax, _edge(ax_, ay_, aw, bx_, by_), _edge(bx_, by_, bw, ax_, ay_), **kw)

    # hot path
    connect("document", "router")
    connect("router", "sandbox", lw=2.5, label="HIT")
    connect("router", "extraction", color=AMBER, lw=1.0, label="MISS")
    connect("sandbox", "validator")
    connect("extraction", "validator")
    connect("validator", "result")
    connect("validator", "pool", color=GREEN, label="pass → pool")

    # learning loop
    connect("pool", "synthesis", label="pool ≥ trigger")
    connect("synthesis", "admission")
    connect("admission", "registry", color=GREEN, label="pass")
    connect("registry", "sandbox", rad=-0.3, label="skill goes live")

    # self-healing loop
    connect("validator", "monitor", style=(0, (4, 3)), label="outcomes")
    connect("monitor", "registry", color=RED, label="deprecate")
    connect("monitor", "extraction", color=RED, style=(0, (4, 3)), rad=0.25, label="fallback to LLM")

    # legend
    lx, ly = 0.3, 1.15
    for i, (text, color) in enumerate([
        ("LLM component", AMBER), ("Deterministic component", BLUE),
        ("Shared state", GRAY), ("Pass / fail edge", "split"),
    ]):
        yy = ly - i * 0.35
        if color == "split":
            ax.add_patch(plt.Rectangle((lx, yy - 0.09), 0.18, 0.18, color=GREEN))
            ax.add_patch(plt.Rectangle((lx + 0.22, yy - 0.09), 0.18, 0.18, color=RED))
        else:
            ax.add_patch(plt.Rectangle((lx, yy - 0.09), 0.4, 0.18, color=color))
        ax.text(lx + 0.55, yy, text, ha="left", va="center", fontsize=8, color=DARK)

    ax.text(8, 0.15,
             "Every arrow into the registry is a deterministic check — no LLM ever issues a quality verdict.",
             ha="center", va="center", fontsize=8.5, color=DARK, style="italic")

    fig.tight_layout()
    return fig


def main() -> None:
    fig = build_figure()
    out_docs = _ROOT / "docs" / "architecture.png"
    out_evals = _ROOT / "evals" / "out" / "architecture.png"
    out_docs.parent.mkdir(parents=True, exist_ok=True)
    out_evals.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_docs, dpi=150)
    fig.savefig(out_evals, dpi=150)
    plt.close(fig)
    print(f"wrote {out_docs}")
    print(f"wrote {out_evals}")


if __name__ == "__main__":
    main()
