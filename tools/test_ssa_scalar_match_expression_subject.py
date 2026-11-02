#!/usr/bin/env python3
"""Differential native proof and verified SSA for computed scalar-match subjects."""

import hashlib
import json
from pathlib import Path

from test_ssa_sequence_length import (
    ROOT,
    SSA_MODULE,
    Probe,
    call,
    decode,
    field,
    module_args,
    wire_linked,
)


SOURCE = (
    "mncs 0.19; module frontier.scalar_match_subjects; "
    "fn classify(value: u64) -> (r: u64) { return match value { 0 => 10, _ => 20 }; } "
    "fn selected(flag: bool, value: u64) -> (r: u64) { "
    "return match select(flag, value, 0) { 0 => 10, _ => 20 }; } "
    "fn computed(value: u64) -> (r: u64) { "
    "return match value + 1 { 0 => 10, _ => 20 }; } "
    "fn called(value: u64) -> (r: u64) { "
    "return match classify(value) { 0 => 10, _ => 20 }; }"
)


def run():
    raw = SOURCE.encode()
    args = module_args(raw)

    flow = Probe(
        "mncs.compiler.flow.v1",
        ["lower_unit"],
        backend="cranelift",
        backend_roots=["lower_unit"],
    )
    try:
        flow_result = call(flow, "mncs.compiler.flow.v1", "lower_unit", args)
        flow_wire = flow_result["returned"][0]
        proof = field(flow_wire, "proof")
        assert field(proof, "ok")["boolean"]["value"] is True, proof
        oracle = flow.send({"ssa": SOURCE})
        assert oracle.get("ssa") is not None and not oracle.get("diagnostics"), oracle
        unit_wire = field(proof, "unit")
    finally:
        flow.close()

    decl = Probe(
        "mncs.compiler.decl.v1",
        ["local_nominal_type_identities"],
        backend="cranelift",
        backend_roots=["local_nominal_type_identities"],
    )
    try:
        nominal_wire = call(
            decl,
            "mncs.compiler.decl.v1",
            "local_nominal_type_identities",
            [*args, unit_wire],
        )["returned"][0]
    finally:
        decl.close()

    ssa = Probe(
        SSA_MODULE,
        ["lower_value_ssa", "verify_function"],
        backend="cranelift",
        backend_roots=["lower_value_ssa", "verify_function"],
    )
    try:
        lowered = call(ssa, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nominal_wire])
        native_wire = lowered["returned"][0]
        native = decode(native_wire)
        functions = wire_linked(field(native_wire, "functions"))
        assert native["valid"] is True, native
        assert native["function_count"] == 4, native
        assert native["supported_function_count"] == native["verified_function_count"] == 4, native

        switches = []
        operation_slots = []
        for function in functions:
            verified = call(ssa, SSA_MODULE, "verify_function", [*args, function])
            assert verified["returned"] == [{"boolean": {"value": True}}], verified
            switch_blocks = [
                block
                for block in wire_linked(field(function, "blocks"))
                if field(block, "terminator")["finite"]["discriminant"] == 5
            ]
            switches.extend(switch_blocks)
            operation_slots.extend(
                field(instruction, "operator")["integer"]["value"]
                for block in wire_linked(field(function, "blocks"))
                for instruction in wire_linked(field(block, "instructions"))
                if field(instruction, "kind")["integer"]["value"] == 9
            )

        assert len(switches) == 4, len(switches)
        assert len(oracle["ssa"].get("functions", [])) == 4, oracle["ssa"]
        assert 8 in operation_slots, operation_slots
        return {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "native_proof_ok": True,
            "native_ssa_verified_functions": native["verified_function_count"],
            "scalar_switches": len(switches),
            "stage0_ssa_functions": len(oracle["ssa"].get("functions", [])),
            "computed_subject_forms": ["select operation", "binary expression", "function call"],
            "native_operation_slots": operation_slots,
            "stage0_diagnostics": oracle.get("diagnostics", []),
        }
    finally:
        ssa.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
