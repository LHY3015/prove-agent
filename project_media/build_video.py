"""Assemble the 3-minute demo video from the narration script, with no screen recording.

    export DASHSCOPE_API_KEY=...
    python project_media/build_video.py            # -> project_media/prove_demo.mp4

Pipeline: each segment's narration is synthesised through Qwen TTS (the same platform the project
runs on), its visual is rendered to frames, and the segment is cut to the spoken length so timing
follows the audio rather than a guessed timestamp. Segments are then concatenated.

Visual kinds:
  image     a still, letterboxed onto the canvas
  terminal  the captured session replayed at its recorded pace, compressed to fit the narration
  code      the parser the model wrote, revealed as a scrolling still

Re-run after changing `script.py` or re-capturing the terminal; nothing here depends on a live
window, so the output is reproducible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))

from board_diagram import reveal_frames  # noqa: E402
from script import SEGMENTS, TTS_LANGUAGE, TTS_MODEL, TTS_VOICE  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

_HERE = Path(__file__).resolve().parent
_BUILD = _HERE / "build"
_AUDIO = _BUILD / "audio"
_FRAMES = _BUILD / "frames"

W, H, FPS = 1920, 1080, 30
BG, FG, DIM, GREEN, CYAN, ACCENT = "#0d1117", "#e6edf3", "#7d8590", "#3fb950", "#39c5cf", "#58a6ff"

_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
_SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_TTS_URL = "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_ANSI = __import__("re").compile(r"\033\[[0-9;]*m")
_BOARD_STAGES = None   # rendered once per run, shared by the opening segments


# --------------------------------------------------------------------------- narration


def synthesise(text: str, dest: Path) -> float:
    """Generate one narration clip through Qwen TTS. Returns its duration in seconds."""
    if not dest.exists():
        key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not key:
            raise SystemExit("DASHSCOPE_API_KEY not set — narration needs Qwen TTS")
        body = json.dumps({
            "model": TTS_MODEL,
            "input": {"text": text, "voice": TTS_VOICE},
            "parameters": {"language_type": TTS_LANGUAGE},
        }).encode()
        req = urllib.request.Request(
            _TTS_URL, data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        url = payload["output"]["audio"]["url"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, dest)

    # the streamed wav carries a placeholder length field, so measure from the data itself
    with wave.open(str(dest)) as w:
        frames = dest.stat().st_size - 44
        return frames / (w.getframerate() * w.getnchannels() * w.getsampwidth())


# --------------------------------------------------------------------------- rendering


def _canvas() -> Image.Image:
    return Image.new("RGB", (W, H), BG)


def _caption(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle([0, H - 96, W, H], fill="#010409")
    d.text((64, H - 66), text, font=ImageFont.truetype(_SANS_B, 30), fill=DIM)


def render_image(asset: Path, caption: str) -> Image.Image:
    img = _canvas()
    art = Image.open(asset).convert("RGB")
    box_w, box_h = W - 200, H - 260
    art.thumbnail((box_w, box_h), Image.LANCZOS)
    img.paste(art, ((W - art.width) // 2, (H - 96 - art.height) // 2))
    _caption(img, caption)
    return img


def render_terminal(lines: list[str], caption: str) -> Image.Image:
    img = _canvas()
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(_MONO, 26)
    d.rectangle([56, 40, W - 56, H - 120], outline="#30363d", width=2)
    d.text((80, 62), "prove — live against Qwen on Alibaba Cloud",
           font=ImageFont.truetype(_MONO_B, 24), fill=DIM)
    y = 110
    for raw in lines[-30:]:
        colour = FG
        if "\033[32m" in raw:
            colour = GREEN
        elif "\033[36m" in raw:
            colour = CYAN
        elif "\033[1m" in raw:
            colour = ACCENT
        elif "\033[2m" in raw:
            colour = DIM
        d.text((88, y), _ANSI.sub("", raw)[:104], font=font, fill=colour)
        y += 31
    _caption(img, caption)
    return img


def render_board(frame: Path, caption: str) -> Image.Image:
    """Letterbox one board stage onto the video canvas."""
    img = _canvas()
    art = Image.open(frame).convert("RGB")
    art.thumbnail((W - 120, H - 200), Image.LANCZOS)
    img.paste(art, ((W - art.width) // 2, (H - 96 - art.height) // 2))
    _caption(img, caption)
    return img


def render_code(path: Path, caption: str) -> Image.Image:
    img = _canvas()
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(_MONO, 23)
    d.rectangle([56, 40, W - 56, H - 120], outline="#30363d", width=2)
    d.text((80, 62), f"{path.name} — written by qwen-coder-plus during the run",
           font=ImageFont.truetype(_MONO_B, 23), fill=CYAN)
    y = 108
    for raw in path.read_text(encoding="utf-8").splitlines()[:30]:
        line = raw[:100]
        colour = FG
        if line.strip().startswith(("import", "def", "return")):
            colour = ACCENT
        elif "re.search" in line or "re.match" in line:
            colour = GREEN
        d.text((88, y), line, font=font, fill=colour)
        y += 28
    _caption(img, caption)
    return img


# --------------------------------------------------------------------------- segments


def _newest_skill(folder: Path) -> Path:
    skills = sorted(folder.glob("*.py"))
    if not skills:
        raise SystemExit(f"no synthesised skill under {folder} — run capture_terminal.py --live")
    return skills[-1]


def build_segment(seg: dict, index: int, duration: float) -> Path:
    """Render one segment to a silent mp4 of exactly `duration` seconds."""
    out = _BUILD / f"seg{index:02d}.mp4"
    frames_dir = _FRAMES / seg["id"]
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.glob("*.png"):
        stale.unlink()

    caption = f"PROVE · {seg['id'].split('_', 1)[1].replace('_', ' ')}"
    total = max(int(duration * FPS), 1)

    if seg["kind"] == "board":
        # the same SVG the animation uses, assembled node by node. Rendering costs one browser
        # launch per stage, so it is cached across segments and stretched over each one.
        global _BOARD_STAGES
        if _BOARD_STAGES is None:
            _BOARD_STAGES = reveal_frames(_BUILD / "board", stages=20)
        stages = _BOARD_STAGES
        lo, hi = seg.get("window", (0.0, 1.0))
        window = stages[int(lo * len(stages)):max(int(hi * len(stages)), 1)] or stages
        for f in range(total):
            src = window[min(int(f / total * len(window)), len(window) - 1)]
            still = render_board(src, caption)
            still.save(frames_dir / f"{f:05d}.png")
    elif seg["kind"] == "terminal":
        record = json.loads((_HERE / "terminal" / "live_demo.json").read_text())
        entries = record["lines"]
        span = entries[-1]["t"] or 1.0
        # Terminal segments carry a window over the RECORDED session (fractions of its length),
        # so consecutive segments continue the same run instead of each replaying from zero —
        # without this the closing segment re-shows the opening and the zero-token rows, which
        # are the point of the demo, never appear.
        if seg.get("mode") == "final":
            # A run's payoff can land in its last fraction of a second — here the third candidate
            # is admitted and the skill-served rows appear at 92.9s of a 93s session. Replaying
            # that linearly would spend the whole segment on the miss phase and flash the result
            # for one frame, so the closing segment holds the completed session instead.
            still = render_terminal([e["line"] for e in entries], caption)
            still.save(frames_dir / "00000.png")
        else:
            lo, hi = seg.get("window", (0.0, 1.0))
            for f in range(total):
                cutoff = (lo + (hi - lo) * (f / total)) * span
                shown = [e["line"] for e in entries if e["t"] <= cutoff]
                render_terminal(shown, caption).save(frames_dir / f"{f:05d}.png")
    else:
        asset = Path(seg["asset"])
        if seg["kind"] == "code":
            asset = _newest_skill(asset)
            still = render_code(asset, caption)
        else:
            still = render_image(asset, caption)
        still.save(frames_dir / "00000.png")

    if seg["kind"] == "board" or (seg["kind"] == "terminal" and seg.get("mode") != "final"):
        cmd = ["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(frames_dir / "%05d.png")]
    else:
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{duration:.3f}",
               "-i", str(frames_dir / "00000.png")]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-t", f"{duration:.3f}", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def main() -> None:
    _BUILD.mkdir(parents=True, exist_ok=True)
    _AUDIO.mkdir(parents=True, exist_ok=True)

    # check every asset up front: a missing still used to surface only after the narration for
    # the preceding segments had already been synthesised and paid for.
    missing = [f"{s['id']} -> {s['asset']}" for s in SEGMENTS
               if s["kind"] == "image" and not Path(s["asset"]).exists()]
    if missing:
        raise SystemExit("missing assets:\n  " + "\n  ".join(missing))

    clips, sounds, total = [], [], 0.0
    for i, seg in enumerate(SEGMENTS):
        wav = _AUDIO / f"{seg['id']}.wav"
        spoken = synthesise(seg["text"], wav)
        duration = spoken + 0.9          # a beat after each line so it does not feel clipped
        total += duration
        print(f"  {seg['id']:18} {spoken:5.1f}s narration -> {duration:5.1f}s on screen")
        clips.append(build_segment(seg, i, duration))
        sounds.append((wav, duration))

    concat = _BUILD / "clips.txt"
    concat.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    silent = _BUILD / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-c", "copy", str(silent)], check=True, capture_output=True)

    # pad each narration clip to its segment length, then join, so audio stays aligned to video
    padded = []
    for i, (wav, duration) in enumerate(sounds):
        p = _AUDIO / f"pad{i:02d}.wav"
        subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-af",
                        f"apad=whole_dur={duration:.3f}", "-t", f"{duration:.3f}", str(p)],
                       check=True, capture_output=True)
        padded.append(p)
    aconcat = _BUILD / "audio.txt"
    aconcat.write_text("".join(f"file '{p}'\n" for p in padded), encoding="utf-8")
    voice = _BUILD / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(aconcat),
                    "-c", "copy", str(voice)], check=True, capture_output=True)

    out = _HERE / "prove_demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", str(silent), "-i", str(voice),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
                   check=True, capture_output=True)
    print(f"\n  {out}  ({total/60:.1f} min)")


if __name__ == "__main__":
    main()
