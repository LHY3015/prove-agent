"""Capture the live demo's terminal output as timed text, for the video's terminal segments.

Records each line together with the moment it appeared, so the video can replay the session at
its real pace instead of guessing. Run this once before building the video:

    export DASHSCOPE_API_KEY=...
    python project_media/capture_terminal.py --live      # what the video should show
    python project_media/capture_terminal.py             # key-free rehearsal
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_OUT = _HERE / "terminal"


def capture(live: bool) -> Path:
    _OUT.mkdir(parents=True, exist_ok=True)
    # -u: without it Python block-buffers stdout into a pipe and every line arrives
    # at once, which would collapse the recorded pacing to a single timestamp.
    cmd = [sys.executable, "-u", str(_HERE / "live_demo.py")]
    if live:
        cmd.append("--live")

    env_note = "live (real Qwen)" if live else "rehearsal (key-free)"
    print(f"capturing live_demo.py — {env_note} …", flush=True)

    lines: list[dict] = []
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for raw in proc.stdout:
        stamp = round(time.perf_counter() - t0, 3)
        lines.append({"t": stamp, "line": raw.rstrip("\n")})
        print(f"  {stamp:6.1f}s  {raw.rstrip()}", flush=True)
    proc.wait()

    out = _OUT / "live_demo.json"
    out.write_text(json.dumps({"live": live, "lines": lines}, indent=1), encoding="utf-8")
    plain = _OUT / "live_demo.txt"
    plain.write_text("\n".join(entry["line"] for entry in lines), encoding="utf-8")
    print(f"\nwrote {out} ({len(lines)} lines, {lines[-1]['t']:.0f}s)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="capture terminal output for the demo video")
    ap.add_argument("--live", action="store_true", help="use real Qwen (spends tokens)")
    capture(ap.parse_args().live)


if __name__ == "__main__":
    main()
