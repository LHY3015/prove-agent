"""Sandbox isolation tests (plan §6): the security guarantees synthesized skills rely on —
import whitelist, no network path, no file I/O, CPU timeout — plus the result protocol."""

from prove.sandbox import run_skill

_TL = {"full_text": "Acme Corporation", "lines": [{"text": "Acme Corporation"}]}


def test_valid_skill_runs_and_returns_dict():
    code = "def extract(tl):\n    return {'vendor_name': tl['lines'][0]['text']}"
    r = run_skill(code, _TL)
    assert r.ok and r.value == {"vendor_name": "Acme Corporation"}


def test_whitelisted_import_allowed():
    code = "import re\ndef extract(tl):\n    return {'n': re.sub('a','b','aa')}"
    r = run_skill(code, _TL)
    assert r.ok and r.value == {"n": "bb"}


def test_import_os_blocked():
    r = run_skill("import os\ndef extract(tl):\n    return {}", _TL)
    assert not r.ok and "blocked" in r.error


def test_network_import_blocked():
    r = run_skill("import socket\ndef extract(tl):\n    return {}", _TL)
    assert not r.ok and "blocked" in r.error


def test_open_removed():
    code = "def extract(tl):\n    open('/etc/passwd').read()\n    return {}"
    r = run_skill(code, _TL)
    assert not r.ok and "open" in r.error


def test_cpu_timeout_enforced():
    code = "def extract(tl):\n    x = 0\n    while True:\n        x += 1\n    return {}"
    r = run_skill(code, _TL, cpu_seconds=1, wall_timeout=8)
    assert not r.ok and r.timed_out


def test_non_dict_return_rejected():
    r = run_skill("def extract(tl):\n    return 42", _TL)
    assert not r.ok and "dict" in r.error


def test_exception_is_captured_not_raised():
    r = run_skill("def extract(tl):\n    return tl['missing']", _TL)
    assert not r.ok and "KeyError" in r.error


def test_skill_prints_do_not_corrupt_result():
    code = "def extract(tl):\n    print('noisy debug')\n    return {'ok': '1'}"
    r = run_skill(code, _TL)
    assert r.ok and r.value == {"ok": "1"}
    assert "noisy debug" in r.stdout  # captured separately for self-repair feedback


def test_values_coerced_to_strings():
    r = run_skill("def extract(tl):\n    return {'n': 3, 'x': None}", _TL)
    assert r.ok and r.value == {"n": "3", "x": ""}
