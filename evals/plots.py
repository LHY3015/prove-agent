"""Learning curve, cost curve, silent-failure, attribution accuracy plots.

Phase 3 ships `plot_drift_timeline` (the self-healing timeline); the rest land in Phase 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def plot_drift_timeline(
    rows: list[dict[str, Any]], markers: dict[str, int], out_path: str | Path
) -> Path:
    """Render the self-healing timeline. Each doc is a dot on one of two lanes (LLM / skill),
    coloured by validation outcome (pass/fail); vertical lines mark drift onset, deprecation,
    and re-admission. The story reads left-to-right: skill serving green -> drift -> skill red
    -> deprecate -> LLM fallback green -> re-admit -> skill green again."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    lane = {"llm": 0, "skill": 1}
    fig, ax = plt.subplots(figsize=(11, 3.2))

    for src, passed in [("llm", True), ("llm", False), ("skill", True), ("skill", False)]:
        xs = [r["index"] for r in rows if r["source"] == src and r["passed"] == passed]
        ys = [lane[src]] * len(xs)
        ax.scatter(xs, ys, s=28, marker="o" if passed else "X",
                   c="#2e7d32" if passed else "#c62828",
                   edgecolors="none", zorder=3)

    mark_style = {"drift": ("#ef6c00", "drift onset"),
                  "deprecate": ("#c62828", "deprecated"),
                  "readmit": ("#1565c0", "skill re-admitted")}
    for key, (color, label) in mark_style.items():
        if markers.get(key) is not None:
            ax.axvline(markers[key], color=color, ls="--", lw=1.4, zorder=1)
            ax.text(markers[key], 1.55, label, color=color, fontsize=8,
                    rotation=90, va="top", ha="right")

    ax.set_yticks([0, 1])
    ax.set_yticklabels(["LLM fallback", "skill"])
    ax.set_ylim(-0.6, 1.8)
    ax.set_xlabel("document index (stream order)")
    ax.set_title("PROVE self-healing under template drift (A2)")
    ax.grid(axis="x", ls=":", alpha=0.4)
    legend = [Line2D([0], [0], marker="o", ls="", mfc="#2e7d32", mec="none", label="validation pass"),
              Line2D([0], [0], marker="X", ls="", mfc="#c62828", mec="none", label="validation fail")]
    ax.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.9)

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
