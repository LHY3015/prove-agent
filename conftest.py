"""Shared pytest fixtures. Renders a small synthetic dataset once per session (PDF
generation is the slow part) and puts the repo root on sys.path so `import evals` works."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from prove.datagen.generator import FORMATS, generate_dataset  # noqa: E402

# one format per distinct layout (classic / left_meta / banner / receipt)
_LAYOUT_REPRESENTATIVES = [FORMATS[0], FORMATS[2], FORMATS[4], FORMATS[6]]


@pytest.fixture(scope="session")
def mini_dataset(tmp_path_factory):
    """Manifest for 4 formats x 5 docs (enough item-count/value variance to lock in
    fingerprint stability). No API key, no network."""
    d = tmp_path_factory.mktemp("dataset")
    return generate_dataset(d, samples_per_format=5, seed=42, formats=_LAYOUT_REPRESENTATIVES)
