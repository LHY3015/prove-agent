"""Config loading. Single source of truth is configs/default.yaml (see the plan §5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# repo_root/src/prove/config.py -> repo_root/configs/default.yaml
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "configs" / "default.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load a YAML config into a plain dict. No validation beyond YAML parsing —
    modules read the keys they need; keeping it a dict avoids a schema class that
    would have to track every future config addition."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
