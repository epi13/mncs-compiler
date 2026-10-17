#!/usr/bin/env python3
"""Verify imported nominal schemas and generic calls through current Flow/SSA.

Stage-0 supplies an independent two-module oracle. The current Compiler's
selected Flow-to-SSA entry consumes that root source and the canonical imported
identity/schema inputs, then a fresh canonical VM executes the sealed artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import select
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_project as project
from test_vm_segment import vm_to_wire, test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN", ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
))
VM_MODULE = "demo.imported_ssa_harness"
TARGET_MODULE = "mncs.compiler.ssa.v1"
TARGET_FUNCTION = "lower_flow_unit_value_ssa_with_imported_schemas"
TYPE_ARGS = [4, 64]
STRIDE = TYPE_ARGS[1]
STEP_BUDGET = 8_000_000
MODULE_NAMES = ["source", "lexer", "segment", "parser", "decl", "flow", "ssa"]
DEPENDENCY = "mncs 0.18; module demo.dep; record Box { value: u64 }"
ROOT_SOURCE = (
    "mncs 0.18; module demo.root; use demo.dep as dep; "
    "fn read<N: Nat>(x: dep.Box) -> (r: u64) { return x.value; } "
    "fn get(x: dep.Box) -> (r: u64) { return read<4>(x); }"
)
APP_SOURCE = (
    "mncs 0.18; module demo.imported_ssa_harness; "
    "use mncs.compiler.ssa.v1 as ssa; use mncs.compiler.decl.v1 as decl; "
    "record FrontierSummary { valid: bool, function_count: u64, verified_function_count: u64, "
    "supported_function_count: u64, unsupported_block_count: u64, first_unsupported_function: u64, "
    "first_unsupported_kind: u64, first_function: ssa.ValueSsaFunctionStatus, second_function: ssa.ValueSsaFunctionStatus, "
    "call_count: u64, has_call: bool, first_call_identity: decl.SemanticIdentity } "
    "fn run(pages: [[byte; up_to 64]; up_to 4], total: u64, "
    "nominals: decl.NominalTypeIdentityList, records: decl.ImportedRecordList, "
    "enums: decl.ImportedEnumList) -> (result: FrontierSummary) { "
    "let summary: ssa.ValueSsaSummary = "
    "ssa.lower_flow_unit_value_ssa_with_imported_schemas<4, 64>(pages, 64, total, "
    "decl.SigTab.HNil, nominals, records, enums); "
    "let first: ssa.ValueSsaFunctionStatus = ssa.value_ssa_function_status(summary, 0); "
    "let second: ssa.ValueSsaFunctionStatus = ssa.value_ssa_function_status(summary, 1); "
    "return FrontierSummary { valid: summary.valid, function_count: summary.function_count, "
    "verified_function_count: summary.verified_function_count, supported_function_count: summary.supported_function_count, "
    "unsupported_block_count: summary.unsupported_block_count, first_unsupported_function: summary.first_unsupported_function, "
    "first_unsupported_kind: summary.first_unsupported_kind, first_function: first, second_function: second, "
    "call_count: summary.call_count, has_call: summary.has_call, first_call_identity: summary.first_call_identity }; }"
)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def memory_snapshot() -> dict | None:
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if set(values) != {"MemTotal", "MemAvailable"}:
        return None
    return {"total_bytes": values["MemTotal"], "available_bytes": values["MemAvailable"]}


def cgroup_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def sample_process(pid: int) -> dict:
    sample = {"rss_bytes": None, "peak_rss_bytes": None, "cpu_seconds": None}
    root = Path("/proc") / str(pid)
    try:
        status = {}
        for line in (root / "status").read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.strip()
        sample["rss_bytes"] = int(status["VmRSS"].split()[0]) * 1024
        sample["peak_rss_bytes"] = int(status["VmHWM"].split()[0]) * 1024
        fields = (root / "stat").read_text()
        fields = fields[fields.rfind(")") + 2 :].split()
        sample["cpu_seconds"] = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return sample


class Probe:
    """One fresh protocol process; sample it while application-build runs."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = ""
        env["MNCS_PROBE_EXECUTION_MODULES"] = ""
        env["MNCS_PROBE_GENERIC_SEEDS"] = "[]"
        env["MNCS_PROBE_CACHE_DIR"] = str(ROOT / ".build/probe-cache")
        env["MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT"] = str(ROOT)
        self.stderr = tempfile.TemporaryFile(mode="w+t")
        self.proc = subprocess.Popen(
            [str(PROBE)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1,
        )

    def send(self, request: dict, timeout: float = 600.0) -> tuple[dict, dict]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        pid = self.proc.pid
        before = sample_process(pid)
        samples = [before]
        started = time.monotonic()
        self.proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = started + timeout
        line = None
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if ready:
                line = self.proc.stdout.readline()
                break
            samples.append(sample_process(pid))
            if self.proc.poll() is not None:
                break
        wall = time.monotonic() - started
        if not line:
            self.stderr.seek(0)
            detail = self.stderr.read()[-4000:]
            raise RuntimeError(f"Stage-0 probe timed out or stopped: {detail}")
        result = json.loads(line)
        after = sample_process(pid)
        samples.append(after)
        peak_rss = max((row["peak_rss_bytes"] or row["rss_bytes"] or 0 for row in samples), default=0)
        cpu = None
        if before["cpu_seconds"] is not None and after["cpu_seconds"] is not None:
            cpu = max(0.0, after["cpu_seconds"] - before["cpu_seconds"])
        return result, {"wall_seconds": round(wall, 6), "cpu_seconds": round(cpu, 6) if cpu is not None else None,
                        "peak_rss_bytes": peak_rss or None}

    def close(self) -> str:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=60)
        self.stderr.seek(0)
        stderr = self.stderr.read()
        self.stderr.close()
        if code != 0:
            raise RuntimeError(f"Stage-0 probe exited {code}: {stderr[-2000:]}")
        return stderr


def application_request() -> dict:
    modules = {
        f"mncs.compiler.{name}.v1": (ROOT / "src/compiler" / f"{name}.mncs").read_text()
        for name in MODULE_NAMES
    }
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "demo/imported_ssa_harness.mncs",
        "root_source": APP_SOURCE,
        "entry": {"module": VM_MODULE, "function": "run"},
        "modules": modules,
        "generic_seeds": [{
            "module": TARGET_MODULE,
            "function": TARGET_FUNCTION,
            "type_arguments": [{"kind": "nat", "value": value} for value in TYPE_ARGS],
        }],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def identity_text(identity: dict) -> str:
    assert identity["valid"] is True, identity
    raw = bytes(identity["bytes"])
    return raw[:identity["len"]].decode()


def identity_value(identity: str, record_identities: dict[str, dict]) -> dict:
    raw = identity.encode()
    if len(raw) > 512:
        raise ValueError(f"identity exceeds SemanticIdentity capacity: {identity}")
    return project.record(record_identities, "SemanticIdentity", {
        "bytes": project.byte_sequence(raw + bytes(512 - len(raw))),
        "len": project.integer(len(raw)),
        "valid": {"boolean": {"value": True}},
    })


def app_value_builders(interface: dict):
    types = interface["types"]
    records = {row["name"]: row for row in types.get("records", [])}
    finite = {}
    for row in types.get("finite", []):
        finite[row["name"]] = row

    def record_value(name: str, fields: dict) -> dict:
        return {"record": {
            "type_identity": records[name]["identity"],
            "name": name,
            "fields": [[key, value] for key, value in sorted(fields.items())],
        }}

    def finite_value(name: str, variant: str, payload: dict | None = None) -> dict:
        row = finite[name]
        chosen = next(item for item in row["variants"] if item["name"] == variant)
        return {"finite": {
            "discriminant": chosen["discriminant"],
            "payload": [[key, value] for key, value in sorted((payload or {}).items())],
            "type_identity": row["identity"],
            "variant_identity": chosen["identity"],
        }}

    return records, finite, record_value, finite_value


def build_inputs(interface: dict, nominal_identity: str, field_is_u64: bool = True,
                 schema_identity: str | None = None) -> dict:
    record_identities, _, record, finite = app_value_builders(interface)
    raw = ROOT_SOURCE.encode()
    pages = [raw[index:index + STRIDE] for index in range(0, len(raw), STRIDE)]
    starts = []
    cursor = 0
    while True:
        cursor = ROOT_SOURCE.find("dep.Box", cursor)
        if cursor < 0:
            break
        starts.append(cursor)
        cursor += len("dep.Box")
    if len(starts) != 2:
        raise AssertionError(f"expected two imported Box type spans, got {starts}")

    nominal_list = finite("NominalTypeIdentityList", "NINil")
    for start in reversed(starts):
        marker = record("NominalTypeIdentity", {
            "start": project.integer(start),
            "end": project.integer(start + len("dep.Box")),
            "identity": identity_value(nominal_identity, record_identities),
            "reference": {"boolean": {"value": True}},
        })
        nominal_list = finite("NominalTypeIdentityList", "NICons", {"head": marker, "tail": nominal_list})

    sem_type = finite("SemType", "TInt", {
        "width": project.integer(64),
        "signed": {"boolean": {"value": False}},
    }) if field_is_u64 else finite("SemType", "TBool")
    imported_field = record("ImportedRecordField", {
        "name": identity_value("value", record_identities),
        "index": project.integer(0),
        "ty": sem_type,
        "type_valid": {"boolean": {"value": True}},
    })
    imported_fields = finite("ImportedRecordFieldList", "IRFCons", {
        "head": imported_field,
        "tail": finite("ImportedRecordFieldList", "IRFNil"),
    })
    schema = record("ImportedRecordSchema", {
        "identity": identity_value(schema_identity or nominal_identity, record_identities),
        "fields": imported_fields,
    })
    imported_records = finite("ImportedRecordList", "IRCons", {
        "head": schema,
        "tail": finite("ImportedRecordList", "IRNil"),
    })
    imported_enums = finite("ImportedEnumList", "IENil")
    return {
        "arguments": [project.pages_value(pages), project.integer(len(raw)), nominal_list, imported_records, imported_enums],
        "nominal_spans": starts,
        "source_bytes": len(raw),
    }


def run_vm_batch(artifact_path: Path, cases: list[dict], directory: Path) -> tuple[dict, float, int | None, int, int]:
    calls = [{"id": case["id"], "request": {
        "schema_version": "0.1",
        "target": {"module": VM_MODULE, "function": "run"},
        "arguments": case["args"],
        "type_arguments": [],
        "step_budget": STEP_BUDGET,
    }} for case in cases]
    calls_path = directory / "calls.json"
    output_path = directory / "results.json"
    calls_path.write_text(json.dumps(calls))
    command = [
        "/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
        "--artifact", str(artifact_path), "--calls", str(calls_path), "--output", str(output_path),
    ]
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True)
    wall = time.monotonic() - started
    if completed.returncode != 0:
        raise AssertionError(
            f"canonical VM batch exited {completed.returncode}; stdout={completed.stdout[-3000:]}; "
            f"stderr={completed.stderr[-3000:]}"
        )
    peak_kb = None
    for line in completed.stderr.splitlines():
        if "Maximum resident set size" in line:
            peak_kb = int(line.split(":", 1)[1].strip().split()[0])
    document = json.loads(output_path.read_text())
    if len(document.get("results", [])) != len(cases):
        raise AssertionError(f"canonical VM returned the wrong result count: {document}")
    return document, wall, peak_kb, calls_path.stat().st_size, output_path.stat().st_size


def main() -> None:
    if not PROBE.is_file():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    test_vm_emit.ensure_vm()
    host_before = memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    probe = Probe()
    oracle, _ = probe.send({"project_oracle": {
        "root": ROOT_SOURCE,
        "modules": {"demo.dep": DEPENDENCY},
    }})
    if oracle.get("valid") is not True or not isinstance(oracle.get("ssa"), dict):
        raise AssertionError(f"Stage-0 rejected the imported nominal/generic fixture: {oracle.get('diagnostics')}")
    nominal_rows = [row for row in oracle.get("program", {}).get("record_types", []) if row.get("name") == "Box"]
    if len(nominal_rows) != 1:
        raise AssertionError(f"Stage-0 oracle did not retain imported Box: {nominal_rows}")
    imported_nominal_identity = nominal_rows[0]["identity"]
    specializations = oracle["ssa"].get("generic_specializations", [])
    expected = [row for row in specializations
                if row.get("generic_function") == "mncs:0.2:function:demo.root::read"
                and row.get("canonical_args") == "value:4"]
    if len(expected) != 1:
        raise AssertionError(f"Stage-0 did not resolve exactly read<4>: {specializations}")
    specialization_identity = expected[0]["specialization_function"]

    build, build_metrics = probe.send({"compile_application": application_request()})
    if (
        build.get("schema_version") != "mncs.compiler.application-build/4"
        or build.get("status") != "verified"
        or build.get("entry", {}).get("status") != "verified"
        or build.get("unresolved_obligations") != []
    ):
        artifact = (build.get("artifact") or {}).get("value") or {}
        interface = build.get("entry", {}).get("interface") or {}
        raise AssertionError("selected SSA application did not verify: " + json.dumps({
            "schema_version": build.get("schema_version"),
            "status": build.get("status"),
            "entry_status": build.get("entry", {}).get("status"),
            "interface_status": interface.get("status"),
            "unresolved_obligations": build.get("unresolved_obligations"),
            "diagnostics": build.get("diagnostics"),
            "artifact_id": artifact.get("artifact_id"),
            "callables": artifact.get("callables"),
            "cache": build.get("cache"),
        }, sort_keys=True))
    interface = build["entry"]["interface"]
    if interface.get("types", {}).get("unresolved_references") != []:
        raise AssertionError(f"application interface has unresolved types: {interface}")
    records, finite, _, _ = app_value_builders(interface)
    required_types = {
        "SemanticIdentity", "NominalTypeIdentity", "ImportedRecordField", "ImportedRecordSchema",
        "ValueSsaFunctionStatus",
    }
    if not required_types <= records.keys():
        raise AssertionError(f"selected application interface omitted imported schema types: {records.keys()}")
    if not {"NominalTypeIdentityList", "ImportedRecordList", "ImportedRecordFieldList", "SemType", "ImportedEnumList"} <= finite.keys():
        raise AssertionError(f"selected application interface omitted schema variants: {finite.keys()}")
    artifact = build.get("artifact", {}).get("value")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"application build omitted canonical VM artifact: {build.get('artifact')}")
    artifact_digest = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    correct = build_inputs(interface, imported_nominal_identity, field_is_u64=True)
    wrong_nominal = build_inputs(
        interface, "mncs:0.2:record-type:demo.other::Box::value%3Au64%3B",
        field_is_u64=True, schema_identity=imported_nominal_identity,
    )
    wrong_schema = build_inputs(interface, imported_nominal_identity, field_is_u64=False)
    empty_context = correct["arguments"].copy()
    # A missing identity and schema context must not validate imported Box uses.
    empty_context[2] = {
        "finite": {
            "discriminant": 0, "payload": [],
            "type_identity": finite["NominalTypeIdentityList"]["identity"],
            "variant_identity": next(v["identity"] for v in finite["NominalTypeIdentityList"]["variants"] if v["name"] == "NINil"),
        }
    }
    empty_context[3] = {
        "finite": {
            "discriminant": 0, "payload": [],
            "type_identity": finite["ImportedRecordList"]["identity"],
            "variant_identity": next(v["identity"] for v in finite["ImportedRecordList"]["variants"] if v["name"] == "IRNil"),
        }
    }
    empty_context[4] = {
        "finite": {
            "discriminant": 0, "payload": [],
            "type_identity": finite["ImportedEnumList"]["identity"],
            "variant_identity": next(v["identity"] for v in finite["ImportedEnumList"]["variants"] if v["name"] == "IENil"),
        }
    }
    probe_stderr = probe.close()

    cases = [
        {"id": "imported_Box_read_4", "args": correct["arguments"]},
        {"id": "nominal_schema_identity_mismatch", "args": wrong_nominal["arguments"]},
        {"id": "wrong_imported_field_schema", "args": wrong_schema["arguments"]},
        {"id": "missing_import_context", "args": empty_context},
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-imported-flow-ssa-") as temp_name:
        directory = Path(temp_name)
        artifact_path = directory / "selected-ssa.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, result_bytes = run_vm_batch(artifact_path, cases, directory)

    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different imported-schema artifact")
    decoded = {}
    for case, result in zip(cases, document.get("results", []), strict=True):
        if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM did not complete {case['id']}: {result}")
        returned = result.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned wrong value count for {case['id']}: {result}")
        decoded[case["id"]] = project.decode(vm_to_wire(returned[0]))
    good = decoded["imported_Box_read_4"]
    function_statuses = [good["first_function"], good["second_function"]]
    if (
        not good["valid"] or good["function_count"] != 2 or good["verified_function_count"] != 2
        or not all(row["found"] and row["supported"] and row["verified"] for row in function_statuses)
    ):
        raise AssertionError(f"canonical VM failed imported Box Flow/SSA verification: {good}")
    if not good["has_call"] or good["call_count"] != 1:
        raise AssertionError(f"canonical VM omitted generic call work: {good}")
    generic_call_identity = identity_text(good["first_call_identity"])
    if generic_call_identity != "mncs:0.2:function:demo.root::read":
        raise AssertionError(f"canonical VM generic call identity changed: {generic_call_identity}")
    for negative in ("nominal_schema_identity_mismatch", "wrong_imported_field_schema", "missing_import_context"):
        if decoded[negative]["valid"]:
            raise AssertionError(f"invalid imported interface was accepted by canonical VM: {negative}: {decoded[negative]}")
    if specialization_identity == generic_call_identity:
        raise AssertionError("Stage-0 specialization identity unexpectedly aliases generic callable identity")

    vm_repo = test_vm_emit.VM_REPO
    vm_binary = test_vm_emit.VM_BIN
    vm_revision = subprocess.check_output(["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True).strip()
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (child_after.ru_stime - child_before.ru_stime)
    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in MODULE_NAMES
    }
    verification_work = build.get("cache", {}).get("verification_work", {})
    canonical = {"good": good, "generic_call_identity": generic_call_identity,
                 "stage0_specialization_identity": specialization_identity,
                 "negative_cases": {key: decoded[key] for key in decoded if key != "imported_Box_read_4"}}
    semantic_digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    source_bytes = DEPENDENCY.encode() + b"\0" + ROOT_SOURCE.encode()
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "imported-nominal-generic-flow-ssa-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
            "selected_worktree": str(ROOT),
            "selected_modules": MODULE_NAMES,
            "module_source_sha256": module_hashes,
            "test_harness": "tools/test_vm_project_imported_flow_ssa.py",
            "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "toolchains": {
            "stage0_probe": str(PROBE), "stage0_probe_sha256": sha(PROBE.read_bytes()),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "vm_binary": str(vm_binary), "vm_binary_sha256": sha(vm_binary.read_bytes()),
            "vm_repository": str(vm_repo), "vm_revision": vm_revision,
        },
        "workload": {
            "module": VM_MODULE, "function": f"run -> {TARGET_MODULE}::{TARGET_FUNCTION}<4,64>",
            "source_sha256": sha(source_bytes),
            "source_bytes": {"dependency": len(DEPENDENCY.encode()), "root": len(ROOT_SOURCE.encode())},
            "imported_nominal_identity": imported_nominal_identity,
            "imported_nominal_reference_spans": correct["nominal_spans"],
            "generic_call_identity": generic_call_identity,
            "stage0_specialization_identity": specialization_identity,
            "artifact_id": artifact["artifact_id"],
            "artifact_sha256": artifact_digest,
            "artifact_bytes": len(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()),
            "application_cache_key": build.get("cache", {}).get("key"),
        },
        "verification": {
            "application_build_contract": build["schema_version"],
            "application_build_status": build["status"],
            "application_entry_status": build["entry"]["status"],
            "canonical_vm_fresh_process": True,
            "canonical_vm_calls": len(cases),
            "positive_imported_schema_flow_ssa": True,
            "verified_functions": good["verified_function_count"],
            "function_statuses": function_statuses,
            "generic_call_matches_stage0_function": generic_call_identity == expected[0]["generic_function"],
            "stage0_generic_specialization_resolved": True,
            "invalid_import_contexts_rejected": ["nominal_schema_identity_mismatch", "wrong_imported_field_schema", "missing_import_context"],
            "language_verified_facts_reused": 0,
            "application_verification_work": verification_work,
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "application_build": build_metrics,
            "canonical_vm_batch_seconds": round(vm_wall, 6),
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": child_after.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_peak_kb,
            "host_memory_before": host_before,
            "cgroup_memory_before": cgroup_before,
            "cgroup_memory_after": cgroup_snapshot(),
            "canonical_vm_batch_bytes": {"calls": calls_bytes, "results": result_bytes},
            "stage0_probe_stderr_sha256": sha(probe_stderr.encode()),
            "cgroup_peak_attributable_to_test": False,
        },
        "claim_boundary": (
            "This current-source canonical VM call proves declaration checking, Flow lowering, and verified "
            "value SSA consume explicit imported Box identity and field schema inputs, including rejection "
            "of mismatched or absent imported context. Stage-0 independently resolves read<4>; this selected "
            "SSA path retains the generic callable identity and does not prove native specialization cloning, "
            "Project-wide module resolution, Language verified-fact reuse, native Test conversion, or compiler succession."
        ),
    }
    report["evidence_artifact"] = "evidence/self-host-imported-nominal-generic-flow-ssa-20261016.json"
    output = ROOT / os.environ.get("MNCS_PROJECT_FLOW_SSA_EVIDENCE_PATH", report["evidence_artifact"])
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
