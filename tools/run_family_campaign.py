#!/usr/bin/env python3
"""Canonical MNCS family compilation campaign runner (thin transport).

Feeds real MNCS sources through the native project compiler
(project.compile_project) plus the locked Stage-0 project oracle and
classifies the stage-specific outcome per slice.

This runner owns no compiler semantics: discovery, probe transport,
oracle transport, and report formatting only. Parsing, proof, CFG, and
SSA all execute in MNCS or Stage-0. Adapted slices apply explicitly
recorded byte transforms and are always reported as adapted, never as
unchanged project compiles.

Usage:
    python3 tools/run_family_campaign.py [slice-name ...]
    python3 tools/run_family_campaign.py --list

Writes evidence/campaign-<id>-family-results.json (CAMPAIGN_ID env or
UTC date) and prints a one-line summary per slice.
"""

import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from test_project import (  # noqa: E402
    Probe,
    decode,
    discover_sources,
    flist,
    identity_map,
    request_value,
)

WORKSPACE = ROOT.parent
DIAGNOSTICS = {
    0: "SourceOrder",
    1: "SourceCountMismatch",
    2: "DuplicateModule",
    3: "MissingImport",
    4: "ParseFailed",
    5: "ProofFailed",
    6: "FlowFailed",
}
CAMPAIGN_ID = os.environ.get(
    "MNCS_CAMPAIGN_ID",
    datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d"),
)

# Slice manifest. classification is one of:
#   unmodified-real-source | minimally-adapted-real-source |
#   minimized-reproducer | synthetic-fixture
# `root` selects the oracle root file (last file by default).
# `adapt` maps a repo-relative path to ordered byte transforms applied
# in memory; the report always carries the exact transforms.
SLICES = [
    {
        "name": "doctor-healthy-lib",
        "classification": "unmodified-real-source",
        "repo": "mncs-doctor",
        "files": ["fixtures/repos/healthy/src/lib.mncs"],
    },
    {
        "name": "doctor-healthy-pair-bare-use",
        "classification": "unmodified-real-source",
        "repo": "mncs-doctor",
        "files": [
            "fixtures/repos/healthy/src/lib.mncs",
            "fixtures/repos/healthy/src/main.mncs",
        ],
        "root": "fixtures/repos/healthy/src/main.mncs",
    },
    {
        "name": "doctor-018-alias-pair",
        "classification": "minimally-adapted-real-source",
        "repo": "mncs-doctor",
        "files": [
            "fixtures/repos/healthy/src/lib.mncs",
            "fixtures/repos/healthy/src/main.mncs",
        ],
        "root": "fixtures/repos/healthy/src/main.mncs",
        "adapt": {
            "fixtures/repos/healthy/src/lib.mncs": [
                {"find": "mncs 0.17;", "replace": "mncs 0.18;"},
            ],
            "fixtures/repos/healthy/src/main.mncs": [
                {"find": "mncs 0.17;", "replace": "mncs 0.18;"},
                {"find": "use healthy.lib;", "replace": "use healthy.lib as lib;"},
            ],
        },
    },
    {
        "name": "lang-structured-artifact",
        "classification": "unmodified-real-source",
        "repo": "mncs-language",
        "files": ["examples/source/structured-artifact.mncs"],
    },
    {
        "name": "lang-fs-metadata",
        "classification": "unmodified-real-source",
        "repo": "mncs-language",
        "files": ["examples/source/fs-metadata.mncs"],
    },
    {
        "name": "lang-cre1-combine",
        "classification": "unmodified-real-source",
        "repo": "mncs-language",
        "files": ["examples/source/cre1-evidence-combine.mncs"],
    },
    {
        "name": "lang-cre1-non-exhaustive",
        "classification": "unmodified-real-source",
        "repo": "mncs-language",
        "files": ["examples/source/cre1-non-exhaustive.mncs"],
    },
    {
        "name": "cli-outcome",
        "classification": "unmodified-real-source",
        "repo": "mncs-cli",
        "files": ["src/cli/outcome.mncs"],
    },
    {
        "name": "next-proj-boundary",
        "classification": "synthetic-fixture",
        "repo": None,
        "inline": "mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { return v.x; }",
    },
]


def git_head(path):
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out, bool(dirty)


def file_dirty(repo_path, rel):
    out = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain", "--", rel],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return bool(out)


def apply_adapt(text, transforms):
    applied = []
    for op in transforms:
        assert text.count(op["find"]) == 1, (op["find"], text.count(op["find"]))
        text = text.replace(op["find"], op["replace"])
        applied.append(op)
    return text, applied


def module_of(text):
    match = re.search(r"module\s+([A-Za-z0-9_.]+)\s*;", text)
    return match.group(1) if match else None


def classify(native):
    diags = flist(native.get("diagnostics", {"$v": 0}))
    named = [DIAGNOSTICS.get(d.get("$v", -1), f"unknown-{d.get('$v')}") for d in diags]
    if not native.get("valid", False):
        for diag, name in zip(diags, named):
            if name in ("ParseFailed", "ProofFailed", "FlowFailed"):
                return f"native-{name}", diag.get("$p", {})
        return "native-invalid-other", {"diagnostics": named}
    flags = []
    for module in flist(native.get("modules", {"$v": 0})):
        ssa = module.get("value_ssa", {})
        if isinstance(ssa, dict):
            flags.append({
                "source_index": module.get("source_index"),
                "valid": ssa.get("valid"),
                "verified": ssa.get("verified_function_count"),
                "first_unsupported_kind": ssa.get("first_unsupported_kind"),
                "first_unsupported_block": ssa.get("first_unsupported_block"),
            })
    if native.get("value_ssa_valid") is not True:
        return "native-ssa-unsupported", {"modules": flags}
    return "native-full-slice-ok", {"modules": flags}


def run_slice(probe, identities, definition):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-family-") as directory:
        root = Path(directory)
        file_report = {}
        texts = []
        if definition.get("inline") is not None:
            (root / "00-inline.mncs").write_text(definition["inline"])
            file_report["<inline>"] = {
                "bytes": len(definition["inline"].encode()),
                "modified": False,
            }
        else:
            repo_path = WORKSPACE / definition["repo"]
            for i, rel in enumerate(definition["files"]):
                raw = (repo_path / rel).read_bytes()
                text = raw.decode()
                transforms = (definition.get("adapt") or {}).get(rel, [])
                if transforms:
                    text, _ = apply_adapt(text, transforms)
                (root / f"{i:02d}-{Path(rel).name}").write_text(text)
                file_report[rel] = {
                    "bytes": len(raw),
                    "modified": bool(transforms),
                    "repo_dirty": file_dirty(repo_path, rel),
                }
        sources = discover_sources(root)
        decoded_texts = [text.decode() if isinstance(text, bytes) else text
                         for _, _, text in sources]
        raw = probe.send(request_value(identities, sources))
        elapsed = round(time.monotonic() - started, 3)
        if raw.get("status") != "returned":
            return {
                "file_report": file_report,
                "elapsed_seconds": elapsed,
                "native_valid": None,
                "native_value_ssa_valid": None,
                "stage": "transport-capacity",
                "detail": {"status": raw.get("status"), "failure": raw.get("failure")},
                "stage0_oracle_valid": None,
                "stage0_oracle_diagnostics": None,
            }, elapsed
        native = decode(raw["returned"][0])
        probe.steps.append(raw["steps"])
        names = [module_of(t) for t in decoded_texts]
        if len(decoded_texts) == 1 or any(n is None for n in names):
            oracle = probe.send({"project_oracle": {
                "root": decoded_texts[0], "modules": {},
            }})
        else:
            mods = {n: t for n, t in zip(names[:-1], decoded_texts[:-1])}
            oracle = probe.send({"project_oracle": {
                "root": decoded_texts[-1], "modules": mods,
            }})
    stage, detail = classify(native)
    return {
        "file_report": file_report,
        "elapsed_seconds": elapsed,
        "native_valid": native.get("valid"),
        "native_value_ssa_valid": native.get("value_ssa_valid"),
        "native_diagnostics": [
            {"kind": DIAGNOSTICS.get(d.get("$v", -1)), "payload": d.get("$p")}
            for d in flist(native.get("diagnostics", {"$v": 0}))
        ],
        "stage": stage,
        "detail": detail,
        "stage0_oracle_valid": oracle.get("valid"),
        "stage0_oracle_diagnostics": oracle.get("diagnostics"),
    }, elapsed


def main(argv):
    if "--list" in argv:
        for definition in SLICES:
            print(f"{definition['name']}  {definition['classification']}")
        return 0
    wanted = [a for a in argv[1:] if not a.startswith("-")] or None
    selected = [d for d in SLICES if wanted is None or d["name"] in wanted]
    assert selected, f"no slices match {wanted}"
    lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    compiler_rev, compiler_dirty = git_head(ROOT)
    source_revs = {}
    for definition in selected:
        repo = definition.get("repo")
        if repo and repo not in source_revs:
            head, dirty = git_head(WORKSPACE / repo)
            source_revs[repo] = {"revision": head, "dirty": dirty}
    probe = Probe()
    try:
        identities = identity_map(probe)
        results = []
        for definition in selected:
            report, _ = run_slice(probe, identities, definition)
            report["slice"] = definition["name"]
            report["classification"] = definition["classification"]
            report["repo"] = definition.get("repo")
            if definition.get("adapt"):
                report["adapt"] = definition["adapt"]
            results.append(report)
            print(f"{definition['name']}: {report['stage']} "
                  f"(native={report['native_valid']}, "
                  f"oracle={report['stage0_oracle_valid']})", flush=True)
        total_steps = sum(probe.steps)
        requests = probe.requests
    finally:
        probe.close()
    campaign = {
        "schema_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "compiler_revision": compiler_rev,
        "compiler_dirty": compiler_dirty,
        "stage0_revision": lock["revision"],
        "source_profile": lock["source_profile"],
        "source_revisions": source_revs,
        "probe_requests": requests,
        "probe_steps_total": total_steps,
        "slices": results,
    }
    out = ROOT / "evidence" / f"campaign-{CAMPAIGN_ID}-family-results.json"
    out.write_text(json.dumps(campaign, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
