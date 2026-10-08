#!/usr/bin/env python3
"""Three-executor witness for SSA verification beyond 1,024 values."""
import copy
import hashlib
import json
import os
from pathlib import Path
import time

from test_ssa_sequence_length import (
    Probe,
    SSA_MODULE,
    TYPE_ARGS,
    call,
    decode,
    field,
    logical_module_args,
    wire_linked,
)

ROOT = Path(__file__).resolve().parents[1]
VALUE_COUNT = 1200
SOURCE = (
    "mncs 0.18; module frontier.ssa_value_scale; "
    "fn seed(value: u64) -> (result: u64) { return value; }"
)


def set_field(record, name, value):
    fields = record["record"]["fields"]
    for index, (key, _) in enumerate(fields):
        if key == name:
            fields[index][1] = value
            return
    raise AssertionError(f"record has no field {name}")


def set_integer(value, integer):
    value["integer"]["value"] = integer


def synthetic_values(template, count, duplicate_second=False):
    values = field(template, "values")
    cell = values["finite"]
    head_template = dict(cell["payload"])["head"]
    tail = values
    while tail["finite"]["discriminant"] == 1:
        tail = dict(tail["finite"]["payload"])["tail"]

    result = tail
    for value_id in range(count):
        head = copy.deepcopy(head_template)
        head_fields = {key: value for key, value in head["record"]["fields"]}
        set_integer(head_fields["id"], value_id)
        set_integer(head_fields["index"], 0)
        set_integer(head_fields["owner_block"], 0)
        set_integer(head_fields["kind"], 0)
        if duplicate_second and value_id == count - 2:
            set_integer(head_fields["id"], count - 1)
        result = {
            "finite": {
                **{key: copy.deepcopy(value) for key, value in cell.items() if key not in ("discriminant", "payload")},
                "discriminant": 1,
                "payload": [["head", head], ["tail", result]],
            }
        }
    output = copy.deepcopy(template)
    set_field(output, "values", result)
    set_field(output, "value_count", {"integer": {"type": {"bits": 64, "signed": False}, "value": count}})
    return output


def verify_calls(probe, module_args, function):
    values = field(function, "values")
    valid = call(probe, SSA_MODULE, "verify_value_ids", [*module_args, function])
    bad_function = synthetic_values(function, VALUE_COUNT, duplicate_second=True)
    invalid = call(probe, SSA_MODULE, "verify_value_ids", [*module_args, bad_function])
    kind_count = call(probe, SSA_MODULE, "value_kind_count", [*module_args, values, {"integer": {"type": {"bits": 64, "signed": False}, "value": 0}}])
    lookup = call(probe, SSA_MODULE, "find_value", [*module_args, values, {"integer": {"type": {"bits": 64, "signed": False}, "value": 0}}])
    lookup_value = decode(lookup["returned"][0])
    return {
        "dense_ids_accept": decode(valid["returned"][0]),
        "duplicate_id_rejected": decode(invalid["returned"][0]) is False,
        "kind_zero_count": decode(kind_count["returned"][0]),
        "oldest_id_found": lookup_value["found"] and lookup_value["value"]["id"] == 0,
    }


def suite():
    raw = SOURCE.encode()
    module_args = logical_module_args(raw)

    flow = Probe("mncs.compiler.flow.v1", ["lower_unit"], backend="cranelift")
    flow_wire = call(flow, "mncs.compiler.flow.v1", "lower_unit", module_args)["returned"][0]
    oracle = flow.send({"ssa": SOURCE})
    assert oracle.get("ssa") is not None and not oracle.get("diagnostics"), oracle
    unit = field(field(flow_wire, "proof"), "unit")
    flow.close()

    decl = Probe("mncs.compiler.decl.v1", ["local_nominal_type_identities"], backend="cranelift")
    nominals = call(decl, "mncs.compiler.decl.v1", "local_nominal_type_identities", [*module_args, unit])["returned"][0]
    decl.close()

    ssa = Probe(SSA_MODULE, ["lower_value_ssa", "verify_value_ids", "value_kind_count", "find_value"], backend="cranelift")
    lowered = call(ssa, SSA_MODULE, "lower_value_ssa", [*module_args, flow_wire, nominals])["returned"][0]
    functions = wire_linked(field(lowered, "functions"))
    assert len(functions) == 1 and decode(field(lowered, "valid")) is True, decode(lowered)
    function = synthetic_values(functions[0], VALUE_COUNT)
    cranelift = verify_calls(ssa, module_args, function)
    assert cranelift == {
        "dense_ids_accept": True,
        "duplicate_id_rejected": True,
        "kind_zero_count": VALUE_COUNT,
        "oldest_id_found": True,
    }, cranelift
    ssa.close()

    reference = Probe(SSA_MODULE, ["verify_value_ids", "value_kind_count", "find_value"])
    reference_result = verify_calls(reference, module_args, function)
    reference.close()
    assert reference_result == cranelift, (reference_result, cranelift)

    return {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "stage0_reference_accepts_source": True,
        "synthetic_value_count": VALUE_COUNT,
        "reference_cranelift": reference_result,
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    import sys
    sys.setrecursionlimit(10000)
    started = time.monotonic()
    print(json.dumps({**suite(), "elapsed_seconds": round(time.monotonic() - started, 3)}, indent=2))
