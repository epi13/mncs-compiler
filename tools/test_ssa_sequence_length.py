#!/usr/bin/env python3
"""Stage-0, canonical VM, and Cranelift proof for sequence SSA operations."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from test_vm_segment import run_batch, vm_to_wire

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
PROBE = Path(os.environ.get("MNCS_PROBE_BIN", BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))
SSA_MODULE = "mncs.compiler.ssa.v1"
TYPE_ARGS = [{"kind": "nat", "value": 1024}, {"kind": "nat", "value": 1024}]
STRIDE = 1024
STEP_BUDGET = 8_000_000
BASE_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn index<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: byte) { return s[offset]; } "
    "fn widened<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: u64) { return s[offset] as u64; } "
    "fn length<N: Nat>(s: [byte; up_to N]) -> (r: u64) { return s.len; } "
    "fn below<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: bool) { "
    "if offset < s.len { return true; } return false; }"
)
SEQUENCE_LOOP_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn sequence_loop<N: Nat>(s: [byte; up_to N]) -> (r: u64) { "
    "iterate i over s carrying total: u64 = 0 { "
    "next total = total + i; } return total; }"
)
SEQUENCE_VIEW_LOOP_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn suffix_loop(s: [byte; up_to 8], start: u64) -> (r: u64) { "
    "iterate i over s[start..s.len] carrying total: u64 = 0 { "
    "next total = total + (i as u64); } return total; }"
)
COUNTED_LOOP_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn counted() -> (r: u64) { iterate i up_to 4 carrying total: u64 = 0 { "
    "next total = total + i; } return total; }"
)
NESTED_SEQUENCE_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn page_length<P: Nat, N: Nat>(pages: [[byte; up_to N]; up_to P], index: u64) -> (r: u64) { "
    "if index < pages.len { return pages[index].len; } return 0; }"
)
SELECT_SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn select_bound(offset: u64, total: u64) -> (r: u64) { "
    "return select(offset <= total, offset, total); }"
)
SOURCE = (
    "mncs 0.18; module frontier.ssa; "
    "fn index<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: byte) { return s[offset]; } "
    "fn widened<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: u64) { return s[offset] as u64; } "
    "fn length<N: Nat>(s: [byte; up_to N]) -> (r: u64) { return s.len; } "
    "fn below<N: Nat>(s: [byte; up_to N], offset: u64) -> (r: bool) { "
    "if offset < s.len { return true; } return false; } "
    "fn folded<N: Nat>(s: [byte; up_to N]) -> (r: u64) { "
    "iterate i over s carrying total: u64 = 0 { "
    "let item: u64 = s[i] as u64; next total = total + item; } return total; } "
    "fn counted() -> (r: u64) { iterate i up_to 4 carrying total: u64 = 0 { "
    "next total = total + i; } return total; }"
)
REAL_SOURCE_PATH = ROOT / "src" / "compiler" / "source.mncs"


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def byte_sequence(raw):
    return {"sequence": {"values": [{"byte": {"value": value}} for value in raw]}}


def page_value(raw):
    return {"sequence": {"values": [byte_sequence(raw)] if raw else []}}


def decode(value):
    if "record" in value:
        return {key: decode(item) for key, item in value["record"]["fields"]}
    if "finite" in value:
        finite = value["finite"]
        return {"$v": finite["discriminant"], "$p": {key: decode(item) for key, item in finite.get("payload", [])}}
    if "sequence" in value:
        return [decode(item) for item in value["sequence"]["values"]]
    if "boolean" in value:
        return value["boolean"]["value"]
    return next(iter(value.values()))["value"]


def linked(value):
    items = []
    while value["$v"] == 1:
        items.append(value["$p"]["head"])
        value = value["$p"]["tail"]
    assert value["$v"] == 0, value
    return items


def wire_linked(value):
    items = []
    while value["finite"]["discriminant"] == 1:
        fields = dict(value["finite"]["payload"])
        items.append(fields["head"])
        value = fields["tail"]
    assert value["finite"]["discriminant"] == 0, value
    return items


class Probe:
    def __init__(self, module, functions, backend=None):
        env = dict(os.environ)
        if backend is None or backend == "reference_interpreter":
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa"
        env["MNCS_PROBE_EXECUTION_MODULES"] = module
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
            {"module": module, "function": function, "type_arguments": TYPE_ARGS}
            for function in functions
        ])
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
        self.stderr_file = tempfile.TemporaryFile(mode="w+t")
        self.proc = subprocess.Popen(
            [str(PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr_file, text=True, env=env, cwd=ROOT,
        )

    def stderr_text(self):
        self.stderr_file.flush()
        self.stderr_file.seek(0)
        return self.stderr_file.read()

    def send(self, request):
        try:
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as error:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
            status = self.proc.wait(timeout=120)
            stderr = self.stderr_text()
            stdout = self.proc.stdout.read()
            self.stderr_file.close()
            raise AssertionError(
                f"probe exited {status} before request completed; "
                f"stdout={stdout[-4000:]!r}; stderr={stderr[-8000:]!r}"
            ) from error
        line = self.proc.stdout.readline()
        if not line:
            status = self.proc.poll()
            stderr = self.stderr_text()
            self.stderr_file.close()
            raise AssertionError(f"probe terminated: {status}; stderr={stderr[-8000:]!r}")
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        status = self.proc.wait(timeout=120)
        stderr = self.stderr_text()
        self.stderr_file.close()
        assert status == 0, f"probe exited {status}; stderr={stderr[-8000:]!r}"
        return stderr


def module_args(raw):
    return [page_value(raw), integer(STRIDE), integer(len(raw))]


def logical_module_args(raw):
    chunks = [raw[index:index + STRIDE] for index in range(0, len(raw), STRIDE)]
    pages = {"sequence": {"values": [byte_sequence(chunk) for chunk in chunks]}}
    return [pages, integer(STRIDE), integer(len(raw))]


def call(probe, module, function, args):
    result = probe.send({
        "schema_version": "0.1",
        "target": {"module": module, "function": function},
        "arguments": args,
        "type_arguments": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    })
    assert result["status"] == "returned", result
    return result


def field(wire, name):
    return next(value for key, value in wire["record"]["fields"] if key == name)


def suite():
    raw = SOURCE.encode()
    args = module_args(raw)
    small_sources = [
        {"id": "ssa-sequence-index-cast-length", "text": BASE_SOURCE},
        {"id": "ssa-sequence-iteration-loop", "text": SEQUENCE_LOOP_SOURCE},
        {"id": "ssa-sequence-view-iteration-loop", "text": SEQUENCE_VIEW_LOOP_SOURCE},
        {"id": "ssa-counted-iteration-loop", "text": COUNTED_LOOP_SOURCE},
        {"id": "ssa-nested-sequence-index-length", "text": NESTED_SEQUENCE_SOURCE},
        {"id": "ssa-select-operation", "text": SELECT_SOURCE},
    ]
    for item in small_sources:
        item["raw"] = item["text"].encode()
        item["args"] = module_args(item["raw"])
    real_source = REAL_SOURCE_PATH.read_bytes()
    real_args = logical_module_args(real_source)

    # The independently selected Stage-0 oracle accepts this exact source.
    flow_probe = Probe("mncs.compiler.flow.v1", ["lower_unit"], backend="cranelift")
    flow_result = call(flow_probe, "mncs.compiler.flow.v1", "lower_unit", args)
    flow_wire = flow_result["returned"][0]
    proof_wire = field(flow_wire, "proof")
    unit_wire = field(proof_wire, "unit")
    oracle_diagnostics = flow_probe.send({"elaborate": SOURCE})
    oracle_ssa = flow_probe.send({"ssa": SOURCE})
    oracle_fact = oracle_ssa.get("ssa")
    assert oracle_fact is not None and not oracle_ssa.get("diagnostics"), oracle_ssa
    assert field(proof_wire, "ok")["boolean"]["value"] is True
    for item in small_sources:
        small_flow = call(flow_probe, "mncs.compiler.flow.v1", "lower_unit", item["args"])["returned"][0]
        small_proof = field(small_flow, "proof")
        assert field(small_proof, "ok")["boolean"]["value"] is True, item["id"]
        item["flow_wire"] = small_flow
        item["unit_wire"] = field(small_proof, "unit")
        item["oracle_ssa"] = flow_probe.send({"ssa": item["text"]})
        assert item["oracle_ssa"].get("ssa") is not None and not item["oracle_ssa"].get("diagnostics"), item["oracle_ssa"]
    real_flow_result = call(flow_probe, "mncs.compiler.flow.v1", "lower_unit", real_args)
    real_flow_wire = real_flow_result["returned"][0]
    real_proof_wire = field(real_flow_wire, "proof")
    real_unit_wire = field(real_proof_wire, "unit")
    assert field(real_proof_wire, "ok")["boolean"]["value"] is True
    real_oracle_ssa = flow_probe.send({"ssa": real_source.decode()})
    assert real_oracle_ssa.get("ssa") is not None and not real_oracle_ssa.get("diagnostics"), real_oracle_ssa
    flow_stderr = flow_probe.close()

    decl_probe = Probe(
        "mncs.compiler.decl.v1", ["local_nominal_type_identities"], backend="cranelift"
    )
    nominal_result = call(
        decl_probe,
        "mncs.compiler.decl.v1",
        "local_nominal_type_identities",
        [*args, unit_wire],
    )
    nominal_wire = nominal_result["returned"][0]
    for item in small_sources:
        item["nominal_wire"] = call(
            decl_probe,
            "mncs.compiler.decl.v1",
            "local_nominal_type_identities",
            [*item["args"], item["unit_wire"]],
        )["returned"][0]
    real_nominal_result = call(
        decl_probe,
        "mncs.compiler.decl.v1",
        "local_nominal_type_identities",
        [*real_args, real_unit_wire],
    )
    real_nominal_wire = real_nominal_result["returned"][0]
    decl_stderr = decl_probe.close()

    functions = ["lower_value_ssa", "verify_function"]
    cranelift = Probe(SSA_MODULE, functions, backend="cranelift")
    cranelift_raw = call(cranelift, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nominal_wire])
    summary = decode(cranelift_raw["returned"][0])
    native_functions = linked(summary["functions"])
    function_wires = wire_linked(field(cranelift_raw["returned"][0], "functions"))
    assert summary["valid"] and summary["function_count"] == 6, summary
    assert summary["supported_function_count"] == summary["verified_function_count"] == 6, summary
    function_wire = function_wires[0]
    cast_function_wire = function_wires[1]
    verify_args = [*args, function_wire]
    verified = call(cranelift, SSA_MODULE, "verify_function", verify_args)
    assert verified["returned"] == [{"boolean": {"value": True}}], verified
    cast_verified = call(cranelift, SSA_MODULE, "verify_function", [*args, cast_function_wire])
    assert cast_verified["returned"] == [{"boolean": {"value": True}}], cast_verified
    for item in small_sources:
        native_raw = call(
            cranelift,
            SSA_MODULE,
            "lower_value_ssa",
            [*item["args"], item["flow_wire"], item["nominal_wire"]],
        )
        item["native_raw"] = native_raw
        item["native_summary"] = decode(native_raw["returned"][0])
        assert item["native_summary"]["valid"], item["native_summary"]
        assert item["native_summary"]["function_count"] == item["native_summary"]["supported_function_count"] == item["native_summary"]["verified_function_count"], item["native_summary"]
    nested = next(item for item in small_sources if item["id"] == "ssa-nested-sequence-index-length")
    nested_functions = wire_linked(field(nested["native_raw"]["returned"][0], "functions"))
    nested_function = nested_functions[0]
    nested_instructions = [
        instruction
        for block in wire_linked(field(nested_function, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
    ]
    nested_kinds = [field(instruction, "kind")["integer"]["value"] for instruction in nested_instructions]
    assert nested_kinds.count(10) == 2, nested_kinds
    assert nested_kinds.count(11) == 1, nested_kinds
    # Replacing the inner bounded sequence with an open sequence must fail
    # verification against the source signature.
    changed_nested = json.loads(json.dumps(nested_function))
    nested_inputs = wire_linked(field(changed_nested, "inputs"))
    nested_value = next(
        value for value in nested_inputs
        if field(value, "ty").get("finite", {}).get("discriminant") == 5
    )
    nested_type = field(nested_value, "ty")
    assert nested_type["finite"]["discriminant"] == 5, nested_type
    nested_element = dict(nested_type["finite"]["payload"])["element"]
    assert nested_element["finite"]["discriminant"] == 5, nested_element
    nested_element["finite"] = {
        "discriminant": 4,
        "payload": [],
        "type_identity": "mncs:0.2:finite-type:mncs.compiler.decl.v1::SemType",
        "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.decl.v1::SemType::TSeq",
    }
    nested_rejected = call(cranelift, SSA_MODULE, "verify_function", [*nested["args"], changed_nested])
    assert nested_rejected["returned"] == [{"boolean": {"value": False}}], nested_rejected
    viewed = next(item for item in small_sources if item["id"] == "ssa-sequence-view-iteration-loop")
    viewed_function = wire_linked(field(viewed["native_raw"]["returned"][0], "functions"))[0]
    viewed_instructions = [
        instruction
        for block in wire_linked(field(viewed_function, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
    ]
    view_instruction = next(
        item for item in viewed_instructions
        if field(item, "kind")["integer"]["value"] == 17
    )
    assert field(view_instruction, "operator")["integer"]["value"] == 9
    assert field(view_instruction, "argc")["integer"]["value"] == 3
    view_verified = call(cranelift, SSA_MODULE, "verify_function", [*viewed["args"], viewed_function])
    assert view_verified["returned"] == [{"boolean": {"value": True}}], view_verified
    changed_view = json.loads(json.dumps(viewed_function))
    changed_view_instruction = next(
        instruction
        for block in wire_linked(field(changed_view, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if field(instruction, "kind")["integer"]["value"] == 17
    )
    for key, value in changed_view_instruction["record"]["fields"]:
        if key == "binding_start":
            value["integer"]["value"] += 1
            break
    else:
        raise AssertionError("sequence-view instruction has no binding span")
    view_rejected = call(cranelift, SSA_MODULE, "verify_function", [*viewed["args"], changed_view])
    assert view_rejected["returned"] == [{"boolean": {"value": False}}], view_rejected
    selected = next(item for item in small_sources if item["id"] == "ssa-select-operation")
    selected_function = wire_linked(field(selected["native_raw"]["returned"][0], "functions"))[0]
    selected_instructions = [
        instruction
        for block in wire_linked(field(selected_function, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
    ]
    select_instruction = next(
        item for item in selected_instructions
        if field(item, "kind")["integer"]["value"] == 9
        and field(item, "operator")["integer"]["value"] == 8
    )
    selected_verified = call(cranelift, SSA_MODULE, "verify_function", [*selected["args"], selected_function])
    assert selected_verified["returned"] == [{"boolean": {"value": True}}], selected_verified
    changed_select = json.loads(json.dumps(selected_function))
    changed_instruction = next(
        instruction
        for block in wire_linked(field(changed_select, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
        if field(instruction, "kind")["integer"]["value"] == 9
        and field(instruction, "operator")["integer"]["value"] == 8
    )
    select_inputs = wire_linked(field(changed_instruction, "inputs"))
    assert len(select_inputs) == 3, select_inputs
    condition_id = field(select_inputs[0], "value")["integer"]["value"]
    true_id = field(select_inputs[1], "value")["integer"]["value"]
    assert condition_id != true_id
    for key, value in changed_instruction["record"]["fields"]:
        if key == "inputs":
            first_id = value["finite"]["payload"][0][1]
            for id_key, id_value in first_id["record"]["fields"]:
                if id_key == "value":
                    id_value["integer"]["value"] = true_id
                    break
            else:
                raise AssertionError("select condition ID has no value")
            break
    else:
        raise AssertionError("select instruction has no inputs")
    select_rejected = call(cranelift, SSA_MODULE, "verify_function", [*selected["args"], changed_select])
    assert select_rejected["returned"] == [{"boolean": {"value": False}}], select_rejected
    loop_function_wire = function_wires[4]
    loop_verified = call(cranelift, SSA_MODULE, "verify_function", [*args, loop_function_wire])
    assert loop_verified["returned"] == [{"boolean": {"value": True}}], loop_verified

    # Loop guard, initialization, and induction values are source anchored.
    changed_loop = json.loads(json.dumps(loop_function_wire))
    loop_blocks = wire_linked(field(changed_loop, "blocks"))
    loop_instruction = next(
        instruction
        for block in loop_blocks
        for instruction in wire_linked(field(block, "instructions"))
        if field(instruction, "kind")["integer"]["value"] == 13
    )
    for key, value in loop_instruction["record"]["fields"]:
        if key == "variant_start":
            value["integer"]["value"] += 1
            break
    else:
        raise AssertionError("loop guard has no source span")
    loop_rejected = call(cranelift, SSA_MODULE, "verify_function", [*args, changed_loop])
    assert loop_rejected["returned"] == [{"boolean": {"value": False}}], loop_rejected
    counted_function_wire = function_wires[5]
    counted_verified = call(cranelift, SSA_MODULE, "verify_function", [*args, counted_function_wire])
    assert counted_verified["returned"] == [{"boolean": {"value": True}}], counted_verified
    counted_kinds = [
        field(instruction, "kind")["integer"]["value"]
        for block in wire_linked(field(counted_function_wire, "blocks"))
        for instruction in wire_linked(field(block, "instructions"))
    ]
    assert [counted_kinds.count(kind) for kind in (13, 14, 15)] == [1, 1, 1], counted_kinds

    # Sequence indexing is bound to the exact source operation span.
    changed_function = json.loads(json.dumps(function_wire))
    blocks = field(changed_function, "blocks")
    first_block = blocks["finite"]["payload"][0][1]
    instructions = field(first_block, "instructions")
    first_instruction = instructions["finite"]["payload"][0][1]
    assert field(first_instruction, "kind")["integer"]["value"] == 11
    for key, value in first_instruction["record"]["fields"]:
        if key == "binding_start":
            value["integer"]["value"] += 1
            break
    else:
        raise AssertionError("sequence-index instruction has no binding_start")
    rejected = call(cranelift, SSA_MODULE, "verify_function", [*args, changed_function])
    assert rejected["returned"] == [{"boolean": {"value": False}}], rejected

    changed_cast_function = json.loads(json.dumps(cast_function_wire))
    cast_block = wire_linked(field(changed_cast_function, "blocks"))[0]
    cast_instructions = wire_linked(field(cast_block, "instructions"))
    cast_instruction = next(
        item for item in cast_instructions
        if field(item, "kind")["integer"]["value"] == 12
    )
    for key, value in cast_instruction["record"]["fields"]:
        if key == "binding_end":
            value["integer"]["value"] -= 1
            break
    else:
        raise AssertionError("cast instruction has no binding_end")
    cast_rejected = call(
        cranelift, SSA_MODULE, "verify_function", [*args, changed_cast_function]
    )
    assert cast_rejected["returned"] == [{"boolean": {"value": False}}], cast_rejected

    real_cranelift_raw = call(
        cranelift,
        SSA_MODULE,
        "lower_value_ssa",
        [*real_args, real_flow_wire, real_nominal_wire],
    )
    real_summary = decode(real_cranelift_raw["returned"][0])
    real_functions = linked(real_summary["functions"])
    real_rows = []
    for index, function in enumerate(real_functions):
        identity = function["identity"]
        function_name = bytes(identity["bytes"][:identity["len"]]).decode(errors="replace")
        failures = [
            block["lowering_failure_kind"]
            for block in linked(function["blocks"])
            if block["lowering_failure_kind"] > 0
        ]
        sequence_lengths = sum(
            instruction["kind"] == 10
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        sequence_indices = sum(
            instruction["kind"] == 11
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        scalar_casts = sum(
            instruction["kind"] == 12
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        loop_guards = sum(
            instruction["kind"] == 13
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        loop_advances = sum(
            instruction["kind"] == 14
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        loop_initializations = sum(
            instruction["kind"] == 15
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        record_constructions = sum(
            instruction["kind"] == 16
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        sequence_views = sum(
            instruction["kind"] == 17
            for block in linked(function["blocks"])
            for instruction in linked(block["instructions"])
        )
        real_rows.append({
            "index": index,
            "identity": function_name,
            "supported": function["supported"],
            "verified": function["verified"],
            "failure_kinds": failures,
            "sequence_length_instructions": sequence_lengths,
            "sequence_index_instructions": sequence_indices,
            "scalar_cast_instructions": scalar_casts,
            "loop_guard_instructions": loop_guards,
            "loop_advance_instructions": loop_advances,
            "loop_index_initializations": loop_initializations,
            "record_constructor_instructions": record_constructions,
            "sequence_view_instructions": sequence_views,
        })
    assert real_rows[0]["identity"].endswith("::byte_at"), real_rows[0]
    assert real_rows[0]["sequence_length_instructions"] == 1, real_rows[0]
    assert real_rows[0]["sequence_index_instructions"] == 1, real_rows[0]
    assert real_rows[0]["supported"] and real_rows[0]["verified"], real_rows[0]
    assert not real_rows[0]["failure_kinds"], real_rows[0]
    ascii_row = next(row for row in real_rows if row["identity"].endswith("::ascii"))
    assert ascii_row["supported"] and ascii_row["verified"], ascii_row
    assert not ascii_row["failure_kinds"], ascii_row
    assert ascii_row["sequence_index_instructions"] == 1, ascii_row
    assert ascii_row["scalar_cast_instructions"] == 1, ascii_row
    assert ascii_row["loop_guard_instructions"] == 1, ascii_row
    assert ascii_row["loop_advance_instructions"] == 1, ascii_row
    assert ascii_row["loop_index_initializations"] == 1, ascii_row

    emitted = cranelift.send({"emit_vm_artifact": {"module": SSA_MODULE}})
    artifact = emitted["artifact"]
    cranelift_stderr = cranelift.close()

    cases = []
    for item in small_sources:
        small_args = [*item["args"], item["flow_wire"], item["nominal_wire"]]
        cases.append({
            "id": item["id"],
            "function": "lower_value_ssa",
            "args": small_args,
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        })
    with tempfile.TemporaryDirectory(prefix="mncs-ssa-sequence-index-") as directory:
        path = Path(directory)
        artifact_path = path / "ssa.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_document, vm_wall, vm_rss, _, _ = run_batch(artifact_path, cases, path, module=SSA_MODULE)
    vm_results = vm_document["results"]
    vm_summaries = []
    for index, vm_result in enumerate(vm_results):
        assert vm_result["outcome"] == {"kind": "completed"}, vm_result
        vm_summary = decode(vm_to_wire(vm_result["record"]["returned"][0]))
        assert vm_summary == small_sources[index]["native_summary"], small_sources[index]["id"]
        vm_summaries.append(vm_summary)

    blocks = [block for function in native_functions for block in linked(function["blocks"])]
    instructions = [instruction for block in blocks for instruction in linked(block["instructions"])]
    assert [instruction["kind"] for instruction in instructions].count(10) == 2, instructions
    assert [instruction["kind"] for instruction in instructions].count(11) == 3, instructions
    assert [instruction["kind"] for instruction in instructions].count(12) == 2, instructions
    assert [instruction["kind"] for instruction in instructions].count(13) == 2, instructions
    assert [instruction["kind"] for instruction in instructions].count(14) == 2, instructions
    assert [instruction["kind"] for instruction in instructions].count(15) == 2, instructions
    assert [(item["code"], item["span"]["start"], item["span"]["end"])
            for item in oracle_diagnostics if item.get("code", "").startswith("MNE")] == []

    return {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_functions": summary["function_count"],
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "stage0_reference_ssa_fact_present": oracle_fact is not None,
        "native_summary": {key: summary[key] for key in (
            "valid", "function_count", "supported_function_count", "verified_function_count",
            "instruction_count", "block_count", "unsupported_block_count",
        )},
        "sequence_length_instructions": sum(item["kind"] == 10 for item in instructions),
        "sequence_index_instructions": sum(item["kind"] == 11 for item in instructions),
        "scalar_cast_instructions": sum(item["kind"] == 12 for item in instructions),
        "loop_guard_instructions": sum(item["kind"] == 13 for item in instructions),
        "loop_advance_instructions": sum(item["kind"] == 14 for item in instructions),
        "loop_index_initializations": sum(item["kind"] == 15 for item in instructions),
        "nested_sequence_executor_agreement": {
            "functions": nested["native_summary"]["function_count"],
            "supported_functions": nested["native_summary"]["supported_function_count"],
            "verified_functions": nested["native_summary"]["verified_function_count"],
            "sequence_length_instructions": nested_kinds.count(10),
            "sequence_index_instructions": nested_kinds.count(11),
            "open_inner_sequence_rejected": True,
        },
        "select_executor_agreement": {
            "functions": selected["native_summary"]["function_count"],
            "supported_functions": selected["native_summary"]["supported_function_count"],
            "verified_functions": selected["native_summary"]["verified_function_count"],
            "select_operation_instructions": sum(
                field(instruction, "kind")["integer"]["value"] == 9
                and field(instruction, "operator")["integer"]["value"] == 8
                for instruction in selected_instructions
            ),
            "wrong_condition_type_rejected": True,
        },
        "record_constructor_instructions": sum(item["kind"] == 16 for item in instructions),
        "sequence_view_loop_executor_agreement": {
            "functions": viewed["native_summary"]["function_count"],
            "supported_functions": viewed["native_summary"]["supported_function_count"],
            "verified_functions": viewed["native_summary"]["verified_function_count"],
            "sequence_view_instructions": sum(
                field(instruction, "kind")["integer"]["value"] == 17
                for instruction in viewed_instructions
            ),
            "stage0_reference_accepts": bool(viewed["oracle_ssa"].get("ssa")) and not viewed["oracle_ssa"].get("diagnostics"),
            "verifier_rejects_changed_span": True,
        },
        "verifier_accepts_source_bound_instruction": True,
        "verifier_rejects_changed_field_span": True,
        "canonical_vm_cranelift_identical": True,
        "vm_artifact_id": artifact["artifact_id"],
        "vm_peak_rss_kb": vm_rss,
        "vm_wall_seconds": round(vm_wall, 6),
        "executor_agreement": [
            {
                "id": item["id"],
                "functions": item["native_summary"]["function_count"],
                "stage0_reference_accepts": bool(item["oracle_ssa"].get("ssa")) and not item["oracle_ssa"].get("diagnostics"),
                "vm_steps": vm_results[index]["record"]["usage"]["steps"],
                "semantic_digest": hashlib.sha256(json.dumps(item["native_summary"], sort_keys=True).encode()).hexdigest(),
            }
            for index, item in enumerate(small_sources)
        ],
        "cache_observations": [
            line for stderr in (flow_stderr, decl_stderr, cranelift_stderr)
            for line in stderr.splitlines() if "mncs-stage0-probe" in line
        ],
        "real_source": {
            "path": REAL_SOURCE_PATH.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(real_source).hexdigest(),
            "proof_ok": True,
            "stage0_reference_ssa_fact_present": real_oracle_ssa.get("ssa") is not None,
            "stage0_diagnostics": len(real_oracle_ssa.get("diagnostics", [])),
            "native_steps": real_cranelift_raw["steps"],
            "summary": {key: real_summary.get(key) for key in (
                "valid", "function_count", "supported_function_count", "verified_function_count",
                "value_count", "instruction_count", "block_count", "unsupported_block_count",
                "first_unsupported_function", "first_unsupported_block", "first_unsupported_kind",
            )},
            "functions": real_rows,
            "execution_scope": "native Cranelift only; whole-module source exceeds the current canonical VM step ceiling",
        },
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    started = time.monotonic()
    result = suite()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    OUT = ROOT / ".build" / "ssa-sequence-frontier-results.json"
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
