#!/usr/bin/env python3
"""Focused current-Stage-0 differential for poisoned return-type recovery."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.test_sem import CASES, KIND_TO_MNE, Probe, flist, prove_case

NAMES = ("binop-left-poison", "binop-right-poison")


def fail_facts(proof):
    return [
        (KIND_TO_MNE[obligation["kind"]], obligation["start"], obligation["end"])
        for obligation in flist(proof["obls"], 1)
        if obligation["status"] == 1
    ]


probe = Probe()
reports = []
try:
    for name in NAMES:
        _, source, _ = next(case for case in CASES if case[0] == name)
        parse = probe.send({"oracle": source})
        expected = [
            (item["code"], item["span"]["start"], item["span"]["end"])
            for item in probe.send({"elaborate": source})
            if item["code"].startswith("MNE")
        ]
        first = prove_case(probe, source)
        second = prove_case(probe, source)
        facts = fail_facts(first)
        assert first == second, f"native semantic proof differed across repetitions: {name}"
        assert facts == expected, (name, facts, expected)
        assert first["ok"] is False
        reports.append({
            "case": name,
            "source": source,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "rust_parse_diagnostics": parse["diagnostics"],
            "rust_elaboration_diagnostics": [
                {"code": code, "span": {"start": start, "end": end}}
                for code, start, end in expected
            ],
            "native_status": "reproduced_match",
            "native_identical_repetitions": 2,
            "native_fail_obligations": [
                {"code": code, "start": start, "end": end}
                for code, start, end in facts
            ],
            "native_ok": first["ok"],
        })
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "compiler_source_profile": "0.18",
        "source_profile": "0.10 (historical reproductions retained)",
        "native_execution_steps_total": sum(probe.steps),
        "cases": reports,
        "scope": "Two current compiler semantic recovery cases; ordered diagnostics and spans compared with current Rust Stage-0, with repeated native proof results.",
    }
    (ROOT / "evidence/semantic-pressure-cp0017-after.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
finally:
    probe.close()
