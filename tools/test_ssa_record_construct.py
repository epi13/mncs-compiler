#!/usr/bin/env python3
"""Differential and executor proof for verified SSA record construction."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from test_ssa_sequence_length import (
    ROOT, SSA_MODULE, TYPE_ARGS, STEP_BUDGET, Probe, call, decode, field,
    linked, module_args, run_batch, vm_to_wire, wire_linked,
)

SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "record Scan { end: u64, depth: u64, dotted: bool, done: bool } "
    "record Scanned { amount: u64, state: Scan } "
    "fn scan_step(st: Scan, amount: u64, block: bool) -> (r: Scan) { "
    "if block { return Scan { ..st, end: st.end + amount, depth: st.depth + 1 }; } "
    "return Scan { ..st, end: st.end + 1 }; } "
    "fn wrap(st: Scan, amount: u64) -> (r: Scanned) { "
    "return Scanned { state: Scan { ..st, end: st.end + amount, done: true }, amount: amount }; }"
)


def instruction_kind(instruction):
    return field(instruction, "kind")["integer"]["value"]


def mutate_field(instruction, key, transform):
    for name, value in instruction["record"]["fields"]:
        if name == key:
            transform(value)
            return
    raise AssertionError(f"instruction has no {key}")


def suite():
    raw = SOURCE.encode()
    args = module_args(raw)

    flow = Probe("mncs.compiler.flow.v1", ["lower_unit"], backend="cranelift")
    flow_result = call(flow, "mncs.compiler.flow.v1", "lower_unit", args)
    flow_wire = flow_result["returned"][0]
    proof = field(flow_wire, "proof")
    assert field(proof, "ok")["boolean"]["value"] is True, proof
    oracle = flow.send({"ssa": SOURCE})
    assert oracle.get("ssa") is not None and not oracle.get("diagnostics"), oracle
    unit_wire = field(proof, "unit")
    flow_stderr = flow.close()

    decl = Probe("mncs.compiler.decl.v1", ["local_nominal_type_identities"], backend="cranelift")
    nominal_wire = call(
        decl, "mncs.compiler.decl.v1", "local_nominal_type_identities", [*args, unit_wire]
    )["returned"][0]
    decl_stderr = decl.close()

    ssa = Probe(SSA_MODULE, ["lower_value_ssa", "verify_function"], backend="cranelift")
    lowered = call(ssa, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nominal_wire])
    native_wire = lowered["returned"][0]
    native = decode(native_wire)
    functions = wire_linked(field(native_wire, "functions"))
    native_functions = linked(native["functions"])
    native_status = {key: native[key] for key in (
        "valid", "function_count", "supported_function_count", "verified_function_count",
        "first_unsupported_function", "first_unsupported_kind", "unsupported_block_count",
    )}
    assert native["valid"] and native["function_count"] == 2, native_status
    assert native["supported_function_count"] == native["verified_function_count"] == 2, native_status

    record_instructions = []
    record_updates = []
    projection_instructions = []
    for function in functions:
        verified = call(ssa, SSA_MODULE, "verify_function", [*args, function])
        assert verified["returned"] == [{"boolean": {"value": True}}], verified
        for block in wire_linked(field(function, "blocks")):
            for instruction in wire_linked(field(block, "instructions")):
                if instruction_kind(instruction) == 16:
                    if field(instruction, "operator")["integer"]["value"] == 1:
                        record_updates.append(instruction)
                    else:
                        record_instructions.append(instruction)
                elif instruction_kind(instruction) == 8:
                    projection_instructions.append(instruction)
    assert len(record_instructions) == 1, len(record_instructions)
    assert len(record_updates) == 3, len(record_updates)
    assert projection_instructions, "constructed record projection was not retained"

    changed_span = json.loads(json.dumps(functions[1]))
    changed_record = next(
        instruction
        for block in wire_linked(field(changed_span, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if instruction_kind(instruction) == 16
        and field(instruction, "operator")["integer"]["value"] == 0
    )
    mutate_field(changed_record, "binding_start", lambda value: value["integer"].__setitem__("value", value["integer"]["value"] + 1))
    rejected_span = call(ssa, SSA_MODULE, "verify_function", [*args, changed_span])
    assert rejected_span["returned"] == [{"boolean": {"value": False}}], rejected_span

    changed_fields = json.loads(json.dumps(functions[0]))
    changed_record = next(
        instruction
        for block in wire_linked(field(changed_fields, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if instruction_kind(instruction) == 16
        and field(instruction, "operator")["integer"]["value"] == 1
    )
    params = field(changed_record, "params")
    first_param = params["finite"]["payload"][0][1]
    mutate_field(first_param, "start", lambda value: value["integer"].__setitem__("value", value["integer"]["value"] + 4))
    rejected_field = call(ssa, SSA_MODULE, "verify_function", [*args, changed_fields])
    assert rejected_field["returned"] == [{"boolean": {"value": False}}], rejected_field

    changed_update_span = json.loads(json.dumps(functions[0]))
    changed_update = next(
        instruction
        for block in wire_linked(field(changed_update_span, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if instruction_kind(instruction) == 16
        and field(instruction, "operator")["integer"]["value"] == 1
    )
    mutate_field(changed_update, "binding_start", lambda value: value["integer"].__setitem__("value", value["integer"]["value"] + 1))
    rejected_update_span = call(ssa, SSA_MODULE, "verify_function", [*args, changed_update_span])
    assert rejected_update_span["returned"] == [{"boolean": {"value": False}}], rejected_update_span

    changed_update_base = json.loads(json.dumps(functions[0]))
    changed_update = next(
        instruction
        for block in wire_linked(field(changed_update_base, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if instruction_kind(instruction) == 16
        and field(instruction, "operator")["integer"]["value"] == 1
    )
    update_inputs = field(changed_update, "inputs")["finite"]["payload"]
    assert len(update_inputs) == 2, update_inputs
    mutate_field(update_inputs[0][1], "value", lambda value: value["integer"].__setitem__("value", 1))
    rejected_update_base = call(ssa, SSA_MODULE, "verify_function", [*args, changed_update_base])
    assert rejected_update_base["returned"] == [{"boolean": {"value": False}}], rejected_update_base

    artifact = ssa.send({"emit_vm_artifact": {"module": SSA_MODULE}})["artifact"]
    ssa_stderr = ssa.close()
    cases = [
        {
            "id": "ssa-record-update-verify",
            "function": "verify_function",
            "args": [*args, functions[0]],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        },
        {
            "id": "ssa-record-update-reject-wrong-base",
            "function": "verify_function",
            "args": [*args, changed_update_base],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        },
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-ssa-record-construct-") as directory:
        folder = Path(directory)
        artifact_path = folder / "ssa.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(artifact_path, cases, folder, module=SSA_MODULE)
    vm_results = {result["id"]: result for result in vm_doc["results"]}
    verified_update = vm_results["ssa-record-update-verify"]
    rejected_bad_base = vm_results["ssa-record-update-reject-wrong-base"]
    assert verified_update["outcome"] == rejected_bad_base["outcome"] == {"kind": "completed"}, vm_results
    assert decode(vm_to_wire(verified_update["record"]["returned"][0])) is True, verified_update
    assert decode(vm_to_wire(rejected_bad_base["record"]["returned"][0])) is False, rejected_bad_base

    return {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "stage0_ssa_accepts": True,
        "stage0_function_count": len(oracle["ssa"].get("functions", [])),
        "native_summary": {key: native[key] for key in (
            "valid", "function_count", "supported_function_count", "verified_function_count",
            "instruction_count", "block_count", "unsupported_block_count",
        )},
        "record_construct_instructions": len(record_instructions),
        "record_update_instructions": len(record_updates),
        "record_projection_instructions": len(projection_instructions),
        "verifier_rejects_changed_type_span": True,
        "verifier_rejects_changed_field_span": True,
        "verifier_rejects_changed_update_span": True,
        "verifier_rejects_wrong_update_base_type": True,
        "canonical_vm_cranelift_verifier_agreement": True,
        "vm_artifact_id": artifact["artifact_id"],
        "vm_steps": {
            key: result["record"]["usage"]["steps"]
            for key, result in vm_results.items()
        },
        "vm_peak_rss_kb": vm_rss,
        "vm_wall_seconds": round(vm_wall, 6),
        "cache_observations": [
            line for stderr in (flow_stderr, decl_stderr, ssa_stderr)
            for line in stderr.splitlines() if "mncs-stage0-probe" in line
        ],
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    started = time.monotonic()
    print(json.dumps({**suite(), "elapsed_seconds": round(time.monotonic() - started, 3)}, indent=2))
