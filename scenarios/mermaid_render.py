"""Render the README's Mermaid diagrams to PNG, so they can be used outside GitHub.

GitHub renders Mermaid inline, but a submission gallery, a slide, or the demo video needs image
files. The blocks are read straight out of README.md rather than duplicated here, so the images
cannot drift from the diagrams they come from.

    python scenarios/mermaid_render.py    # -> docs/lifecycle.png, docs/attribution_peel.png

Mermaid itself is fetched from a CDN at render time and Chrome rasterises the result; neither is
a project dependency, and adding a Node toolchain for two figures is not worth it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_README = _ROOT / "README.md"
_MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"

# in README order; the names describe what each diagram is for
_NAMES = ["lifecycle", "attribution_peel"]
_SCALE = 3   # device pixels per CSS pixel, so the crop is usable as an image asset


def _chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no Chrome/Chromium found — needed to rasterise the diagrams")


def blocks() -> list[str]:
    text = _README.read_text(encoding="utf-8")
    found = re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)
    if len(found) != len(_NAMES):
        raise SystemExit(f"expected {len(_NAMES)} mermaid blocks in README.md, found {len(found)}")
    return found


def render(source: str, out: Path, css_width: int = 660, css_height: int = 860) -> Path:
    page = f"""<!doctype html><meta charset="utf-8">
<style>html,body{{margin:0;padding:24px;background:#fff;font-family:sans-serif}}
 #d{{display:inline-block}}</style>
<div id="d" class="mermaid">{source}</div>
<script src="{_MERMAID}"></script>
<script>
  mermaid.initialize({{startOnLoad:true, theme:'default',
    themeVariables:{{fontSize:'17px'}}, flowchart:{{useMaxWidth:false}} }});
</script>"""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "d.html"
        src.write_text(page, encoding="utf-8")
        subprocess.run(
            [_chrome(), "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             # window is in CSS pixels, so the scale factor divides it — a layout wider than the
             # viewport is clipped, not scaled, and `_trim` then crops a full-size partial diagram
             f"--window-size={css_width * _SCALE},{css_height * _SCALE}",
             # mermaid lays out at its natural size, which for the compact state diagram is only
             # a few hundred pixels wide; render at 3x so the crop is usable as an image asset.
             f"--force-device-scale-factor={_SCALE}",
             # the CDN script and the SVG layout both need a beat before the shot
             "--virtual-time-budget=8000",
             f"--screenshot={out}", f"file://{src}"],
            check=True, capture_output=True, timeout=180,
        )
    _trim(out)
    return out


def _trim(path: Path) -> None:
    """Crop the screenshot down to the diagram plus a margin."""
    from PIL import Image, ImageChops

    img = Image.open(path).convert("RGB")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    box = ImageChops.difference(img, bg).getbbox()
    if box:
        pad = 28
        img.crop((max(box[0] - pad, 0), max(box[1] - pad, 0),
                  min(box[2] + pad, img.width), min(box[3] + pad, img.height))).save(path)


def main() -> None:
    written = []
    sources = blocks()
    for name, source in zip(_NAMES, sources):
        out = _ROOT / "docs" / f"{name}.png"
        render(source, out)
        written.append(out)
        print(f"wrote {out}")

    # A 16:9 frame cannot carry the peel chart's top-down layout: at that aspect ratio the text
    # shrinks past legibility. The same source laid out left-to-right fills a video frame, so the
    # README keeps the tall version and the video gets a wide one.
    peel = sources[_NAMES.index("attribution_peel")]
    wide = _ROOT / "docs" / "attribution_peel_wide.png"
    render(peel.replace("flowchart TD", "flowchart LR", 1), wide,
           css_width=2200, css_height=620)
    written.append(wide)
    print(f"wrote {wide}")
    (_ROOT / "docs" / "diagram_sources.json").write_text(
        json.dumps({n: s.strip() for n, s in zip(_NAMES, blocks())}, indent=1), encoding="utf-8")
    return written


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
