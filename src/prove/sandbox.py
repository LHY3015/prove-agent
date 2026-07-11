"""Subprocess isolation for all synthesized skill code (Hard Design Rule 3).

Every candidate/admitted skill runs here, never in-process. A skill is a module exposing
`def extract(text_layout: dict) -> dict[str, str]`; it receives a pre-extracted text_layout
dict (never the PDF), so it needs no file/network I/O.

Isolation model (threat = buggy or accidentally-malicious LLM parser code, NOT a determined
adversary):
  - a fresh `python -I` child (isolated mode ignores PYTHONPATH / user site — also keeps this
    machine's ROS PYTHONPATH leak out of the sandbox) with a scrubbed env;
  - CPU + address-space rlimits set in the child's first lines (covers infinite loops and
    catastrophic `re` backtracking, and memory blow-ups);
  - a wall-clock timeout on the parent that kills the whole process group as a backstop;
  - the skill runs with a restricted `__builtins__`: `__import__` allows only
    {re, json, datetime, decimal, math} (+ submodules) and open/exec/eval/compile/input are
    removed. Network is blocked *transitively* — there is no import path to socket/urllib/http.

Note: a restricted `__builtins__` is not a hard security boundary (Python MRO escapes exist);
it is defence-in-depth appropriate to the threat model. Kernel-level isolation
(seccomp/unshare) is added at the ECS-deploy phase where the target kernel is known.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

ALLOWED_IMPORTS = {"re", "json", "datetime", "decimal", "math"}


@dataclass
class SandboxResult:
    ok: bool
    value: Optional[dict] = None       # the skill's returned dict (str->str), when ok
    error: Optional[str] = None        # exception text / reason, when not ok
    timed_out: bool = False
    stdout: str = ""                   # anything the skill printed (feedback for self-repair)


# The child program. Reads {code, text_layout, cpu_seconds, mem_mb} as JSON on stdin, runs the
# skill under a restricted builtins, and writes one protocol JSON line to the REAL stdout.
# Trusted imports happen before any restriction; only the skill's namespace is sandboxed.
_RUNNER = r"""
import sys, json, io, resource, builtins

_req = json.loads(sys.stdin.read())
_cpu = int(_req.get("cpu_seconds", 5))
_mem = int(_req.get("mem_mb", 512)) * 1024 * 1024
# Cap CPU time and address space of THIS child. Set after interpreter startup so the caps
# apply to skill execution, not to bringing up the interpreter.
try:
    resource.setrlimit(resource.RLIMIT_CPU, (_cpu, _cpu + 1))
    resource.setrlimit(resource.RLIMIT_AS, (_mem, _mem))
except (ValueError, OSError):
    pass

_ALLOWED = set(%(allowed)r)
_real_import = builtins.__import__

def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED:
        raise ImportError("import of %%r is blocked by the sandbox" %% name)
    return _real_import(name, globals, locals, fromlist, level)

# Restricted builtins for the skill: drop the I/O / dynamic-exec surface, gate imports.
_safe = dict(vars(builtins))
for _bad in ("open", "exec", "eval", "compile", "input", "breakpoint", "help", "__import__"):
    _safe.pop(_bad, None)
_safe["__import__"] = _guarded_import

_real_stdout = sys.stdout
sys.stdout = io.StringIO()          # capture skill prints; keep the protocol channel clean

def _emit(obj):
    sys.stdout = _real_stdout
    _real_stdout.write(json.dumps(obj))
    _real_stdout.flush()

try:
    _ns = {"__builtins__": _safe, "__name__": "__skill__"}
    exec(_req["code"], _ns)
    _fn = _ns.get("extract")
    if not callable(_fn):
        _emit({"ok": False, "error": "skill defines no callable extract()", "stdout": ""})
        sys.exit(0)
    _out = _fn(_req["text_layout"])
    _captured = sys.stdout.getvalue()
    if not isinstance(_out, dict):
        _emit({"ok": False, "error": "extract() returned %%s, expected dict" %% type(_out).__name__,
               "stdout": _captured})
        sys.exit(0)
    _clean = {str(k): ("" if v is None else str(v)) for k, v in _out.items()}
    _emit({"ok": True, "value": _clean, "stdout": _captured})
except Exception as _e:
    _captured = sys.stdout.getvalue() if isinstance(sys.stdout, io.StringIO) else ""
    _emit({"ok": False, "error": "%%s: %%s" %% (type(_e).__name__, _e), "stdout": _captured})
""" % {"allowed": sorted(ALLOWED_IMPORTS)}


def run_skill(
    code: str,
    text_layout: dict,
    *,
    cpu_seconds: int = 5,
    mem_mb: int = 512,
    wall_timeout: Optional[float] = None,
) -> SandboxResult:
    """Execute `code` (which must define extract()) against `text_layout` in an isolated
    child. Returns a SandboxResult; never raises for skill-side failures."""
    wall = wall_timeout if wall_timeout is not None else cpu_seconds + 3.0
    payload = json.dumps(
        {"code": code, "text_layout": text_layout, "cpu_seconds": cpu_seconds, "mem_mb": mem_mb}
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _RUNNER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group, so a timeout can kill any children
        )
    except OSError as e:
        return SandboxResult(ok=False, error=f"failed to launch sandbox: {e}")

    try:
        out, err = proc.communicate(payload, timeout=wall)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        return SandboxResult(ok=False, error=f"wall-clock timeout after {wall}s", timed_out=True)

    if not out.strip():
        # No protocol line: the child died before emitting (rlimit CPU/mem kill, hard crash).
        reason = (err or "").strip().splitlines()[-1:] or ["no output"]
        timed_out = proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
        return SandboxResult(
            ok=False, error=f"sandbox produced no result ({reason[0]})", timed_out=timed_out
        )

    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return SandboxResult(ok=False, error=f"unparseable sandbox output: {out[:200]!r}")

    return SandboxResult(
        ok=bool(result.get("ok")),
        value=result.get("value"),
        error=result.get("error"),
        stdout=result.get("stdout", ""),
    )


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
