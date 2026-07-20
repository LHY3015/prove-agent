"""Learning curve, cost curve, silent-failure, attribution accuracy plots.

Phase 3 ships `plot_drift_timeline` (the self-healing timeline); Phase 4 adds the A2-vs-A3
routing-noise comparison and the attribution confusion matrix.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

# The README renders figures from docs/, while the eval scripts generate into evals/out/ (which is
# gitignored). Every driver publishes through `publish()` so a regenerated figure reaches the
# README in the same step — a figure updated in only one of the two shows the README a stale number.
_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


def publish(path: str | Path) -> Path:
    """Copy a generated figure into docs/. Returns the published path."""
    path = Path(path)
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _DOCS_DIR / path.name
    shutil.copyfile(path, dest)
    return dest


def _rolling(values: list[float], k: int) -> list[float]:
    out, acc = [], 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= k:
            acc -= values[i - k]
        out.append(acc / min(i + 1, k))
    return out


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


def plot_routing_noise_comparison(
    a2_rows: list[dict[str, Any]], a3_rows: list[dict[str, Any]], out_path: str | Path,
    *, window: int = 20,
) -> Path:
    """The A2-vs-A3 headline under routing noise. Two stacked panels over the production stream:
    (top) rolling tokens-per-doc — A2 rebounds as wrongly-killed skills fall back to the LLM and
    resynthesize, A3 stays low; (bottom) cumulative skill deprecations — every one in this scenario
    is a healthy skill killed by mis-attributed routing failures, so A3's flat line is the payoff."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_cost, ax_kill) = plt.subplots(2, 1, figsize=(11, 6), sharex=True)

    for rows, color, label in [(a2_rows, "#c62828", "A2 (no attribution)"),
                               (a3_rows, "#1565c0", "A3 (attribution)")]:
        toks = [r["tokens"] for r in rows]
        ax_cost.plot(range(len(toks)), _rolling(toks, window), color=color, lw=2, label=label)
        kills = []
        c = 0
        for r in rows:
            c += r.get("deprecated", 0)
            kills.append(c)
        ax_kill.plot(range(len(kills)), kills, color=color, lw=2, label=label,
                     drawstyle="steps-post")

    ax_cost.set_ylabel(f"tokens / doc (rolling {window})")
    ax_cost.set_title("PROVE under routing noise: attribution keeps healthy skills alive (A2 vs A3)")
    ax_cost.legend(loc="upper left", fontsize=9)
    ax_cost.grid(ls=":", alpha=0.4)
    ax_kill.set_ylabel("cumulative deprecations\n(healthy-skill kills)")
    ax_kill.set_xlabel("production document index")
    ax_kill.legend(loc="upper left", fontsize=9)
    ax_kill.grid(ls=":", alpha=0.4)

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


def plot_confusion_matrix(
    matrix: dict[str, dict[str, int]], labels: list[str], out_path: str | Path,
) -> Path:
    """Attribution fault-injection coverage matrix: injected root cause (rows) vs attributed root
    cause (cols). `matrix[injected][attributed]` = count (shown per cell — n is small by design).
    A full diagonal means every planted signature is separable, not a statistical accuracy claim."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    grid = [[matrix.get(r, {}).get(c, 0) for c in labels] for r in labels]
    fig, ax = plt.subplots(figsize=(1.4 * len(labels) + 2, 1.4 * len(labels) + 1))
    ax.imshow(grid, cmap="Blues")
    ax.set_xticks(range(len(labels)))
    ax.set_yticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=9)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("attributed cause")
    ax.set_ylabel("injected cause")
    ax.set_title("Attribution fault-injection coverage (injected vs attributed, n per cell)")
    thresh = max((max(row) for row in grid), default=0) / 2 or 1
    for i in range(len(labels)):
        for j in range(len(labels)):
            ax.text(j, i, grid[i][j], ha="center", va="center", fontsize=10,
                    color="white" if grid[i][j] > thresh else "black")

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


def plot_learning_and_cost(
    arms: dict[str, list[dict[str, Any]]], out_path: str | Path, *, window: int = 20,
) -> Path:
    """A0-A3 side by side over the document stream: (left) cumulative mean field-F1 — skills track
    or beat the LLM once admitted; (right) rolling tokens-per-doc — the cost collapses toward zero
    as skill hit-rate rises ("the agent learns to replace itself with cheap deterministic code")."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"A0": "#616161", "A1": "#ef6c00", "A2": "#c62828", "A3": "#1565c0"}
    fig, (ax_f1, ax_cost) = plt.subplots(1, 2, figsize=(13, 4.2))
    for name, rows in arms.items():
        c = colors.get(name, "#000")
        f1 = [r["field_f1"] for r in rows]
        cum = [sum(f1[: i + 1]) / (i + 1) for i in range(len(f1))]
        ax_f1.plot(range(len(cum)), cum, color=c, lw=2, label=name)
        toks = [r["tokens_in"] + r["tokens_out"] for r in rows]
        ax_cost.plot(range(len(toks)), _rolling(toks, window), color=c, lw=2, label=name)

    ax_f1.set_xlabel("document index")
    ax_f1.set_ylabel("cumulative mean field-F1")
    ax_f1.set_title("Learning curve")
    ax_f1.legend(fontsize=9)
    ax_f1.grid(ls=":", alpha=0.4)
    ax_cost.set_xlabel("document index")
    ax_cost.set_ylabel(f"tokens / doc (rolling {window})")
    ax_cost.set_title("Cost curve")
    ax_cost.legend(fontsize=9)
    ax_cost.grid(ls=":", alpha=0.4)

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path


def plot_silent_failure(a1: dict[str, Any], a2: dict[str, Any], out_path: str | Path) -> Path:
    """A1-vs-A2 silent failures: skill outputs that PASS validation yet carry a wrong field. The
    ungated arm (A1) admits an overfit skill and ships them confidently; the held-out gate (A2)
    rejects the same candidate, so the count drops to zero — the single figure worth the project."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5.2, 4.2))
    names = ["A1 (no gate)", "A2 (admission gate)"]
    counts = [a1.get("silent_failure_count", 0), a2.get("silent_failure_count", 0)]
    ax.bar(names, counts, color=["#c62828", "#1565c0"], width=0.55)
    for i, v in enumerate(counts):
        ax.text(i, v, str(v), ha="center", va="bottom", fontsize=11)
    ax.set_ylabel("silent wrong-field docs (validation-passing)")
    ax.set_title("Silent deterministic failures: gate off vs on")
    ax.grid(axis="y", ls=":", alpha=0.4)

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
