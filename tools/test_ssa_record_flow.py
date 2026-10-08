#!/usr/bin/env python3
"""Verify SSA source spans for nested operations and nominal values across branches and loops."""
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

BRANCH_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "record LineCol { line: u64, col: u64 } "
    "fn branch_return(value: u64, st: LineCol) -> (r: LineCol) { "
    "if value == 1 { return LineCol { line: st.line + 1, col: 1 }; } "
    "return st; }"
)

LOOP_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "record LineCol { line: u64, col: u64 } "
    "fn loop_carry<N: Nat>(source: [byte; up_to N]) -> (r: LineCol) { "
    "iterate i over source carrying st: LineCol = LineCol { line: 1, col: 1 } { "
    "next st = st; } return st; }"
)

LENGTH_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn length_plus<N: Nat>(source: [byte; up_to N]) -> (r: u64) { "
    "return source.len + 1; }"
)

SOURCE_LOOP_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "record LineCol { line: u64, col: u64 } "
    "fn byte_at<N: Nat>(source: [byte; up_to N], offset: u64) -> (r: u64) { "
    "if offset < source.len { return source[offset] as u64; } return 256; } "
    "fn line_step<N: Nat>(source: [byte; up_to N], offset: u64, pos: u64, st: LineCol) -> (r: LineCol) { "
    "if pos >= offset { return st; } "
    "if byte_at<N>(source, pos) == 10 { return LineCol { line: st.line + 1, col: 1 }; } "
    "return LineCol { line: st.line, col: st.col + 1 }; } "
    "fn line_col<N: Nat>(source: [byte; up_to N], offset: u64) -> (r: LineCol) { "
    "iterate i over source carrying st: LineCol = LineCol { line: 1, col: 1 } { "
    "next st = line_step<N>(source, offset, i, st); } return st; }"
)

SOURCES = [
    {"id": "nominal-branch-return", "text": BRANCH_SOURCE},
    {"id": "nominal-loop-carry", "text": LOOP_SOURCE},
    {"id": "nested-sequence-length", "text": LENGTH_SOURCE},
    {"id": "source-loop-nominal-call", "text": SOURCE_LOOP_SOURCE},
]


def instruction_kind(instruction):
    return field(instruction, "kind")["integer"]["value"]


def mutate_integer(record, key, value):
    for name, field_value in record["record"]["fields"]:
        if name == key:
            field_value["integer"]["value"] = value
            return
    raise AssertionError(f"record has no {key}")


def suite():
    sources = list(SOURCES)
    focus_source = os.environ.get("MNCS_FOCUS_SOURCE")
    if focus_source:
        sources = [item for item in sources if item["id"] == focus_source]
        assert sources, f"unknown focused source: {focus_source}"
    for item in sources:
        item["raw"] = item["text"].encode()
        item["args"] = module_args(item["raw"])

    flow = Probe("mncs.compiler.flow.v1", ["lower_unit"], backend="cranelift")
    for item in sources:
        flow_result = call(flow, "mncs.compiler.flow.v1", "lower_unit", item["args"])
        item["flow_wire"] = flow_result["returned"][0]
        item["proof"] = field(item["flow_wire"], "proof")
        assert field(item["proof"], "ok")["boolean"]["value"] is True, item["id"]
        item["unit_wire"] = field(item["proof"], "unit")
        item["oracle_ssa"] = flow.send({"ssa": item["text"]})
        assert item["oracle_ssa"].get("ssa") is not None and not item["oracle_ssa"].get("diagnostics"), item["id"]
    flow_stderr = flow.close()

    decl = Probe("mncs.compiler.decl.v1", ["local_nominal_type_identities"], backend="cranelift")
    for item in sources:
        item["nominal_wire"] = call(
            decl, "mncs.compiler.decl.v1", "local_nominal_type_identities",
            [*item["args"], item["unit_wire"]],
        )["returned"][0]
    decl_stderr = decl.close()

    ssa = Probe(SSA_MODULE, [
        "lower_value_ssa", "verify_function", "verify_one_block",
        "verify_parameters", "verify_instructions", "verify_terminator",
        "verify_instruction", "verify_call_instruction", "call_argument_types_match",
        "available_values", "bind_count", "id_count", "supported_value_type",
    ], backend="cranelift")
    for item in sources:
        lowered = call(
            ssa, SSA_MODULE, "lower_value_ssa",
            [*item["args"], item["flow_wire"], item["nominal_wire"]],
        )
        item["native_wire"] = lowered["returned"][0]
        item["native"] = decode(item["native_wire"])
        if item["id"] == "source-loop-nominal-call":
            assert item["native"]["supported_function_count"] == 3, (item["id"], item["native"])
            assert item["native"]["verified_function_count"] == 3, (item["id"], item["native"])
        else:
            assert item["native"]["valid"], (item["id"], item["native"])
        item["functions"] = wire_linked(field(item["native_wire"], "functions"))
        item["function_status"] = linked(item["native"]["functions"])
        assert item["native"]["function_count"] == len(item["function_status"]), item["id"]
        assert item["native"]["supported_function_count"] == len(item["function_status"]), item["id"]
        for index, function in enumerate(item["functions"]):
            if item["id"] == "source-loop-nominal-call":
                continue
            result = call(ssa, SSA_MODULE, "verify_function", [*item["args"], function])
            expected = item["function_status"][index]["verified"]
            assert result["returned"] == [{"boolean": {"value": expected}}], (item["id"], index, result)
        if item["id"] == "source-loop-nominal-call":
            call_instruction = next(
                instruction
                for block in wire_linked(field(item["functions"][1], "blocks"))
                for instruction in wire_linked(field(block, "instructions"))
                if instruction_kind(instruction) == 3
            )
            assert len(wire_linked(field(call_instruction, "inputs"))) == 2
        if item["id"] == "nominal-branch-return":
            block_wires = wire_linked(field(item["functions"][0], "blocks"))
            item["block_verifier"] = [
                call(ssa, SSA_MODULE, "verify_one_block", [*item["args"], item["functions"][0], block])["returned"][0]["boolean"]["value"]
                for block in block_wires
            ]
            projection_block, projection_instruction = next(
                (block, instruction)
                for block in block_wires
                for instruction in wire_linked(field(block, "instructions"))
                if instruction_kind(instruction) == 8
            )
            projection_check = call(ssa, SSA_MODULE, "verify_instruction", [
                *item["args"], item["functions"][0], field(projection_block, "id"),
                projection_instruction, field(projection_instruction, "sequence"),
            ])
            item["first_projection_verifier"] = {
                "kind": instruction_kind(projection_instruction),
                "accepted": projection_check["returned"][0]["boolean"]["value"],
                "steps": projection_check.get("steps"),
            }
            item["failed_block_checks"] = []
            for index in [i for i, passed in enumerate(item["block_verifier"]) if not passed]:
                block = block_wires[index]
                block_id = field(block, "id")["integer"]["value"]
                instructions = field(block, "instructions")
                terminator = field(block, "terminator")
                instruction_wires = wire_linked(instructions)
                instruction_summaries = [{
                    "sequence": field(instruction, "sequence")["integer"]["value"],
                    "id": field(instruction, "id")["integer"]["value"],
                    "kind": instruction_kind(instruction),
                    "result": field(instruction, "result")["integer"]["value"],
                    "type": decode(field(instruction, "ty")),
                    "inputs": decode(field(instruction, "inputs")),
                    "parameters": decode(field(instruction, "params")),
                    "binding_span": [
                        field(instruction, "binding_start")["integer"]["value"],
                        field(instruction, "binding_end")["integer"]["value"],
                    ],
                    "source_span": [
                        field(instruction, "start")["integer"]["value"],
                        field(instruction, "end")["integer"]["value"],
                    ],
                } for instruction in instruction_wires]
                item["failed_block_checks"].append({
                    "index": index,
                    "id": block_id,
                    "parameters_ok": call(ssa, SSA_MODULE, "verify_parameters", [*item["args"], item["functions"][0], field(block, "id"), field(block, "parameters")])["returned"][0]["boolean"]["value"],
                    "instructions_ok": call(ssa, SSA_MODULE, "verify_instructions", [*item["args"], item["functions"][0], block, instructions])["returned"][0]["boolean"]["value"],
                    "terminator_ok": call(ssa, SSA_MODULE, "verify_terminator", [*item["args"], item["functions"][0], block, terminator, instructions])["returned"][0]["boolean"]["value"],
                    "instructions": instruction_summaries,
                })
            assert item["first_projection_verifier"]["accepted"], item["first_projection_verifier"]
            changed_function = json.loads(json.dumps(item["functions"][0]))
            first_project = next(
                instruction
                for block in wire_linked(field(changed_function, "blocks"))
                for instruction in wire_linked(field(block, "instructions"))
                if instruction_kind(instruction) == 8
            )
            mutate_integer(first_project, "end", field(first_project, "end")["integer"]["value"] + 1)
            rejected = call(ssa, SSA_MODULE, "verify_function", [*item["args"], changed_function])
            assert rejected["returned"] == [{"boolean": {"value": False}}], rejected
            item["tampered_project_span_rejected"] = True
        if item["id"] == "source-loop-nominal-call":
            item["failed_function_checks"] = []
            for function_index, function in enumerate(item["functions"]):
                if item["function_status"][function_index]["verified"]:
                    continue
                function_blocks = wire_linked(field(function, "blocks"))
                failed_blocks = []
                for block in function_blocks:
                    block_id = field(block, "id")["integer"]["value"]
                    instructions = wire_linked(field(block, "instructions"))
                    instruction_checks = [
                        call(ssa, SSA_MODULE, "verify_instruction", [
                            *item["args"], function, field(block, "id"), instruction,
                            field(instruction, "sequence"),
                        ])["returned"][0]["boolean"]["value"]
                        for instruction in instructions
                    ]
                    block_check = {
                        "block_id": block_id,
                        "parameters_ok": call(ssa, SSA_MODULE, "verify_parameters", [
                            *item["args"], function, field(block, "id"), field(block, "parameters"),
                        ])["returned"][0]["boolean"]["value"],
                        "instructions_ok": call(ssa, SSA_MODULE, "verify_instructions", [
                            *item["args"], function, block, field(block, "instructions"),
                        ])["returned"][0]["boolean"]["value"],
                        "terminator_ok": call(ssa, SSA_MODULE, "verify_terminator", [
                            *item["args"], function, block, field(block, "terminator"), field(block, "instructions"),
                        ])["returned"][0]["boolean"]["value"],
                        "instruction_checks": instruction_checks,
                        "instruction_details": [{
                            "sequence": field(instruction, "sequence")["integer"]["value"],
                            "kind": instruction_kind(instruction),
                            "operator": field(instruction, "operator")["integer"]["value"],
                            "result": field(instruction, "result")["integer"]["value"],
                            "argc": field(instruction, "argc")["integer"]["value"],
                            "inputs": [
                                field(argument, "value")["integer"]["value"]
                                for argument in wire_linked(field(instruction, "inputs"))
                            ],
                            "binding_span": [
                                field(instruction, "binding_start")["integer"]["value"],
                                field(instruction, "binding_end")["integer"]["value"],
                            ],
                            "source_span": [
                                field(instruction, "start")["integer"]["value"],
                                field(instruction, "end")["integer"]["value"],
                            ],
                        } for instruction in instructions],
                    }
                    block_check["call_details"] = []
                    for instruction in instructions:
                        if instruction_kind(instruction) != 3:
                            continue
                        value_types = {
                            field(value, "id")["integer"]["value"]: decode(field(value, "ty"))
                            for value in wire_linked(field(function, "values"))
                        }
                        argument_ids = [
                            field(argument, "value")["integer"]["value"]
                            for argument in wire_linked(field(instruction, "inputs"))
                        ]
                        parameters = wire_linked(field(instruction, "params"))
                        parameter_types = [decode(field(parameter, "ty")) for parameter in parameters]
                        block_check["call_details"].append({
                            "sequence": field(instruction, "sequence")["integer"]["value"],
                            "verify_call_instruction": call(ssa, SSA_MODULE, "verify_call_instruction", [
                                *item["args"], function, instruction,
                            ])["returned"][0]["boolean"]["value"],
                            "argument_types_match": call(ssa, SSA_MODULE, "call_argument_types_match", [
                                *item["args"], field(function, "values"), field(instruction, "inputs"), field(instruction, "params"),
                            ])["returned"][0]["boolean"]["value"],
                            "available_values": call(ssa, SSA_MODULE, "available_values", [
                                *item["args"], field(function, "values"), field(instruction, "inputs"), field(block, "id"), field(instruction, "sequence"),
                            ])["returned"][0]["boolean"]["value"],
                            "operand_count": call(ssa, SSA_MODULE, "id_count", [
                                *item["args"], field(instruction, "inputs"),
                            ])["returned"][0]["integer"]["value"],
                            "parameter_count": call(ssa, SSA_MODULE, "bind_count", [
                                *item["args"], field(instruction, "params"),
                            ])["returned"][0]["integer"]["value"],
                            "result_supported": call(ssa, SSA_MODULE, "supported_value_type", [
                                *item["args"], field(instruction, "ty"), field(function, "nominals"),
                            ])["returned"][0]["boolean"]["value"],
                            "identity_valid": field(field(instruction, "identity"), "valid")["boolean"]["value"],
                            "identity_length": field(field(instruction, "identity"), "len")["integer"]["value"],
                            "argument_types": [value_types.get(value_id) for value_id in argument_ids],
                            "parameter_types": parameter_types,
                        })
                    if not block_check["parameters_ok"] or not block_check["instructions_ok"] or not block_check["terminator_ok"]:
                        failed_blocks.append(block_check)
                item["failed_function_checks"].append({
                    "function_index": function_index,
                    "verified": False,
                    "failed_blocks": failed_blocks,
                })
        if item["id"] == "nested-sequence-length":
            changed_function = json.loads(json.dumps(item["functions"][0]))
            length_instruction = next(
                instruction
                for block in wire_linked(field(changed_function, "blocks"))
                for instruction in wire_linked(field(block, "instructions"))
                if instruction_kind(instruction) == 10
            )
            mutate_integer(length_instruction, "end", field(length_instruction, "end")["integer"]["value"] + 1)
            rejected = call(ssa, SSA_MODULE, "verify_function", [*item["args"], changed_function])
            assert rejected["returned"] == [{"boolean": {"value": False}}], rejected
            item["tampered_length_span_rejected"] = True
        item["record_constructions"] = sum(
            instruction_kind(instruction) == 16
            for function in item["functions"]
            for block in wire_linked(field(function, "blocks"))
            for instruction in wire_linked(field(block, "instructions"))
        )
        if item["id"] != "nested-sequence-length":
            assert item["record_constructions"] >= 1, item["id"]

    artifact = ssa.send({"emit_vm_artifact": {"module": SSA_MODULE}})["artifact"]
    ssa_stderr = ssa.close()
    cases = [{
        "id": item["id"],
        "function": "lower_value_ssa",
        "args": [*item["args"], item["flow_wire"], item["nominal_wire"]],
        "type_args": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    } for item in sources]
    with tempfile.TemporaryDirectory(prefix="mncs-ssa-record-flow-") as directory:
        folder = Path(directory)
        artifact_path = folder / "ssa.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(artifact_path, cases, folder, module=SSA_MODULE)

    results = []
    for item, vm_result in zip(sources, vm_doc["results"]):
        outcome = vm_result["outcome"]
        vm_equal = None
        if outcome == {"kind": "completed"}:
            vm_summary = decode(vm_to_wire(vm_result["record"]["returned"][0]))
            vm_equal = vm_summary == item["native"]
            assert vm_equal, item["id"]
        else:
            assert outcome.get("dimension") == "steps" and outcome.get("kind") == "budget_exhausted", vm_result
        results.append({
            "id": item["id"],
            "source_sha256": hashlib.sha256(item["raw"]).hexdigest(),
            "stage0_reference_accepts": True,
            "native_summary": {key: item["native"][key] for key in (
                "valid", "function_count", "supported_function_count", "verified_function_count",
                "instruction_count", "block_count", "unsupported_block_count",
            )},
            "function_status": [
                {"supported": function["supported"], "verified": function["verified"]}
                for function in item["function_status"]
            ],
            "record_constructor_instructions": item["record_constructions"],
            "block_verifier": item.get("block_verifier"),
            "failed_block_checks": item.get("failed_block_checks"),
            "failed_function_checks": item.get("failed_function_checks"),
            "first_projection_verifier": item.get("first_projection_verifier"),
            "tampered_project_span_rejected": item.get("tampered_project_span_rejected"),
            "tampered_length_span_rejected": item.get("tampered_length_span_rejected"),
            "canonical_vm_outcome": outcome,
            "canonical_vm_steps": vm_result["record"]["usage"]["steps"],
            "canonical_vm_native_equal": vm_equal,
        })

    return {
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "step_budget": STEP_BUDGET,
        "artifact_id": artifact["artifact_id"],
        "vm_peak_rss_kb": vm_rss,
        "vm_wall_seconds": round(vm_wall, 6),
        "cases": results,
        "cache_observations": [
            line for stderr in (flow_stderr, decl_stderr, ssa_stderr)
            for line in stderr.splitlines() if "mncs-stage0-probe" in line
        ],
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    started = time.monotonic()
    result = {**suite(), "elapsed_seconds": round(time.monotonic() - started, 3)}
    output = ROOT / ".build" / "ssa-record-flow-results.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
