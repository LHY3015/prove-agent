"""Router fingerprint tests: same format -> identical fingerprint; different format ->
different; a drifted header -> mismatch; exact-match routing + miss on unknown formats."""

import copy

from prove.layout import extract_layout
from prove.router import Router, _jaccard, compute_fingerprint, fingerprint_hash


def _by_format(manifest):
    out = {}
    for e in manifest:
        out.setdefault(e["format_id_true"], []).append(e)
    return out


def _fingerprints(entries):
    return [compute_fingerprint(extract_layout(e["pdf_path"])) for e in entries]


def test_same_format_same_fingerprint(mini_dataset):
    for fmt, entries in _by_format(mini_dataset).items():
        hashes = {fingerprint_hash(fp) for fp in _fingerprints(entries)}
        assert len(hashes) == 1, f"{fmt} produced unstable fingerprints: {hashes}"


def test_different_formats_differ(mini_dataset):
    by_fmt = _by_format(mini_dataset)
    canon = {fmt: _fingerprints(es)[0] for fmt, es in by_fmt.items()}
    fmts = list(canon)
    for i in range(len(fmts)):
        for j in range(i + 1, len(fmts)):
            assert _jaccard(canon[fmts[i]], canon[fmts[j]]) < 0.999


def test_exact_match_routing(mini_dataset):
    by_fmt = _by_format(mini_dataset)
    router = Router()
    for fmt, es in by_fmt.items():
        router.register(fmt, extract_layout(es[0]["pdf_path"]))
    # route the *other* docs of each format
    for fmt, es in by_fmt.items():
        for e in es[1:]:
            fid, conf, method = router.route(extract_layout(e["pdf_path"]))
            assert fid == fmt
            assert method == "exact"
            assert conf == 1.0


def test_unknown_format_misses(mini_dataset):
    by_fmt = _by_format(mini_dataset)
    fmts = list(by_fmt)
    router = Router()
    # register all but the last format
    for fmt in fmts[:-1]:
        router.register(fmt, extract_layout(by_fmt[fmt][0]["pdf_path"]))
    held_out = by_fmt[fmts[-1]][0]
    fid, conf, method = router.route(extract_layout(held_out["pdf_path"]))
    assert fid is None
    assert method == "miss"


def test_empty_router_misses(mini_dataset):
    e = mini_dataset[0]
    fid, conf, method = Router().route(extract_layout(e["pdf_path"]))
    assert fid is None and method == "miss" and conf == 0.0


def test_header_drift_changes_fingerprint(mini_dataset):
    """Mutating a header label (a structural drift) changes the fingerprint -> mismatch."""
    e = mini_dataset[0]
    layout = extract_layout(e["pdf_path"])
    base_fp = compute_fingerprint(layout)

    drifted = copy.deepcopy(layout)
    # rename the first alphabetic header word (e.g. the vendor token)
    for w in drifted["words"]:
        if w["text"].isalpha():
            w["text"] = "ZZZDRIFT"
            break
    drifted["lines"] = layout["lines"]  # cutoff logic reads lines; header text still present
    drifted_fp = compute_fingerprint(drifted)
    assert base_fp != drifted_fp
    assert _jaccard(base_fp, drifted_fp) < 0.999
