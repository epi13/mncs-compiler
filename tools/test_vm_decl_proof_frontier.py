#!/usr/bin/env python3
"""Check selected `decl.prove_unit` results on a fresh canonical VM.

Stage-0 supplies independent oracle and elaboration diagnostics and emits the
current-source VM artifact. The proof implementation itself runs only in the
canonical VM batch. This keeps the bounded frontier inside Environment's
fixed invocation window without presenting backend startup as proof evidence.
One case embeds an exact, import-free declaration from current Compiler source
in a standalone unit so the current declaration verifier checks its body.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_sem as sem
from test_vm_segment import run_batch, vm_to_wire, test_vm_emit


ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.decl.v1"
SOURCE_PATH = "src/compiler/source.mncs"
SOURCE_RAW = (ROOT / SOURCE_PATH).read_bytes()
SOURCE_FUNCTION_NAME = b"span_valid_global"
SOURCE_FUNCTION_CASE = "compiler-source-span-valid-global"
SOURCE_FUNCTION_START = SOURCE_RAW.index(b"fn " + SOURCE_FUNCTION_NAME + b"(")
SOURCE_FUNCTION_OPEN = SOURCE_RAW.index(b"{", SOURCE_FUNCTION_START)
_brace_depth = 0
SOURCE_FUNCTION_END = None
for _offset in range(SOURCE_FUNCTION_OPEN, len(SOURCE_RAW)):
    if SOURCE_RAW[_offset] == ord("{"):
        _brace_depth += 1
    elif SOURCE_RAW[_offset] == ord("}"):
        _brace_depth -= 1
        if _brace_depth == 0:
            SOURCE_FUNCTION_END = _offset + 1
            break
if SOURCE_FUNCTION_END is None:
    raise RuntimeError("could not find the end of current source.span_valid_global")
SOURCE_FUNCTION_BYTES = SOURCE_RAW[SOURCE_FUNCTION_START:SOURCE_FUNCTION_END]
SOURCE_FUNCTION_TEXT = (
    "mncs 0.18;\nmodule self.compiler_source;\n\n"
    + SOURCE_FUNCTION_BYTES.decode("ascii")
    + "\n"
)
SELECTED_CASES = (
    "clean-add",
    "clean-div-nonlit",
    "callee",
    "clean-if",
    "clean-record-proj",
    "generic-kind-type-as-nat",
    SOURCE_FUNCTION_CASE,
)
CASE_BY_NAME = {name: (text, unknowns) for name, text, unknowns in sem.CASES}
CASE_BY_NAME[SOURCE_FUNCTION_CASE] = (SOURCE_FUNCTION_TEXT, set())
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN", ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
))


class Probe:
    """Minimal Stage-0 route for oracle facts and direct VM artifact emission."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": MODULE,
            "function": "prove_unit",
            "type_arguments": [sem.nat_arg(sem.PAGE_BOUND), sem.nat_arg(sem.STRIDE_BOUND)],
        }])
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build/probe-cache"))
        self.proc = subprocess.Popen(
            [str(PROBE)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

    def send(self, request: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Stage-0 probe stopped: {self.proc.poll()}")
        return json.loads(line)

    def close(self) -> str:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        stderr = self.proc.stderr.read()
        result = self.proc.wait(timeout=30)
        if result != 0:
            raise RuntimeError(f"Stage-0 probe exited {result}: {stderr[-2000:]}")
        return stderr


def request_for(text: str) -> dict:
    data = sem.source_bytes(text)
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": "prove_unit"},
        "arguments": sem.logical_args(data),
        "type_arguments": [sem.nat_arg(sem.PAGE_BOUND), sem.nat_arg(sem.STRIDE_BOUND)],
        "step_budget": 8_000_000,
    }


def make_oracle_case(probe: Probe, name: str, text: str, expected_unknowns: set[int]) -> dict:
    source = sem.source_bytes(text).decode()
    oracle = probe.send({"oracle": source})
    diagnostics = [
        (item["code"], item["span"]["start"], item["span"]["end"])
        for item in probe.send({"elaborate": source})
        if item.get("code", "").startswith("MNE")
    ]
    expected_fn_count = 0 if any(code == "MNE104" for code, _, _ in diagnostics) else len(
        oracle["ast"]["functions"]
    )
    data = text.encode()
    return {
        "name": name,
        "request": request_for(text),
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_bytes": len(data),
        "diagnostics": diagnostics,
        "expected_fn_count": expected_fn_count,
        "expected_unknowns": expected_unknowns,
    }


def check_result(
    name: str,
    value: dict,
    diagnostics: list[tuple[str, int, int]],
    expected_unknowns: set[int],
    expected_fn_count: int,
) -> None:
    obligations = sem.flist(value["obls"], 1)
    failures = [
        (sem.KIND_TO_MNE[item["kind"]], item["start"], item["end"])
        for item in obligations if item["status"] == 1
    ]
    unknowns = {item["kind"] for item in obligations if item["status"] == 2}
    if failures != diagnostics:
        raise AssertionError(f"{name} failures differ from Stage-0: {failures} != {diagnostics}")
    if not unknowns <= sem.UNKNOWN_KINDS:
        raise AssertionError(f"{name} has unclassified UNKNOWN obligations: {unknowns}")
    if name == SOURCE_FUNCTION_CASE and unknowns:
        raise AssertionError(
            f"{name} contains UNKNOWN obligations instead of a complete body proof: {unknowns}"
        )
    if not expected_unknowns <= unknowns:
        raise AssertionError(f"{name} lost expected UNKNOWN obligations: {unknowns}")
    if value["ok"] != (not diagnostics):
        raise AssertionError(f"{name} returned ok={value['ok']} for diagnostics {diagnostics}")
    if value["fn_count"] != expected_fn_count:
        raise AssertionError(f"{name} function count {value['fn_count']} != {expected_fn_count}")


def semantic_digest(values: list[dict]) -> str:
    state = hashlib.sha256()
    for value in values:
        state.update(json.dumps([value["name"], value["value"]], sort_keys=True).encode() + b"\n")
    return state.hexdigest()


def host_memory_snapshot() -> dict | None:
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


def evidence_output_path() -> Path:
    configured = os.environ.get("MNCS_DECL_PROOF_FRONTIER_EVIDENCE_PATH")
    if configured:
        output = Path(configured)
        if not output.is_absolute():
            output = ROOT / output
        if output.exists() or output.is_symlink():
            raise SystemExit(f"refusing to overwrite immutable evidence: {output}")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        output = ROOT / "evidence" / f"self-host-decl-prove-unit-current-{stamp}-{os.getpid()}.json"
    try:
        output = output.resolve(strict=False)
        output.relative_to(ROOT.resolve())
    except (OSError, ValueError) as error:
        raise SystemExit(
            f"declaration proof evidence path must remain inside the Compiler checkout: {output}"
        ) from error
    if output.exists() or output.is_symlink():
        raise SystemExit(f"refusing to overwrite immutable evidence: {output}")
    return output


def main() -> None:
    if not PROBE.exists():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    output = evidence_output_path()
    memory_before = host_memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()

    probe = Probe()
    cases = [make_oracle_case(probe, name, *CASE_BY_NAME[name]) for name in SELECTED_CASES]
    emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
    artifact = emitted.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"Stage-0 did not emit a canonical VM artifact: {emitted}")
    probe_stderr = probe.close()

    with tempfile.TemporaryDirectory(prefix="mncs-vm-decl-proof-") as directory_name:
        directory = Path(directory_name)
        artifact_path = directory / "decl-proof-current.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        batch_cases = [{
            "id": f"decl.prove_unit:{case['name']}",
            "function": "prove_unit",
            "args": case["request"]["arguments"],
            "type_args": case["request"]["type_arguments"],
            "step_budget": case["request"]["step_budget"],
        } for case in cases]
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = run_batch(
            artifact_path, batch_cases, directory, module=MODULE
        )

    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different artifact")
    vm_values = []
    vm_steps = []
    for case, batch_case, result in zip(cases, batch_cases, document.get("results", [])):
        if result.get("id") != batch_case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['name']}: {result}")
        wire = [vm_to_wire(value) for value in result["record"]["returned"]]
        if len(wire) != 1:
            raise AssertionError(f"canonical VM returned wrong result count for {case['name']}")
        value = sem.decode(wire[0])
        check_result(
            case["name"], value, case["diagnostics"], case["expected_unknowns"],
            case["expected_fn_count"],
        )
        case["unknown_kinds"] = sorted({
            obligation["kind"] for obligation in sem.flist(value["obls"], 1)
            if obligation["status"] == 2
        })
        vm_values.append({"name": case["name"], "value": value})
        vm_steps.append(result["record"]["usage"]["steps"])

    modules = ["source", "lexer", "parser", "segment", "decl"]
    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in modules
    }
    vm_repository = test_vm_emit.VM_REPO
    vm_binary = test_vm_emit.VM_BIN
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repository), "rev-parse", "HEAD"], text=True
    ).strip()
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (
        child_after.ru_stime - child_before.ru_stime
    )
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "decl-prove-unit-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
            "selected_worktree": str(ROOT),
            "selected_modules": modules,
            "module_source_sha256": module_hashes,
            "decl_source_sha256": module_hashes["decl"],
            "test_harness": "tools/test_vm_decl_proof_frontier.py",
            "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "current_source_semantic_probe": {
            "source_path": SOURCE_PATH,
            "module_source_sha256": hashlib.sha256(SOURCE_RAW).hexdigest(),
            "function_name": SOURCE_FUNCTION_NAME.decode("ascii"),
            "function_span": [SOURCE_FUNCTION_START, SOURCE_FUNCTION_END],
            "function_source_sha256": hashlib.sha256(SOURCE_FUNCTION_BYTES).hexdigest(),
            "standalone_unit_sha256": hashlib.sha256(SOURCE_FUNCTION_TEXT.encode()).hexdigest(),
            "imports": [],
            "proof_result_requires_no_unknown_obligations": True,
        },
        "toolchains": {
            "stage0_probe": str(PROBE),
            "stage0_probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "vm_binary": str(vm_binary),
            "vm_binary_sha256": hashlib.sha256(vm_binary.read_bytes()).hexdigest(),
            "vm_repository": str(vm_repository),
            "vm_revision": vm_revision,
        },
        "workload": {
            "module": MODULE,
            "function": "prove_unit<1024,1024>",
            "artifact_id": artifact["artifact_id"],
            "selected_case_count": len(cases),
            "cases": [{
                "name": case["name"],
                "source_bytes": case["source_bytes"],
                "source_sha256": case["source_sha256"],
                "stage0_diagnostics": [list(item) for item in case["diagnostics"]],
                "expected_unknown_obligation_kinds": sorted(case["expected_unknowns"]),
                "unknown_obligation_kinds": case["unknown_kinds"],
            } for case in cases],
        },
        "verification": {
            "stage0_oracle_and_elaboration_cases": len(cases),
            "canonical_vm_proof_cases": len(vm_values),
            "canonical_vm_semantic_digest": semantic_digest(vm_values),
            "canonical_vm_steps": vm_steps,
            "unknown_obligation_kinds": sorted({
                kind for case in cases for kind in case["unknown_kinds"]
            }),
            "artifact_identity_matched": True,
            "stage0_diagnostics_match_fail_obligations": True,
            "unknown_obligations_remain_explicit": True,
            "language_verified_facts_reused": 0,
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "canonical_vm_batch_seconds": round(vm_wall, 6),
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": child_after.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_peak_kb,
            "host_memory_before": memory_before,
            "cgroup_memory_before": cgroup_before,
            "cgroup_memory_after": cgroup_snapshot(),
            "canonical_vm_batch_bytes": {"calls": calls_bytes, "results": results_bytes},
            "stage0_probe_stderr_sha256": hashlib.sha256(probe_stderr.encode()).hexdigest(),
            "cgroup_peak_attributable_to_test": False,
        },
        "claim_boundary": (
            "This is a selected current-source decl.prove_unit semantic frontier: Stage-0 MNE "
            "diagnostics match canonical VM FAIL obligations, UNKNOWN obligations stay explicit, "
            "and the proof implementation runs on a fresh canonical VM artifact. One case embeds "
            "the exact import-free source.span_valid_global declaration and requires a complete "
            "body proof with no UNKNOWN obligations. The standalone wrapper does not prove the "
            "full Source module or its imported interfaces. This does not claim Cranelift "
            "differential coverage, Language-owned verified-fact reuse, or compiler succession."
        ),
    }
    report["evidence_artifact"] = output.relative_to(ROOT).as_posix()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    performance_path = ROOT / ".build/vm-efficiency/decl-proof-frontier.json"
    performance_path.parent.mkdir(parents=True, exist_ok=True)
    performance_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
