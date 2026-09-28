#!/usr/bin/env python3
"""Focused imported nominal ownership check through verified value SSA."""

import hashlib
import json
import os
import subprocess
import shutil
import resource
import sys
from pathlib import Path
import tempfile
import time

NOMINAL_MODULE_BOUND = 2
NOMINAL_BYTE_BOUND = int(os.environ.get("MNCS_IMPORTED_NOMINAL_BYTE_BOUND", "64"))
FIXTURE_KIND = os.environ.get("MNCS_IMPORTED_NOMINAL_FIXTURE", "record")
if FIXTURE_KIND == "record":
    NOMINAL_TYPES = "mncs 0.18; module b; record T{x:u64}fn f(x:T)->(r:T){return x;}"
    NOMINAL_ROOT = "mncs 0.18; module a; use b;fn g(x:b.T)->(r:b.T){return b.f(x);}"
    ROOT_FUNCTION_COUNT = 1
elif FIXTURE_KIND == "finite":
    NOMINAL_TYPES = "mncs 0.18;module b;enum E{A}fn f(x:E)->(r:E){return x;}"
    NOMINAL_ROOT = "mncs 0.18;module a;use b;fn g(x:b.E)->(r:b.E){return b.f(x);}"
    ROOT_FUNCTION_COUNT = 1
elif FIXTURE_KIND == "nested":
    NOMINAL_TYPES = "mncs 0.18;module b;record I{v:u64}record O{i:I}fn f(x:O)->(r:O){return x;}"
    NOMINAL_ROOT = "mncs 0.18;module a;use b;fn g(x:b.O)->(r:b.O){return b.f(x);}"
    ROOT_FUNCTION_COUNT = 1
elif FIXTURE_KIND == "effects":
    NOMINAL_TYPES = "mncs 0.18;module b;record T{x:u64}fn f(x:T)->(r:T) capability auth effect read_state authorized_by auth{return x;}"
    NOMINAL_ROOT = "mncs 0.18;module a;use b;fn g(x:b.T)->(r:b.T) capability auth effect read_state authorized_by auth{return b.f(x);}"
    ROOT_FUNCTION_COUNT = 1
elif FIXTURE_KIND == "backend":
    NOMINAL_TYPES = "mncs 0.18; module b; record T{x:u64} fn f(x:u64)->(r:u64){return x;}"
    NOMINAL_ROOT = "mncs 0.18; module a; use b; record A{item:b.T} fn g(flag:bool,x:u64)->(r:u64){let value:u64=b.f(x); if flag {} else {} return value + 1;}"
    ROOT_FUNCTION_COUNT = 1
else:
    raise ValueError(f"unsupported imported nominal fixture: {FIXTURE_KIND}")
STAGE_MODE = os.environ.get("MNCS_IMPORTED_NOMINAL_MODE", "all")
STAGE_CONFIG = {
    "resolve": (
        (("build_modules", [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND]),
         ("reverse_modules", [64]),
         ("resolve_imports", [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND]),
         ("collect_local_nominal_modules", [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND]),
         ("resolve_project_nominal_modules", [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND])),
        "mncs.compiler.project.v1",
    ),
    "signatures": ((("imported_signatures", [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND]),), "mncs.compiler.project.v1"),
    "proof": ((("prove_parsed_unit_with_nominals", [NOMINAL_BYTE_BOUND]),), "mncs.compiler.decl.v1"),
    "cfg": ((("lower_proven_unit", [NOMINAL_BYTE_BOUND]),), "mncs.compiler.flow.v1"),
    "ssa": ((("lower_value_ssa", [NOMINAL_BYTE_BOUND]),), "mncs.compiler.ssa.v1"),
}
if STAGE_MODE not in {*STAGE_CONFIG, "all", "native-backend"}:
    raise ValueError(f"unsupported imported nominal mode: {STAGE_MODE}")
if STAGE_MODE in {"all", "native-backend"}:
    STAGE_TYPE_ARGUMENTS = ()
    STAGE_MODULES = None
else:
    STAGE_TYPE_ARGUMENTS, STAGE_MODULES = STAGE_CONFIG[STAGE_MODE]
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = STAGE_MODULES
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": STAGE_MODULES,
            "function": function,
            "type_arguments": [
                {"kind": "nat", "value": value} for value in type_arguments
            ],
        }
        for function, type_arguments in STAGE_TYPE_ARGUMENTS
    ])

from test_project import (
    Probe,
    decode,
    discover_sources,
    flist,
    identity_map,
    request_value,
    wire_field,
    wire_flist,
    wire_number,
)

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = os.environ.get("MNCS_CAMPAIGN_ID", time.strftime("%Y%m%d", time.gmtime()))


def campaign_artifact_directory() -> Path:
    configured = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
    return Path(configured) if configured else ROOT / ".build" / "campaign-artifacts"


def run_native_backend_vertical() -> dict:
    if FIXTURE_KIND != "backend":
        raise ValueError("native backend vertical requires MNCS_IMPORTED_NOMINAL_FIXTURE=backend")
    root_source = NOMINAL_ROOT
    dep_source = NOMINAL_TYPES
    fixture_digest = hashlib.sha256(
        b"two-module-imported-backend-v1\0" + dep_source.encode() + b"\0" + root_source.encode()
    ).hexdigest()
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.mncs"), key=lambda item: item.relative_to(ROOT).as_posix()):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    compiler_digest = digest.hexdigest()
    lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    session_artifacts = campaign_artifact_directory()
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", session_artifacts / "cargo-target"))
    target_dir.mkdir(parents=True, exist_ok=True)
    build_env = os.environ.copy()
    build_env["CARGO_TARGET_DIR"] = str(target_dir)
    build_env["CARGO_INCREMENTAL"] = "0"
    cargo = os.environ.get("CARGO", "cargo")
    language_checkout = Path(os.environ.get(
        "MNCS_LANGUAGE_CHECKOUT",
        "/workspace/mncs-language/.worktrees/compiler-parity-language-20260927",
    ))

    build_started = time.monotonic()
    if os.environ.get("MNCS_NATIVE_BACKEND_PREBUILT") != "1":
        build_commands = [
            (
                [cargo, "build", "--offline", "--manifest-path",
                 str(ROOT / "tools/stage0-probe/Cargo.toml"), "--bin",
                 "mncs-compiler-stage0-probe"],
                ROOT,
            ),
            (
                [cargo, "build", "--offline", "--manifest-path",
                 str(language_checkout / "Cargo.toml"), "-p", "mncs-cli", "--bin", "mncs"],
                language_checkout,
            ),
        ]
        for args, cwd in build_commands:
            built = subprocess.run(args, cwd=cwd, env=build_env, capture_output=True, text=True, timeout=75)
            if built.returncode != 0:
                raise RuntimeError(f"campaign backend tool build failed: {args}: {built.stderr[-5000:]}")
    build_seconds = time.monotonic() - build_started

    probe_binary = target_dir / "debug" / "mncs-compiler-stage0-probe"
    cli_binary = target_dir / "debug" / "mncs"
    if not probe_binary.is_file() or not cli_binary.is_file():
        raise FileNotFoundError("campaign backend tools are missing from the Environment-owned output directory")
    if os.environ.get("MNCS_NATIVE_BACKEND_BUILD_ONLY") == "1":
        return {
            "schema_version": 1,
            "status": "tools-built",
            "stage0_revision": lock["revision"],
            "compiler_source_sha256": compiler_digest,
            "language_checkout": str(language_checkout.resolve()),
            "target_directory": str(target_dir),
            "probe_binary": str(probe_binary),
            "cli_binary": str(cli_binary),
            "tool_build_seconds": round(build_seconds, 6),
            "peak_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        }

    state_path = session_artifacts / "compiler-facts" / f"campaign-{CAMPAIGN_ID}-imported-nominal-resolved-facts.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"saved native facts are missing: {state_path}")
    state = json.loads(state_path.read_text())
    if state.get("compiler_checkout") != str(ROOT.resolve()):
        raise ValueError("saved native SSA facts belong to a different selected compiler checkout")
    if state.get("compiler_source_sha256") != compiler_digest:
        raise ValueError("saved native SSA facts belong to a different compiler revision")
    if state.get("stage0_revision") != lock["revision"]:
        raise ValueError("saved native SSA facts use a different pinned Stage-0 revision")
    if state.get("fixture_sha256") != fixture_digest:
        raise ValueError("saved native SSA facts belong to a different source fixture")
    if "ssa_summary" not in state or "owner_ssa_summary" not in state:
        raise ValueError("saved facts have not reached verified SSA for both modules")

    os.environ["MNCS_PROBE_BIN"] = str(probe_binary)
    os.environ["MNCS_PROBE_MODULES"] = "project"
    # The Stage-0 project oracle is a Rust reference operation. It does not
    # need another Cranelift compilation of the compiler modules.
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = ""

    def identity_text(identity: dict) -> str:
        assert identity["valid"] is True and identity["len"] > 0, identity
        return bytes(identity["bytes"][:identity["len"]]).decode()

    def type_name(value: dict) -> str:
        variant = value.get("$v")
        payload = value.get("$p", {})
        if variant == 0:
            return "bool"
        if variant == 2 and payload.get("width") == 64 and payload.get("signed") is False:
            return "u64"
        raise ValueError(f"native SSA type is outside the scalar backend envelope: {value}")

    def scalar_value(value_wire: dict) -> dict:
        return {"id": str(value_wire["id"]), "ty": type_name(value_wire["ty"])}

    def id_list(value_wire: dict) -> list[str]:
        return [str(item["value"]) for item in flist(value_wire)]

    def normalized_function(summary: dict) -> dict:
        functions = flist(summary["functions"])
        assert len(functions) == 1, functions
        function = functions[0]
        assert function["supported"] is True and function["verified"] is True, function
        blocks = []
        for block in flist(function["blocks"]):
            instructions = []
            for instruction in flist(block["instructions"]):
                if instruction["kind"] == 0:
                    instructions.append({
                        "kind": "constant",
                        "dest": {"id": str(instruction["result"]), "ty": type_name(instruction["ty"])},
                        "value": int(instruction["operator"]),
                    })
                elif instruction["kind"] == 2:
                    if instruction["operator"] != 50:
                        raise ValueError(f"native SSA binary opcode {instruction['operator']} is outside the C11 slice")
                    operands = id_list(instruction["inputs"])
                    if len(operands) != 2 or type_name(instruction["ty"]) != "u64":
                        raise ValueError("native C11 addition requires two u64 operands and a u64 result")
                    instructions.append({
                        "kind": "integer", "operator": "add",
                        "dest": {"id": str(instruction["result"]), "ty": type_name(instruction["ty"])},
                        "lhs": operands[0], "rhs": operands[1],
                    })
                elif instruction["kind"] == 3:
                    assert flist(instruction["effects"]) == [] and flist(instruction["capabilities"]) == [], instruction
                    instructions.append({
                        "kind": "call",
                        "dest": {"id": str(instruction["result"]), "ty": type_name(instruction["ty"])},
                        "callee": identity_text(instruction["identity"]),
                        "args": id_list(instruction["inputs"]),
                    })
                else:
                    raise ValueError(f"native SSA instruction kind {instruction['kind']} is outside the imported-call/constant/u64-add vertical")
            term = block["terminator"]
            payload = term["$p"]
            if term["$v"] == 0:
                terminator = {
                    "kind": "branch",
                    "condition": str(payload["condition"]),
                    "then_target": int(payload["yes"]),
                    "then_args": id_list(payload["yes_args"]),
                    "else_target": int(payload["no"]),
                    "else_args": id_list(payload["no_args"]),
                }
            elif term["$v"] == 1:
                terminator = {
                    "kind": "jump",
                    "target": int(payload["target"]),
                    "args": id_list(payload["arguments"]),
                }
            elif term["$v"] == 2:
                terminator = {"kind": "return", "value": str(payload["value"])}
            else:
                raise ValueError(f"native SSA terminator {term['$v']} is outside the scalar backend envelope")
            blocks.append({
                "id": int(block["id"]),
                "params": [scalar_value(item) for item in flist(block["parameters"])],
                "instructions": instructions,
                "terminator": terminator,
            })
        return {
            "identity": identity_text(function["identity"]),
            "params": [scalar_value(item) for item in flist(function["inputs"])],
            "result_type": type_name(function["result_type"]),
            "blocks": blocks,
        }

    root_ssa = state["ssa_summary"]
    owner_ssa = state["owner_ssa_summary"]
    assert root_ssa["valid"] is True and root_ssa["verified_function_count"] == 1, root_ssa
    assert root_ssa["call_count"] == 1, root_ssa
    assert owner_ssa["valid"] is True and owner_ssa["verified_function_count"] == 1, owner_ssa
    assert owner_ssa["call_count"] == 0, owner_ssa

    calls = [
        {
            "schema_version": "0.1",
            "target": {"module": "a", "function": "g"},
            "arguments": [
                {"boolean": {"value": flag}},
                {"integer": {"type": {"bits": 64, "signed": False}, "value": number}},
            ],
            "type_arguments": [],
            "step_budget": 10000,
        }
        for flag, number in [(False, 37), (True, 81)]
    ]
    stage0_started = time.monotonic()
    probe = Probe()
    try:
        status = probe.send({"execution_status": True})
        readiness_seconds = time.monotonic() - stage0_started
        oracle = probe.send({
            "project_oracle": {"root": root_source, "modules": {"b": dep_source}, "calls": calls}
        })
        stage0_seconds = time.monotonic() - stage0_started - readiness_seconds
        assert oracle["valid"] is True and oracle["program"] is not None, oracle
        assert len(oracle["executions"]) == len(calls), oracle
        dep_fn = next(
            item for item in oracle["program"]["functions"]
            if item["home_module"] == "b" and item["name"] == "f"
        )
        root_function = flist(root_ssa["functions"])[0]
        owner_function = flist(owner_ssa["functions"])[0]
        assert identity_text(root_ssa["first_call_identity"]) == dep_fn["identity"]
        assert identity_text(owner_function["identity"]) == dep_fn["identity"]

        normalized = {
            "schema_version": "mncs.native-scalar-ssa-structural/1",
            "functions": [normalized_function(root_ssa), normalized_function(owner_ssa)],
        }
        with tempfile.TemporaryDirectory(prefix="mncs-native-c11-") as output_dir:
            output_root = Path(output_dir)
            input_path = output_root / "native-ssa.json"
            source_path = output_root / "module.c"
            driver_path = output_root / "driver.c"
            executable_path = output_root / "run"
            input_path.write_text(json.dumps(normalized))
            lowered = subprocess.run(
                [str(cli_binary), "emit-native-ssa-c11-structural", str(input_path)],
                capture_output=True, text=True, timeout=30,
            )
            if lowered.returncode != 0:
                raise RuntimeError(f"native C11 lowering failed: {lowered.stderr[-3000:]}")
            c_source = lowered.stdout
            source_path.write_text(c_source)
            c_types = {"bool": "uint8_t", "u64": "uint64_t"}
            root_params = normalized["functions"][0]["params"]
            parameter_types = [value["ty"] for value in root_params]
            if sorted(parameter_types) != ["bool", "u64"]:
                raise ValueError(f"native C11 canary expects bool/u64 inputs, got {parameter_types}")
            parameter_declaration = ", ".join(c_types[value["ty"]] for value in root_params)

            def c_arguments(flag: bool, number: int) -> str:
                values = {"bool": int(flag), "u64": number}
                return ", ".join(str(values[ty]) for ty in parameter_types)

            driver_source = f"""#include <stdint.h>
#include <stdio.h>
extern void mncs_a__g({parameter_declaration}, int32_t *, int64_t *, uint64_t);
int main(void) {{
  int32_t status;
  int64_t value;
  mncs_a__g({c_arguments(False, 37)}, &status, &value, 64);
  printf("%d %lld\\n", status, (long long)value);
  mncs_a__g({c_arguments(True, 81)}, &status, &value, 64);
  printf("%d %lld\\n", status, (long long)value);
  return 0;
}}
"""
            driver_path.write_text(driver_source)
            compiler = shutil.which("clang") or shutil.which("cc")
            if compiler is None:
                raise RuntimeError("native C11 backend verification requires clang or cc")
            compile_started = time.monotonic()
            compiled = subprocess.run(
                [compiler, "-std=c11", "-O0", str(source_path), str(driver_path), "-lm", "-o", str(executable_path)],
                capture_output=True, text=True, timeout=30,
            )
            compile_seconds = time.monotonic() - compile_started
            if compiled.returncode != 0:
                raise RuntimeError(f"emitted C did not compile: {compiled.stderr[-5000:]}")
            execution_started = time.monotonic()
            executed = subprocess.run([str(executable_path)], capture_output=True, text=True, timeout=10)
            execution_seconds = time.monotonic() - execution_started
            if executed.returncode != 0:
                raise RuntimeError(f"emitted C exited {executed.returncode}: {executed.stderr}")
            outputs = [tuple(map(int, line.split())) for line in executed.stdout.splitlines()]
            expected = []
            for result in oracle["executions"]:
                assert result["status"] == "returned", result
                returned = result["returned"][0]
                payload = next(iter(returned.values())) if len(returned) == 1 else returned
                expected.append((0, int(payload["value"])))
            if outputs != expected:
                raise AssertionError({"native_c11": outputs, "pinned_stage0": expected})

        ssa_fact_digest = hashlib.sha256(json.dumps(
            [state["ssa_wire"], state["owner_ssa_wire"]], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        artifact = {
            "schema_version": 1,
            "compiler_checkout": str(ROOT.resolve()),
            "compiler_source_sha256": compiler_digest,
            "stage0_revision": lock["revision"],
            "fixture_sha256": fixture_digest,
            "verified_ssa_facts": {
                "artifact": state_path.name,
                "sha256": ssa_fact_digest,
                "root": {key: root_ssa[key] for key in (
                    "valid", "verified_function_count", "call_count", "block_count", "block_parameter_count"
                )},
                "owner": {key: owner_ssa[key] for key in (
                    "valid", "verified_function_count", "call_count", "block_count", "block_parameter_count"
                )},
            },
            "normalized_native_ssa": normalized,
            "c11_source": c_source,
            "c11_driver": driver_source,
            "native_entrypoint_parameter_order": parameter_types,
            "pinned_stage0_executions": oracle["executions"],
            "emitted_c_executions": outputs,
            "observable_behavior_equal": True,
        }
        return {
            "schema_version": 1,
            "status": "verified",
            "scope": "test-verified imported two-module SSA was projected into explicitly unattested structural C11 input; emitted execution matched the pinned Stage-0 oracle",
            "native_root_identity": identity_text(root_function["identity"]),
            "native_imported_callable_identity": identity_text(root_ssa["first_call_identity"]),
            "native_root_blocks": root_ssa["block_count"],
            "native_root_block_parameters": root_ssa["block_parameter_count"],
            "native_verified_functions": root_ssa["verified_function_count"] + owner_ssa["verified_function_count"],
            "stage0_revision": lock["revision"],
            "observable_behavior_equal": True,
            "native_pipeline_seconds": sum(
                item.get("elapsed_seconds", 0.0)
                for item in state.get("resolution_stages", []) + state.get("lowering_stages", [])
            ),
            "pinned_stage0_seconds": round(stage0_seconds, 6),
            "tool_build_seconds": round(build_seconds, 6),
            "c11_compile_seconds": round(compile_seconds, 6),
            "c11_execution_seconds": round(execution_seconds, 6),
            "readiness_seconds": round(readiness_seconds, 6),
            "planning_seconds": 0.0,
            "requests": probe.requests,
            "retained_sessions": status.get("retained_sessions", 0),
            "peak_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "native_backend_artifact": artifact,
        }
    finally:
        probe.close()


def run() -> dict:
    if STAGE_MODE == "native-backend":
        return run_native_backend_vertical()
    def progress(stage: str) -> None:
        print(json.dumps({"stage": stage}), flush=True)

    progress(f"fixture-mode:{STAGE_MODE}:byte-bound:{NOMINAL_BYTE_BOUND}")

    def identity_text(identity: dict) -> str:
        assert identity["valid"] is True and identity["len"] > 0, identity
        return bytes(identity["bytes"][:identity["len"]]).decode()

    def nat_values(values: list[int]) -> list[dict]:
        return [{"kind": "nat", "value": value} for value in values]

    def compiler_source_digest() -> str:
        digest = hashlib.sha256()
        for path in sorted((ROOT / "src").rglob("*.mncs"), key=lambda item: item.relative_to(ROOT).as_posix()):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def invoke_stage(
        probe: Probe,
        name: str,
        module: str,
        arguments: list,
        type_arguments: list[int],
        stage_reports: list[dict],
        stage_budget: int,
    ):
        progress(f"waiting-for-native-project-stage:{name}")
        progress(f"native-stage-type-arguments:{name}:{type_arguments}")
        request = {
            "schema_version": "0.1",
            "target": {"module": module, "function": name},
            "arguments": arguments,
            "type_arguments": nat_values(type_arguments),
            "step_budget": stage_budget,
        }
        started = time.monotonic()
        response = probe.send(request)
        elapsed = time.monotonic() - started
        stage_reports.append({
            "stage": name,
            "module": module,
            "status": response.get("status", "unknown"),
            "steps": response.get("steps"),
            "elapsed_seconds": round(elapsed, 6),
        })
        if response.get("status") != "returned" or not response.get("returned"):
            raise StageBudgetFailure(name, response)
        probe.steps.append(response["steps"])
        progress(f"native-project-stage-returned:{name}")
        raw = response["returned"][0]
        return raw, decode(raw)

    artifact_transport = os.environ.get("MNCS_CAMPAIGN_ARTIFACT_TRANSPORT") == "stdout"
    session_artifact_directory = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
    facts_directory = campaign_artifact_directory() / "compiler-facts"
    state_path = facts_directory / f"campaign-{CAMPAIGN_ID}-imported-nominal-resolved-facts.json"

    def persist_state(value: dict) -> None:
        # Environment-owned session artifacts are the persistent scratch space
        # for read-only bound compiler checkouts. Only final evidence is handed
        # back to the checkout owner for repo-level persistence.
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(value, indent=2) + "\n")

    def exported_facts(value: dict) -> dict:
        if not artifact_transport or STAGE_MODE != "ssa":
            return {}
        artifact = {
            "schema_version": 1,
            "stage0_revision": value["stage0_revision"],
            "compiler_checkout": value["compiler_checkout"],
            "compiler_source_sha256": value["compiler_source_sha256"],
            "fixture_sha256": value["fixture_sha256"],
            "ssa_wire": value["ssa_wire"],
            "ssa_summary": value["ssa_summary"],
        }
        if "owner_ssa_wire" in value:
            artifact["owner_ssa_wire"] = value["owner_ssa_wire"]
            artifact["owner_ssa_summary"] = value["owner_ssa_summary"]
        return {"native_ssa_artifact": artifact}

    stage_budget = int(os.environ.get("MNCS_PROJECT_STAGE_STEP_BUDGET", "1000000"))
    fixture_tag = (
        b"two-module-imported-nominal-v1\0"
        if FIXTURE_KIND == "record"
        else f"two-module-imported-{FIXTURE_KIND}-v1\0".encode()
    )
    fixture_digest = hashlib.sha256(
        fixture_tag + NOMINAL_TYPES.encode() + b"\0" + NOMINAL_ROOT.encode()
    ).hexdigest()
    compiler_digest = compiler_source_digest()
    test_harness_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    try:
        if STAGE_MODE == "resolve":
            with tempfile.TemporaryDirectory(prefix="mncs-imported-nominal-") as directory:
                project_root = Path(directory)
                (project_root / "a-root.mncs").write_text(NOMINAL_ROOT)
                (project_root / "b-nominal-dep.mncs").write_text(NOMINAL_TYPES)
                sources = discover_sources(project_root)
                progress("starting-retained-stage0-probe")
                probe = Probe()
                stage_reports: list[dict] = []
                started = time.monotonic()
                try:
                    progress("waiting-for-stage0-execution-status")
                    execution = probe.send({"execution_status": True})
                    readiness_seconds = time.monotonic() - started
                    progress("stage0-probe-ready")
                    if os.environ.get("MNCS_PROBE_BACKEND") == "cranelift":
                        assert execution["backend"] == "cranelift", execution
                        assert execution["retained_sessions"] == 1, execution
                    identities = identity_map(probe)
                    native_request = request_value(
                        identities, sources, module_bound=NOMINAL_MODULE_BOUND,
                        byte_bound=NOMINAL_BYTE_BOUND,
                    )
                    snapshot, text_units = native_request["arguments"]
                    source_wires = wire_field(snapshot, "sources")

                    built_wire, built = invoke_stage(
                        probe, "build_modules", "mncs.compiler.project.v1",
                        [source_wires, text_units],
                        [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND],
                        stage_reports, stage_budget,
                    )
                    assert built["valid"] is True and built["count"] == 2, built

                    parsed_wire, parsed = invoke_stage(
                        probe, "reverse_modules", "mncs.compiler.project.v1",
                        [source_wires, wire_field(built_wire, "acc")], [64],
                        stage_reports, stage_budget,
                    )
                    assert len(flist(parsed)) == 2

                    imports_wire, imports = invoke_stage(
                        probe, "resolve_imports", "mncs.compiler.project.v1",
                        [source_wires, text_units, parsed_wire],
                        [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND],
                        stage_reports, stage_budget,
                    )
                    assert imports["valid"] is True and imports["count"] == 1, imports

                    local_wire, local_modules = invoke_stage(
                        probe, "collect_local_nominal_modules", "mncs.compiler.project.v1",
                        [text_units, parsed_wire],
                        [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND],
                        stage_reports, stage_budget,
                    )
                    nominal_wire, nominal_modules = invoke_stage(
                        probe, "resolve_project_nominal_modules", "mncs.compiler.project.v1",
                        [text_units, parsed_wire, local_wire],
                        [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND],
                        stage_reports, stage_budget,
                    )

                    nominal_facts = flist(nominal_modules)
                    owner_fact = next(item for item in nominal_facts if item["source_index"] == 1)
                    root_fact = next(item for item in nominal_facts if item["source_index"] == 0)
                    owner_identity = next(
                        item["identity"] for item in flist(owner_fact["resolved"])
                        if not item["reference"]
                    )
                    imported_reference = next(
                        item for item in flist(root_fact["resolved"])
                        if item["reference"]
                    )
                    owner_identities = [
                        identity_text(item["identity"])
                        for item in flist(owner_fact["resolved"])
                        if not item["reference"]
                    ]
                    imported_reference_identities = [
                        identity_text(item["identity"])
                        for item in flist(root_fact["resolved"])
                        if item["reference"]
                    ]
                    assert set(imported_reference_identities).issubset(set(owner_identities))

                    wire_nominal_facts = wire_flist(nominal_wire)
                    root_nominal_wire = next(
                        item for item in wire_nominal_facts
                        if wire_number(wire_field(item, "source_index")) == 0
                    )
                    owner_nominal_wire = next(
                        item for item in wire_nominal_facts
                        if wire_number(wire_field(item, "source_index")) == 1
                    )
                    wire_parsed_modules = wire_flist(parsed_wire)
                    root_parsed_wire = next(
                        item for item in wire_parsed_modules
                        if wire_number(wire_field(item, "source_index")) == 0
                    )
                    owner_parsed_wire = next(
                        item for item in wire_parsed_modules
                        if wire_number(wire_field(item, "source_index")) == 1
                    )
                    initial_wire, initial = invoke_stage(
                        probe, "begin_project_lowering", "mncs.compiler.project.v1",
                        [parsed_wire, wire_field(imports_wire, "imports"), nominal_wire], [],
                        stage_reports, stage_budget,
                    )
                    assert initial["count"] == 0 and initial["valid"] is True

                    lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
                    state = {
                        "schema_version": 1,
                        "compiler_checkout": str(ROOT.resolve()),
                        "compiler_source_sha256": compiler_digest,
                        "stage0_revision": lock["revision"],
                        "fixture_sha256": fixture_digest,
                        "project_source_ids": ["a-root.mncs", "b-nominal-dep.mncs"],
                        "module_bounds": [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND],
                        "source_wires": source_wires,
                        "text_units": text_units,
                        "parsed_wire": parsed_wire,
                        "imports_wire": wire_field(imports_wire, "imports"),
                        "nominal_wire": nominal_wire,
                        "root_parsed_wire": root_parsed_wire,
                        "root_nominals_wire": wire_field(root_nominal_wire, "resolved"),
                        "owner_parsed_wire": owner_parsed_wire,
                        "owner_nominals_wire": wire_field(owner_nominal_wire, "resolved"),
                        "owner_input": text_units["sequence"]["values"][1],
                        "initial_lowering_wire": initial_wire,
                        "native_owner_identity": identity_text(owner_identity),
                        "native_imported_reference_identity": identity_text(imported_reference["identity"]),
                        "native_owner_identities": owner_identities,
                        "native_imported_reference_identities": imported_reference_identities,
                        "resolution_stages": stage_reports,
                        "probe_requests": probe.requests,
                        "probe_digest": probe.digest.hexdigest(),
                        "readiness_seconds": round(readiness_seconds, 6),
                        "stage_budget": stage_budget,
                    }
                    persist_state(state)
                    return {
                        "schema_version": 1,
                        "status": "resolved",
                        "scope": "two-module imported nominal identities resolved and serialized as typed stage facts",
                        "stage0_revision": lock["revision"],
                        "compiler_source_sha256": compiler_digest,
                        "fixture_sha256": fixture_digest,
                        "owner_identity": identity_text(owner_identity),
                        "imported_reference_identity": identity_text(imported_reference["identity"]),
                        "facts_artifact": state_path.name,
                        **exported_facts(state),
                        "resolution_stages": stage_reports,
                        "readiness_seconds": round(readiness_seconds, 6),
                        "planning_seconds": 0.0,
                        "execution_seconds": round(time.monotonic() - started, 6),
                        "native_execution_steps_total": sum(item["steps"] or 0 for item in stage_reports),
                        "requests": probe.requests,
                        "retained_sessions": execution.get("retained_sessions", 0),
                    }
                finally:
                    probe.close()

        if not state_path.is_file():
            raise FileNotFoundError(f"resolved project facts are missing: {state_path}")
        state = json.loads(state_path.read_text())
        if state.get("compiler_checkout") != str(ROOT.resolve()):
            raise ValueError("resolved project facts belong to a different selected checkout")
        if state.get("compiler_source_sha256") != compiler_digest:
            raise ValueError("compiler source changed after project facts were saved")
        if state.get("fixture_sha256") != fixture_digest:
            raise ValueError("resolved project facts belong to a different source fixture")
        lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
        if state.get("stage0_revision") != lock["revision"]:
            raise ValueError("resolved project facts use a different pinned Stage-0 revision")

        progress(f"resuming-from-resolved-project-facts:{STAGE_MODE}")
        probe = Probe()
        stage_reports = []
        started = time.monotonic()
        try:
            progress("waiting-for-stage0-execution-status")
            execution = probe.send({"execution_status": True})
            readiness_seconds = time.monotonic() - started
            progress("stage0-probe-ready")
            root_input = state["text_units"]["sequence"]["values"][0]
            root_parsed = state["root_parsed_wire"]
            root_nominals = state["root_nominals_wire"]

            if STAGE_MODE == "signatures":
                imported_wire, _ = invoke_stage(
                    probe, "imported_signatures", STAGE_MODULES,
                    [state["text_units"], root_parsed, state["parsed_wire"], root_nominals, state["nominal_wire"]],
                    [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                )
                state["imported_signatures_wire"] = imported_wire
                if FIXTURE_KIND == "backend":
                    owner_imported_wire, _ = invoke_stage(
                        probe, "imported_signatures", STAGE_MODULES,
                        [state["text_units"], state["owner_parsed_wire"], state["parsed_wire"], state["owner_nominals_wire"], state["nominal_wire"]],
                        [NOMINAL_MODULE_BOUND, NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                    )
                    state["owner_imported_signatures_wire"] = owner_imported_wire
            elif STAGE_MODE == "proof":
                proof_wire, proof = invoke_stage(
                    probe, "prove_parsed_unit_with_nominals", STAGE_MODULES,
                    [root_input, wire_field(root_parsed, "unit"), state["imported_signatures_wire"], root_nominals],
                    [NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                )
                assert proof["ok"] is True, proof
                state["proof_wire"] = proof_wire
                if FIXTURE_KIND == "backend":
                    owner_proof_wire, owner_proof = invoke_stage(
                        probe, "prove_parsed_unit_with_nominals", STAGE_MODULES,
                        [state["owner_input"], wire_field(state["owner_parsed_wire"], "unit"), state["owner_imported_signatures_wire"], state["owner_nominals_wire"]],
                        [NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                    )
                    assert owner_proof["ok"] is True, owner_proof
                    state["owner_proof_wire"] = owner_proof_wire
            elif STAGE_MODE == "cfg":
                lowered_wire, lowered = invoke_stage(
                    probe, "lower_proven_unit", STAGE_MODULES,
                    [root_input, state["proof_wire"]], [NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                )
                assert lowered["valid"] is True, lowered
                state["flow_wire"] = lowered_wire
                if FIXTURE_KIND == "backend":
                    owner_lowered_wire, owner_lowered = invoke_stage(
                        probe, "lower_proven_unit", STAGE_MODULES,
                        [state["owner_input"], state["owner_proof_wire"]], [NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                    )
                    assert owner_lowered["valid"] is True, owner_lowered
                    state["owner_flow_wire"] = owner_lowered_wire
            elif STAGE_MODE == "ssa":
                ssa_wire, value_ssa = invoke_stage(
                    probe, "lower_value_ssa", STAGE_MODULES,
                    [root_input, state["flow_wire"], root_nominals], [NOMINAL_BYTE_BOUND],
                    stage_reports, stage_budget,
                )
                assert value_ssa["valid"] is True, value_ssa
                assert value_ssa["verified_function_count"] == ROOT_FUNCTION_COUNT, value_ssa
                expected_calls = ROOT_FUNCTION_COUNT
                assert value_ssa["call_count"] == expected_calls, value_ssa
                if FIXTURE_KIND == "backend":
                    owner_ssa_wire, owner_value_ssa = invoke_stage(
                        probe, "lower_value_ssa", STAGE_MODULES,
                        [state["owner_input"], state["owner_flow_wire"], state["owner_nominals_wire"]],
                        [NOMINAL_BYTE_BOUND], stage_reports, stage_budget,
                    )
                    assert owner_value_ssa["valid"] is True and owner_value_ssa["verified_function_count"] == 1, owner_value_ssa
                    assert owner_value_ssa["call_count"] == 0, owner_value_ssa
                    state["owner_ssa_wire"] = owner_ssa_wire
                    state["owner_ssa_summary"] = owner_value_ssa
                # Keep the complete verified SSA artifact so backend work consumes
                # these facts directly instead of lowering the source again.
                state["ssa_wire"] = ssa_wire
                state["ssa_summary"] = value_ssa
                native_callable_identity = identity_text(value_ssa["first_call_identity"])
                native_effect_identities = []
                native_capability_identities = []
                if FIXTURE_KIND == "effects":
                    call_instructions = [
                        instruction
                        for function in flist(value_ssa["functions"])
                        for block in flist(function["blocks"])
                        for instruction in flist(block["instructions"])
                        if instruction["kind"] == 3
                    ]
                    assert len(call_instructions) == ROOT_FUNCTION_COUNT, call_instructions
                    native_effect_identities = [
                        {
                            "identity": identity_text(effect["identity"]),
                            "capability_identity": identity_text(effect["capability_identity"]),
                        }
                        for effect in flist(call_instructions[0]["effects"])
                    ]
                    native_capability_identities = [
                        identity_text(capability["identity"])
                        for capability in flist(call_instructions[0]["capabilities"])
                    ]
                    assert native_effect_identities == [{
                        "identity": "mncs:0.2:effect:b::read_state",
                        "capability_identity": "mncs:0.2:capability:b::auth",
                    }], native_effect_identities
                    assert native_capability_identities == [
                        "mncs:0.2:capability:b::auth"
                    ], native_capability_identities
                progress("waiting-for-pinned-stage0-project-oracle")
                oracle = probe.send({"project_oracle": {
                    "root": NOMINAL_ROOT,
                    "modules": {"b": NOMINAL_TYPES},
                }})
                progress("pinned-stage0-project-oracle-returned")
                assert oracle["valid"] is True, oracle
                assert oracle["program"] is not None and oracle["ssa"] is not None
                stage0_callable = next(
                    function for function in oracle["program"]["functions"]
                    if function["home_module"] == "b" and function["name"] == "f"
                )
                assert native_callable_identity == stage0_callable["identity"], {
                    "native": native_callable_identity,
                    "stage0": stage0_callable,
                }
                stage0_type_identities = {
                    item["identity"]
                    for key in ("record_types", "finite_types")
                    for item in oracle["program"].get(key, [])
                }
                owner_type_identities = set(state.get(
                    "native_owner_identities", [state["native_owner_identity"]]
                ))
                if FIXTURE_KIND == "backend":
                    assert owner_type_identities.issubset(stage0_type_identities), {
                        "native_owner": sorted(owner_type_identities),
                        "stage0_types": sorted(stage0_type_identities),
                    }
                else:
                    assert owner_type_identities == stage0_type_identities, {
                        "native_owner": sorted(owner_type_identities),
                        "stage0_types": sorted(stage0_type_identities),
                    }
                assert set(state.get("native_imported_reference_identities", [state["native_imported_reference_identity"]])).issubset(
                    stage0_type_identities
                ), state.get("native_imported_reference_identities", [state["native_imported_reference_identity"]])

            stage_names = {item["stage"] for item in stage_reports}
            state["lowering_stages"] = [
                item for item in state.get("lowering_stages", [])
                if item["stage"] not in stage_names
            ] + stage_reports
            run_record = {
                "mode": STAGE_MODE,
                "readiness_seconds": round(readiness_seconds, 6),
                "execution_seconds": round(time.monotonic() - started, 6),
                "requests": probe.requests,
                "probe_digest": probe.digest.hexdigest(),
                "retained_sessions": execution.get("retained_sessions", 0),
            }
            state["stage_runs"] = [
                item for item in state.get("stage_runs", [])
                if item["mode"] != STAGE_MODE
            ] + [run_record]
            state["probe_requests_total"] = state.get("probe_requests", 0) + sum(
                item["requests"] for item in state["stage_runs"]
            )
            persist_state(state)

            if STAGE_MODE == "ssa":
                combined_stages = state["resolution_stages"] + state["lowering_stages"]
                facts_bytes = state_path.read_bytes()
                facts_sha256 = hashlib.sha256(facts_bytes).hexdigest()
                return {
                    "schema_version": 1,
                    "status": "verified",
                    "stage0_revision": state["stage0_revision"],
                    "compiler_source_sha256": compiler_digest,
                    "test_harness_sha256": test_harness_digest,
                    "fixture_sha256": fixture_digest,
                    "scope": f"root module imported {FIXTURE_KIND} calls through proof, typed CFG, and verified value SSA using persisted parsed and resolved facts",
                    "native_nominal_identity": state["native_imported_reference_identity"],
                    "native_owner_identity": state["native_owner_identity"],
                    "native_imported_callable_identity": native_callable_identity,
                    "stage0_callable_identity": stage0_callable["identity"],
                    "native_proof_ok": True,
                    "native_cfg_valid": True,
                    "native_value_ssa_valid": value_ssa["valid"],
                    "native_verified_functions": value_ssa["verified_function_count"],
                    "native_call_count": value_ssa["call_count"],
                    "native_imported_effect_identities": native_effect_identities,
                    "native_imported_capability_identities": native_capability_identities,
                    "stage0_valid": oracle["valid"],
                    "stage0_linked_functions": len(oracle["program"]["functions"]),
                    "stage0_ssa_functions": len(oracle["ssa"]["functions"]),
                    "resolution_stages": state["resolution_stages"],
                    "lowering_stages": state["lowering_stages"],
                    "stage_runs": state["stage_runs"],
                    "planning_seconds": 0.0,
                    "execution_seconds": round(sum(item["execution_seconds"] for item in state["stage_runs"]), 6),
                    "native_execution_steps_total": sum(item["steps"] or 0 for item in combined_stages),
                    "requests": state["probe_requests_total"],
                    "retained_sessions": [item["retained_sessions"] for item in state["stage_runs"]],
                    "facts_artifact": state_path.name,
                    "facts_evidence": {
                        "reference": (Path("compiler-facts") / state_path.name).as_posix(),
                        "identity": f"sha256:{facts_sha256}",
                        "sha256": facts_sha256,
                        "bytes": len(facts_bytes),
                        "retention": ("session-artifact-directory"
                                      if os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
                                      else "ignored-repository-build-directory"),
                    },
                    **exported_facts(state),
                    "coverage": [
                        "declaring-module ownership for every imported nominal identity in the owner module",
                        "imported finite identity" if FIXTURE_KIND == "finite" else ("nested imported nominal field identity" if FIXTURE_KIND == "nested" else ("imported record identity" if FIXTURE_KIND != "effects" else "imported effect and capability identities retained on the verified call instruction")),
                        "resolved imported callable identity",
                        "parsed and resolved facts consumed from the saved project artifact",
                        "proof, CFG, and value SSA consume imported nominal facts without reparsing or re-resolving the project",
                    ],
                }
            return {
                "schema_version": 1,
                "status": "stage-complete",
                "mode": STAGE_MODE,
                "scope": "one native project pipeline stage consumed persisted upstream facts",
                "stage0_revision": state["stage0_revision"],
                "compiler_source_sha256": compiler_digest,
                "facts_artifact": state_path.name,
                **exported_facts(state),
                "stages": stage_reports,
                "readiness_seconds": round(readiness_seconds, 6),
                "planning_seconds": 0.0,
                "execution_seconds": round(time.monotonic() - started, 6),
                "native_execution_steps_total": sum(item["steps"] or 0 for stage in state.get("resolution_stages", []) + state.get("lowering_stages", []) for item in [stage]),
                "requests": probe.requests,
                "retained_sessions": execution.get("retained_sessions", 0),
            }
        finally:
            probe.close()
    except StageBudgetFailure as failure:
        report = {
            "schema_version": 1,
            "status": failure.response.get("status", "unknown"),
            "failed_stage": failure.stage,
            "stage_budget": stage_budget,
            "response": failure.response,
            "scope": "bounded staged native project cost diagnostic; no parity claim",
        }
        session_artifact_directory = campaign_artifact_directory()
        failed_probe = session_artifact_directory / "compiler-facts" / (
            f"campaign-{CAMPAIGN_ID}-imported-nominal-budget-probe.json"
        )
        failed_probe.parent.mkdir(parents=True, exist_ok=True)
        failed_probe.write_text(json.dumps(report, indent=2) + "\n")
        return report

class StageBudgetFailure(Exception):
    def __init__(self, stage: str, response: dict) -> None:
        self.stage = stage
        self.response = response


if __name__ == "__main__":
    if STAGE_MODE == "all":
        for mode in ("resolve", "signatures", "proof", "cfg", "ssa"):
            child_env = os.environ.copy()
            child_env["MNCS_IMPORTED_NOMINAL_MODE"] = mode
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=ROOT,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if completed.returncode != 0:
                sys.stderr.write(completed.stdout)
                sys.stderr.write(completed.stderr)
                raise SystemExit(completed.returncode)
        report = json.loads(
            (ROOT / "evidence" / f"campaign-{CAMPAIGN_ID}-imported-nominal-ssa.json").read_text()
        )
    else:
        report = run()
        session_artifact_directory = campaign_artifact_directory()
        if (STAGE_MODE == "native-backend"
                and isinstance(report.get("native_backend_artifact"), dict)):
            # Keep the full reproducible dump in the owning session artifact
            # area; commit only its identity, hash, and compact result summary.
            full_artifact = report.pop("native_backend_artifact")
            relative_reference = (
                Path("compiler-facts")
                / f"campaign-{CAMPAIGN_ID}-native-backend-full-evidence.json"
            )
            artifact_path = Path(session_artifact_directory) / relative_reference
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_bytes = (json.dumps(full_artifact, indent=2) + "\n").encode()
            artifact_path.write_bytes(artifact_bytes)
            artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
            report["full_evidence"] = {
                "reference": relative_reference.as_posix(),
                "identity": f"sha256:{artifact_sha256}",
                "sha256": artifact_sha256,
                "bytes": len(artifact_bytes),
                "retention": ("session-artifact-directory"
                              if os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
                              else "ignored-repository-build-directory"),
            }
        if STAGE_MODE in {"resolve", "signatures", "proof", "cfg"}:
            artifact_root = session_artifact_directory
            output = artifact_root / "compiler-facts" / (
                f"campaign-{CAMPAIGN_ID}-imported-nominal-{STAGE_MODE}.json"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
        elif STAGE_MODE == "ssa":
            output = ROOT / "evidence" / f"campaign-{CAMPAIGN_ID}-imported-nominal-ssa.json"
        elif STAGE_MODE == "native-backend":
            output = ROOT / "evidence" / f"campaign-{CAMPAIGN_ID}-native-backend-vertical.json"
        else:
            output = ROOT / "evidence" / f"campaign-{CAMPAIGN_ID}-imported-nominal-{STAGE_MODE}.json"
        if os.environ.get("MNCS_CAMPAIGN_ARTIFACT_TRANSPORT") != "stdout":
            output.write_text(json.dumps(report, indent=2) + "\n")
    if os.environ.get("MNCS_CAMPAIGN_ARTIFACT_TRANSPORT") == "stdout":
        print("MNCS_CAMPAIGN_REPORT=" + json.dumps(report, separators=(",", ":")))
    else:
        print(json.dumps(report, indent=2))
