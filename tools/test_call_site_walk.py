#!/usr/bin/env python3
"""Stage-0 and native differential for complete declaration call-site walks.

The imported call follows 600 ordinary statements.  The old 1,024-turn
collector spent two turns per statement and silently omitted that call from
the project importer's signature inventory.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN",
    ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe",
))
MODULE = "mncs.compiler.decl.v1"
PAGE_BOUND = 1024
STRIDE = 1024
STEP_BUDGET = 8_000_000
NAT_ARGS = [
    {"kind": "nat", "value": PAGE_BOUND},
    {"kind": "nat", "value": STRIDE},
]
SEEDS = [
    {"module": MODULE, "function": name, "type_arguments": NAT_ARGS}
    for name in ("parse_unit", "collect_call_sites")
]


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def byte_value(value):
    return {"byte": {"value": value}}


def pages_value(data):
    chunks = [data[i:i + STRIDE] for i in range(0, len(data), STRIDE)]
    return {"sequence": {"values": [
        {"sequence": {"values": [byte_value(value) for value in chunk]}}
        for chunk in chunks
    ]}}


def field(value, name):
    return next(item for key, item in value["record"]["fields"] if key == name)


def record_integer(value, name):
    return field(value, name)["integer"]["value"]


def list_items(value, nil=0, cons=1):
    items = []
    while value["finite"]["discriminant"] == cons:
        payload = {key: item for key, item in value["finite"].get("payload", [])}
        items.append(payload["head"])
        value = payload["tail"]
    assert value["finite"]["discriminant"] == nil, value
    return items


def run_request(proc, function, arguments):
    request = {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": function},
        "arguments": arguments,
        "type_arguments": NAT_ARGS,
        "step_budget": STEP_BUDGET,
    }
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, f"probe exited early: {proc.poll()}"
    result = json.loads(line)
    assert result["status"] == "returned", (function, result)
    return result


def main():
    if not PROBE.is_file():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    dependency = (
        "mncs 0.18; module demo.capacity; "
        "fn answer(value: u64) -> (result: u64) { return value; }"
    )
    imported_name = "capacity.answer"
    caller = (
        "mncs 0.18; module demo.capacity_user; use demo.capacity as capacity; "
        "fn get() -> (result: u64) { "
        + " ".join(f"let filler_{i:04}: u64 = {i};" for i in range(600))
        + f" let imported_value: u64 = {imported_name}(7); return imported_value; }}"
    )
    source = caller.encode()
    source_pages = pages_value(source)
    total = len(source)

    env = dict(os.environ)
    if env.get("MNCS_PROBE_BACKEND") == "reference_interpreter":
        env.pop("MNCS_PROBE_BACKEND", None)
    else:
        env.setdefault("MNCS_PROBE_BACKEND", "cranelift")
    env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
    env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(SEEDS)
    env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
    proc = subprocess.Popen(
        [str(PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=None, text=True, env=env, cwd=ROOT)
    try:
        proc.stdin.write(json.dumps({"execution_status": True}) + "\n")
        proc.stdin.flush()
        status = json.loads(proc.stdout.readline())
        if status.get("backend") == "cranelift":
            assert status["retained_sessions"] == 1, status

        parsed = run_request(proc, "parse_unit", [
            source_pages, integer(STRIDE), integer(total),
        ])
        unit = parsed["returned"][0]
        call_sites = run_request(proc, "collect_call_sites", [
            source_pages, integer(STRIDE), integer(total), field(unit, "fns"),
        ])
        sites = list_items(call_sites["returned"][0])
        target_span = [caller.rindex(imported_name), caller.rindex(imported_name) + len(imported_name)]
        native_call_spans = [
            [record_integer(item, "start"), record_integer(item, "end")]
            for item in sites
        ]
        native_call_texts = [caller[a:b] for a, b in native_call_spans]
        found = target_span in native_call_spans

        proc.stdin.write(json.dumps({"project_oracle": {
            "root": caller,
            "modules": {"demo.capacity": dependency},
        }}) + "\n")
        proc.stdin.flush()
        oracle_line = proc.stdout.readline()
        assert oracle_line, f"probe exited early: {proc.poll()}"
        oracle = json.loads(oracle_line)
        assert oracle["valid"] is True, oracle["diagnostics"]

        report = {
            "scope": "declaration call-site traversal after 600 initializer statements",
            "stage0_valid": oracle["valid"],
            "stage0_linked_functions": len(oracle["program"]["functions"]),
            "native_call_sites": len(sites),
            "native_call_spans": native_call_spans,
            "native_call_texts": native_call_texts,
            "imported_call_span": target_span,
            "native_collector_found_imported_call": found,
            "native_steps": [parsed["steps"], call_sites["steps"]],
            "backend": status.get("backend"),
        }
        Path(".build/campaign-20261007-call-site-walk.json").write_text(
            json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        assert found, report
    finally:
        proc.stdin.close()
        status_code = proc.wait(timeout=60)
        assert status_code == 0, f"probe exited with {status_code}"


if __name__ == "__main__":
    main()
