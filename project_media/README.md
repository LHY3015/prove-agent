# Submission media

| | |
| --- | --- |
| `prove_demo.mp4` | the 3-minute demo video, ready to upload |
| `Image gallery/` | stills for the submission gallery |
| `script.py` | the narration, segment by segment, with the visual each line belongs to |
| `live_demo.py` | the demo the video shows — one format, cold start to zero-token recall |
| `capture_terminal.py` | records `live_demo.py` with per-line timestamps |
| `build_video.py` | synthesises the narration and cuts the video together |

## Rebuilding

```bash
export DASHSCOPE_API_KEY=...
python project_media/capture_terminal.py --live   # record a real run (~90s)
python project_media/build_video.py               # narrate and assemble (~3 min)
```

Narration is generated with `qwen3-tts-flash` on the same platform the project runs on, and each
segment is cut to its spoken length, so editing a line in `script.py` re-times the video
automatically. Nothing is screen-recorded: the terminal segments are re-rendered from the captured
timestamps, which is why a rebuild reproduces exactly.

Re-capturing gives a genuinely different run — synthesis is non-deterministic, and the recorded
session happens to show two candidates rejected before the third is admitted. Segments 03 and 04
describe that sequence, so re-record and re-read those two lines together.
