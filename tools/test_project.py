#!/usr/bin/env python3
"""Native bounded project snapshot vs current Stage-0 module resolver.

Python only builds typed request values and compares compiler outputs. The
project/module/import interpretation remains in MNCS or the Rust oracle.
"""
import hashlib
import copy
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = os.environ.get("MNCS_CAMPAIGN_ID", datetime.now(timezone.utc).strftime("%Y%m%d"))
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
os.environ.setdefault("MNCS_PROBE_MODULES", "source,lexer,parser,segment,decl,flow,ssa,project")
os.environ.setdefault("MNCS_PROBE_EXECUTION_MODULES", "mncs.compiler.project.v1,mncs.compiler.ssa.v1")
os.environ.setdefault("MNCS_PROBE_BACKEND", "cranelift")
os.environ.setdefault("MNCS_PROBE_GENERIC_SEEDS", json.dumps([
    {"module": "mncs.compiler.project.v1", "function": "compile_project",
     "type_arguments": [
         {"kind": "nat", "value": 3},
         {"kind": "nat", "value": 1024},
     ]},
    {"module": "mncs.compiler.ssa.v1", "function": "verify_function",
     "type_arguments": [{"kind": "nat", "value": 1024}]},
]))


def byte_sequence(raw):
    return {"sequence": {"values": [{"byte": {"value": value}} for value in raw]}}


def discover_sources(root):
    """Host boundary: enumerate files, read exact bytes, and sort stable IDs."""
    discovered = []
    for path in root.rglob("*.mncs"):
        source_id = path.relative_to(root).as_posix()
        discovered.append((source_id, path, path.read_bytes()))
    return sorted(discovered, key=lambda item: item[0].encode())


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


def flist(value, nil=0, cons=1):
    items = []
    while value["$v"] == cons:
        items.append(value["$p"]["head"])
        value = value["$p"]["tail"]
    assert value["$v"] == nil, value
    return items


def identity_text(identity):
    assert identity["valid"] is True and identity["len"] > 0, identity
    return bytes(identity["bytes"][:identity["len"]]).decode()


class Probe:
    def __init__(self):
        env = os.environ.copy()
        if env.get("MNCS_PROBE_BACKEND") == "reference_interpreter":
            env.pop("MNCS_PROBE_BACKEND", None)
        self.proc = subprocess.Popen(
            [env.get("MNCS_PROBE_BIN", str(BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.digest = hashlib.sha256()
        self.requests = 0
        self.steps = []
        self.last_response = None

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"Stage-0 probe terminated: {self.proc.poll()}"
        result = json.loads(line)
        self.last_response = result
        self.digest.update(json.dumps([request, result], sort_keys=True).encode())
        self.requests += 1
        return result

    def native(self, request):
        result = self.send(request)
        assert result["status"] == "returned", result
        self.steps.append(result["steps"])
        return decode(result["returned"][0])

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


def identity_map(probe):
    records = probe.send({"record_types": "mncs.compiler.project.v1"})
    return {record["name"]: record for record in records}


def record(identities, name, fields):
    type_info = identities[name]
    return {
        "record": {
            "type_identity": type_info["identity"],
            "name": name,
            "fields": [[key, value] for key, value in fields.items()],
        }
    }


def wire_field(value, name):
    return next(item for key, item in value["record"]["fields"] if key == name)


def set_wire_field(value, name, replacement):
    fields = value["record"]["fields"]
    for index, (key, _) in enumerate(fields):
        if key == name:
            fields[index] = [key, replacement]
            return
    raise KeyError(name)


def wire_sequence(value):
    return value["sequence"]["values"]


def wire_flist(value, nil=0, cons=1):
    items = []
    while True:
        finite = value["finite"]
        if finite["discriminant"] == nil:
            return items
        assert finite["discriminant"] == cons, finite
        payload = {key: item for key, item in finite.get("payload", [])}
        items.append(payload["head"])
        value = payload["tail"]


def set_wire_variant_field(value, name, replacement):
    payload = value["finite"].get("payload", [])
    for index, (key, _) in enumerate(payload):
        if key == name:
            payload[index] = [key, replacement]
            return
    raise KeyError(name)


def wire_number(value):
    return next(iter(value.values()))["value"]


def verify_ssa_function(probe, source, function, byte_bound=1024):
    return probe.native({
        "schema_version": "0.1",
        "target": {"module": "mncs.compiler.ssa.v1", "function": "verify_function"},
        "arguments": [byte_sequence(source.encode()), function],
        "type_arguments": [{"kind": "nat", "value": byte_bound}],
        "step_budget": 8_000_000,
    })


def source_value(identities, source_id, path):
    return record(identities, "ProjectSource", {
        "source_id": byte_sequence(source_id.encode()),
        "source_path": byte_sequence(path.encode()),
    })


def request_value(identities, sources, module_bound=3, byte_bound=1024):
    project = record(identities, "ProjectSnapshot", {
        "fingerprint": byte_sequence(hashlib.sha256(
            b"".join(len(source[2]).to_bytes(8, "big") + source[2] for source in sources)
        ).hexdigest().encode()),
        "sources": {"sequence": {"values": [
            source_value(identities, source_id, source_id)
            for source_id, _, _ in sources
        ]}},
    })
    return {
        "schema_version": "0.1",
        "target": {"module": "mncs.compiler.project.v1", "function": "compile_project"},
        "arguments": [project, {"sequence": {"values": [byte_sequence(text) for _, _, text in sources]}}],
        "type_arguments": [
            # M is a capacity bound. One retained M=3 instance serves the
            # one-, two-, and three-module fixtures below.
            {"kind": "nat", "value": module_bound},
            # Per-file values remain exact length. N is their common Profile
            # 0.18 sequence ceiling, not padding or semantic source content.
            {"kind": "nat", "value": byte_bound},
        ],
        "step_budget": 8_000_000,
    }


def check_operations(project_root, probe, identities):
    """CP-0019: authorized compiler operations reach proof, typed TOp,
    CFG, kind-9 SSA, and SSA verification; corruptions are rejected and
    the unauthorized twin matches Stage-0 at the same span."""
    operation_source = (
        "mncs 0.18; module demo.operations; "
        "record Artifact { count: u64 } "
        "fn kind_of(i: u64) -> (r: u64) capability fs effect fs_list authorized_by fs { return fs_entry_kind_at(i); } "
        "fn size_of(i: u64) -> (r: u64) capability fs effect fs_list authorized_by fs { return fs_entry_size_at(i); } "
        "fn mtime_of(i: u64) -> (r: u64) capability fs effect fs_list authorized_by fs { return fs_entry_mtime_at(i); } "
        "fn load(p: [byte; up_to 1024], s: [byte; up_to 64]) -> (r: Artifact) capability a effect structured_read authorized_by a { return structured_read(p, s); } "
        "fn store(p: [byte; up_to 1024], s: [byte; up_to 64], v: Artifact) -> (r: u64) capability a effect structured_write authorized_by a { return structured_write(p, s, v); }"
    )
    assert len(operation_source.encode()) <= 1024, len(operation_source.encode())
    operation_probe = Probe()
    try:
        for path in project_root.glob("*.mncs"):
            path.unlink()
        (project_root / "a-operations.mncs").write_text(operation_source)
        operation_native = operation_probe.native(request_value(identities, discover_sources(project_root)))
        operation_wire_project = copy.deepcopy(operation_probe.last_response["returned"][0])
        operation_modules = flist(operation_native["modules"])
        operation_module = next(module for module in operation_modules if module["source_index"] == 0)
        operation_ssa = operation_module["value_ssa"]
        assert operation_native["valid"] is True, operation_native["diagnostics"]
        assert operation_native["value_ssa_valid"] is True, operation_ssa
        assert operation_ssa["valid"] is True and operation_ssa["verified_function_count"] == 5, operation_ssa
        operation_oracle = probe.send({"project_oracle": {"root": operation_source, "modules": {}}})
        assert operation_oracle["valid"] is True, operation_oracle
        assert operation_oracle["ssa"] is not None

        # Proof reaches typed TOp nodes with canonical operation identity.
        tops = [op for body in flist(operation_module["flow"]["proof"]["tops"]) for op in flist(body)]
        top_ops = [op["$p"] for op in tops if "slot" in op.get("$p", {})]
        assert len(top_ops) == 5, tops
        assert sorted(op["slot"] for op in top_ops) == [0, 1, 2, 3, 4], top_ops
        for op in top_ops:
            name = operation_source[op["ns"]:op["ne"]]
            assert identity_text(op["identity"]) == f"mncs:0.2:operation::{name}", op
        operation_functions = flist(operation_ssa["functions"])

        def operation_instructions(function_name):
            function = next(item for item in operation_functions
                            if identity_text(item["identity"]).endswith(f"::{function_name}"))
            return [instruction for block in flist(function["blocks"])
                    for instruction in flist(block["instructions"])
                    if instruction["kind"] == 9]

        operation_operators = {
            name: [(instruction["operator"], instruction["argc"])
                   for instruction in operation_instructions(name)]
            for name in ("kind_of", "size_of", "mtime_of", "load", "store")
        }
        assert operation_operators == {
            "kind_of": [(0, 1)], "size_of": [(3, 1)], "mtime_of": [(4, 1)],
            "load": [(1, 2)], "store": [(2, 3)],
        }, operation_operators
        for name in ("kind_of", "size_of", "mtime_of", "load", "store"):
            for instruction in operation_instructions(name):
                op_name = operation_source[instruction["binding_start"]:instruction["binding_end"]]
                assert identity_text(instruction["identity"]) == f"mncs:0.2:operation::{op_name}", instruction
                assert len(flist(instruction["inputs"])) == instruction["argc"], instruction
                assert instruction["nominal_identity"]["valid"] is False, instruction
        load_ty = operation_instructions("load")[0]["ty"]
        assert load_ty["$v"] == 3, load_ty

        operation_wire_modules = wire_flist(wire_field(operation_wire_project, "modules"))
        operation_wire_module = next(module for module in operation_wire_modules
                                     if wire_number(wire_field(module, "source_index")) == 0)
        operation_wire_ssa = wire_field(operation_wire_module, "value_ssa")
        operation_wire_functions = wire_flist(wire_field(operation_wire_ssa, "functions"))

        def operation_rejected(name, source_function, mutate):
            candidate = copy.deepcopy(next(
                function for function in operation_wire_functions
                if identity_text(decode(wire_field(function, "identity"))).endswith(f"::{source_function}")
            ))
            mutate(candidate)
            accepted = verify_ssa_function(operation_probe, operation_source, candidate)
            assert accepted is False, {"case": name, "accepted": accepted}

        def op_instruction(function):
            return next(inst for block in wire_flist(wire_field(function, "blocks"))
                        for inst in wire_flist(wire_field(block, "instructions"))
                        if wire_number(wire_field(inst, "kind")) == 9)

        def mutate_op_slot(function):
            set_wire_field(op_instruction(function), "operator",
                           {"integer": {"type": {"bits": 64, "signed": False}, "value": 9}})

        def mutate_op_identity_invalid(function):
            identity = wire_field(op_instruction(function), "identity")
            set_wire_field(identity, "valid", {"boolean": {"value": False}})

        def mutate_op_identity_mismatch(function):
            donor = next(item for item in operation_wire_functions
                         if identity_text(decode(wire_field(item, "identity"))).endswith("::size_of"))
            donor_instruction = next(inst for block in wire_flist(wire_field(donor, "blocks"))
                                     for inst in wire_flist(wire_field(block, "instructions"))
                                     if wire_number(wire_field(inst, "kind")) == 9)
            set_wire_field(op_instruction(function), "identity",
                           copy.deepcopy(wire_field(donor_instruction, "identity")))

        def mutate_op_arity(function):
            set_wire_field(op_instruction(function), "argc",
                           {"integer": {"type": {"bits": 64, "signed": False}, "value": 2}})

        def mutate_op_result_type(function):
            instruction = op_instruction(function)
            refs = wire_flist(wire_field(instruction, "inputs"))
            assert len(refs) == 3, len(refs)
            value_id = wire_number(wire_field(refs[2], "value"))
            nominal_ty = copy.deepcopy(next(
                wire_field(value, "ty") for value in wire_flist(wire_field(function, "values"))
                if wire_number(wire_field(value, "id")) == value_id))
            set_wire_field(instruction, "ty", nominal_ty)
            result_id = wire_number(wire_field(instruction, "result"))
            result_value = next(value for value in wire_flist(wire_field(function, "values"))
                                if wire_number(wire_field(value, "id")) == result_id)
            set_wire_field(result_value, "ty", copy.deepcopy(nominal_ty))

        def mutate_op_input_type(function):
            instruction = op_instruction(function)
            refs = wire_flist(wire_field(instruction, "inputs"))
            assert len(refs) == 3, len(refs)
            path_ref = copy.deepcopy(wire_field(refs[0], "value"))
            set_wire_field(refs[1], "value", path_ref)

        def mutate_op_provenance(function):
            instruction = op_instruction(function)
            end = wire_number(wire_field(instruction, "binding_end"))
            set_wire_field(instruction, "binding_end",
                           {"integer": {"type": {"bits": 64, "signed": False}, "value": end + 1}})

        operation_rejected("op_slot", "kind_of", mutate_op_slot)
        operation_rejected("op_identity_invalid", "kind_of", mutate_op_identity_invalid)
        operation_rejected("op_identity_mismatch", "kind_of", mutate_op_identity_mismatch)
        operation_rejected("op_arity", "kind_of", mutate_op_arity)
        operation_rejected("op_result_type", "store", mutate_op_result_type)
        operation_rejected("op_input_type", "store", mutate_op_input_type)
        operation_rejected("op_provenance", "size_of", mutate_op_provenance)
        operation_positive = verify_ssa_function(
            operation_probe, operation_source,
            next(function for function in operation_wire_functions
                 if identity_text(decode(wire_field(function, "identity"))).endswith("::store")))
        assert operation_positive is True

        # Unauthorized twin: the same operation without its declared
        # effect fails natively with kind 53 where Stage-0 reports
        # MNE257, at the same source span.
        denied_source = (
            "mncs 0.18; module demo.denied; "
            "fn kind_of(i: u64) -> (r: u64) capability fs { return fs_entry_kind_at(i); }"
        )
        for path in project_root.glob("*.mncs"):
            path.unlink()
        (project_root / "a-denied.mncs").write_text(denied_source)
        denied_native = operation_probe.native(request_value(identities, discover_sources(project_root)))
        denied_module = next(module for module in flist(denied_native["modules"])
                             if module["source_index"] == 0)
        denied_obligations = flist(denied_module["flow"]["proof"]["obls"])
        denied_kind = next(item for item in denied_obligations if item["kind"] == 53)
        denied_oracle = probe.send({"project_oracle": {"root": denied_source, "modules": {}}})
        assert denied_native["valid"] is False
        assert denied_oracle["valid"] is False, denied_oracle
        stage0_denied = next(item for item in denied_oracle["diagnostics"] if item["code"] == "MNE257")
        op_start = denied_source.index("fs_entry_kind_at(i)")
        op_span = [op_start, op_start + len("fs_entry_kind_at(i)")]
        assert [denied_kind["start"], denied_kind["end"]] == op_span, denied_kind
        assert [stage0_denied["span"]["start"], stage0_denied["span"]["end"]] == op_span, stage0_denied
        operation_case = {
            "native_project_valid": operation_native["valid"],
            "native_value_ssa_valid": operation_native["value_ssa_valid"],
            "stage0_project_valid": operation_oracle["valid"],
            "verified_functions": operation_ssa["verified_function_count"],
            "operation_operators": {name: pairs for name, pairs in operation_operators.items()},
            "covered": ["fs metadata operations", "nominal Artifact structured read", "nominal Artifact structured write", "canonical operation identity in proof and SSA", "binding-span provenance"],
            "corruption_rejections": ["slot", "identity invalid", "identity mismatch", "arity", "result type", "input type", "provenance"],
            "ssa_verifier_positive": operation_positive,
            "unauthorized_twin": {
                "native_obligation_kind": denied_kind["kind"],
                "native_span": [denied_kind["start"], denied_kind["end"]],
                "stage0_diagnostic": stage0_denied["code"],
                "stage0_span": [stage0_denied["span"]["start"], stage0_denied["span"]["end"]],
                "matching": True,
            },
        }
        operation_case["requests"] = operation_probe.requests
        operation_case["result_sha256"] = operation_probe.digest.hexdigest()
        operation_case["native_execution_steps_total"] = sum(operation_probe.steps)
        return operation_case
    finally:
        operation_probe.close()


def run():
    dependency = "mncs 0.18; module demo.dep; fn answer(value: u64) -> (r: u64) { return value; }"
    root = "mncs 0.18; module demo.root; use demo.dep as dep; fn main() -> (r: u64) { return 1; }"
    # Cross the experimental 256-byte unit ABI and reach the current
    # Profile-0.18 Nat ceiling. The project ABI carries path metadata
    # separately from generic nested source-byte sequences; it has no
    # four-chunk compatibility adapter or hard-coded 256-byte source field.
    root = root + " " * (1024 - len(root.encode()))
    with tempfile.TemporaryDirectory(prefix="mncs-project-") as directory:
        project_root = Path(directory)
        # Create in reverse path order; the host adapter must produce the same
        # deterministic source order regardless of filesystem traversal.
        (project_root / "b-root.mncs").write_text(root)
        (project_root / "a-dep.mncs").write_text(dependency)
        discovered = discover_sources(project_root)
        probe = Probe()
        enum_probe = None
        scalar_probe = None
        projection_probe = None
        try:
            execution_status = probe.send({"execution_status": True})
            if os.environ.get("MNCS_PROBE_BACKEND") == "cranelift":
                assert execution_status["backend"] == "cranelift", execution_status
                assert execution_status["retained_sessions"] == 2, execution_status
            identities = identity_map(probe)
            native_request = request_value(identities, discovered)
            first = probe.native(native_request)
            second = probe.native(native_request)
            assert first == second, "project output must be deterministic for one exact snapshot"
            assert first["valid"] is True, first["diagnostics"]
            assert first["source_count"] == first["module_count"] == 2
            modules = flist(first["modules"])
            imports = flist(first["imports"])
            assert [bytes(module["source_id"]).decode() for module in modules] == ["a-dep.mncs", "b-root.mncs"]
            assert len(imports) == 1 and imports[0]["source_index"] == 1 and imports[0]["target_index"] == 0
            assert imports[0]["has_alias"] is True
            assert modules[1]["source_index"] == 1
            assert modules[0]["flow"]["proof"]["ok"] is True
            assert modules[1]["flow"]["proof"]["ok"] is True
            assert first["fingerprint_authenticated"] is False
            # A caller-supplied digest is unverified metadata; changing it
            # must not change project semantic validity.
            tampered_request = request_value(identities, discovered)
            tampered_request["arguments"][0]["record"]["fields"] = [
                [key, byte_sequence(b"0" * 64) if key == "fingerprint" else value]
                for key, value in tampered_request["arguments"][0]["record"]["fields"]
            ]
            tampered = probe.native(tampered_request)
            assert tampered["valid"] is True and tampered["fingerprint_authenticated"] is False
            native_flow = []
            for module in modules:
                source_text = discovered[module["source_index"]][2]
                header = module["flow"]["proof"]["unit"]["header"]
                native_flow.append({
                    "module": source_text[header["module_start"]:header["module_end"]].decode(),
                    "functions": len(flist(module["flow"]["functions"])),
                    "verified": all(fn["verified"] for fn in flist(module["flow"]["functions"])),
                })
            oracle = probe.send({"project_oracle": {
                "root": root,
                "modules": {"demo.dep": dependency},
            }})
            assert oracle["valid"] is True, oracle["diagnostics"]
            assert len(oracle["module_resolutions"]) == 1, oracle["module_resolutions"]
            assert oracle["program"] is not None
            assert oracle["program"]["module"] == "demo.root"
            assert len(oracle["program"]["functions"]) == 2
            assert oracle["ssa"] is not None
            assert len(oracle["ssa"]["functions"]) == len(oracle["program"]["functions"])

            # Compare native declarations from both modules with the linked
            # Stage-0 program. Spans stay byte-based and identity ownership is
            # selected by the declaring module, not a colliding short name.
            reference_functions = {}
            for function in oracle["program"]["functions"]:
                owner = function.get("home_module") or oracle["program"]["module"]
                reference_functions[(owner, function["name"])] = function
            signature_matches = []
            for module in modules:
                source_text = discovered[module["source_index"]][2]
                unit = module["flow"]["proof"]["unit"]
                header = unit["header"]
                module_name = source_text[header["module_start"]:header["module_end"]].decode()
                for function in flist(unit["fns"]):
                    signature = function["sig"]
                    name = source_text[signature["name_start"]:signature["name_end"]].decode()
                    reference = reference_functions[(module_name, name)]
                    native_inputs = [
                        {
                            "name": source_text[field["name_start"]:field["name_end"]].decode(),
                            "type": source_text[field["type_start"]:field["type_end"]].decode(),
                        }
                        for field in flist(signature["params"])
                    ]
                    native_outputs = [
                        {
                            "name": source_text[field["name_start"]:field["name_end"]].decode(),
                            "type": source_text[field["type_start"]:field["type_end"]].decode(),
                        }
                        for field in flist(signature["results"])
                    ]
                    assert native_inputs == reference["inputs"], (module_name, name, native_inputs, reference["inputs"])
                    assert native_outputs == reference["outputs"], (module_name, name, native_outputs, reference["outputs"])
                    signature_matches.append(f"{module_name}::{name}")

            # The project resolver supplies the imported scalar signature and
            # a compiler-built canonical Stage-0 callable identity.
            imported_caller = "mncs 0.18; module demo.imported_call; use demo.dep as dep; fn main() -> (r: u64) { return dep.answer(42); }"
            (project_root / "b-root.mncs").write_text(imported_caller)
            imported_sources = discover_sources(project_root)
            imported_native = probe.native(request_value(identities, imported_sources))
            imported_modules = flist(imported_native["modules"])
            imported_root = next(module for module in imported_modules if module["source_index"] == 1)
            imported_obligations = flist(imported_root["flow"]["proof"]["obls"])
            imported_tops = flist(imported_root["flow"]["proof"]["tops"])
            imported_ops = [op for body in imported_tops for op in flist(body)]
            imported_call_ops = [op["$p"] for op in imported_ops if "identity" in op.get("$p", {})]
            imported_oracle = probe.send({"project_oracle": {
                "root": imported_caller,
                "modules": {"demo.dep": dependency},
            }})
            assert imported_native["import_count"] == 1
            assert imported_native["valid"] is True, imported_native["diagnostics"]
            assert imported_root["flow"]["proof"]["ok"] is True, imported_obligations
            assert len(imported_call_ops) == 1, imported_ops
            resolved_call = imported_call_ops[0]
            assert resolved_call["argc"] == 1, resolved_call
            native_identity = resolved_call["identity"]
            native_identity_text = identity_text(native_identity)
            assert imported_oracle["valid"] is True, imported_oracle["diagnostics"]
            assert imported_oracle["program"] is not None
            assert len(imported_oracle["program"]["functions"]) == 2
            assert imported_oracle["ssa"] is not None
            stage0_callable = next(
                function for function in imported_oracle["program"]["functions"]
                if function["home_module"] == "demo.dep" and function["name"] == "answer"
            )
            assert native_identity_text == stage0_callable["identity"], (native_identity_text, stage0_callable)
            imported_call_identity = {
                "native_project_valid": imported_native["valid"],
                "native_import_count": imported_native["import_count"],
                "native_root_proof_ok": imported_root["flow"]["proof"]["ok"],
                "native_callable_identity": native_identity_text,
                "native_argument_count": resolved_call["argc"],
                "stage0_callable_identity": stage0_callable["identity"],
                "identity_binding": "typed call carries the exact canonical Stage-0 callable identity",
                "stage0_valid": imported_oracle["valid"],
                "stage0_diagnostics": imported_oracle["diagnostics"],
                "stage0_linked_functions": len(imported_oracle["program"]["functions"]),
                "stage0_ssa_functions": len(imported_oracle["ssa"]["functions"]),
            }

            # The imported call now reaches value SSA, with its canonical ID
            # still attached.  Exercise a branch join in the same project so
            # this checks cross-module identity and block arguments together.
            imported_ssa = imported_root["value_ssa"]
            imported_ssa_identity = imported_ssa["first_call_identity"]
            imported_ssa_identity_text = identity_text(imported_ssa_identity)
            assert imported_ssa["valid"] is True, imported_ssa
            assert imported_ssa["call_count"] == 1 and imported_ssa["has_call"] is True
            assert imported_ssa_identity_text == native_identity_text
            assert imported_ssa_identity == native_identity

            merge_root = (
                "mncs 0.18; module demo.merge_root; use demo.dep as dep; "
                "fn main(flag: bool) -> (r: u64) { "
                "let value: u64 = dep.answer(17); if flag { } else { } return value; }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-dep.mncs").write_text(dependency)
            (project_root / "b-root.mncs").write_text(merge_root)
            merge_sources = discover_sources(project_root)
            merge_native = probe.native(request_value(identities, merge_sources))
            merge_wire_project = copy.deepcopy(probe.last_response["returned"][0])
            merge_modules = flist(merge_native["modules"])
            merge_module = next(module for module in merge_modules if module["source_index"] == 1)
            merge_ssa = merge_module["value_ssa"]
            merge_ssa_identity = merge_ssa["first_call_identity"]
            merge_ssa_identity_text = identity_text(merge_ssa_identity)
            merge_oracle = probe.send({"project_oracle": {
                "root": merge_root,
                "modules": {"demo.dep": dependency},
            }})
            assert merge_native["valid"] is True, merge_native["diagnostics"]
            assert merge_native["value_ssa_valid"] is True, [module["value_ssa"] for module in merge_modules]
            assert merge_module["flow"]["proof"]["ok"] is True
            assert merge_ssa["valid"] is True, merge_ssa
            assert merge_ssa["function_count"] == 1
            assert merge_ssa["call_count"] == 1 and merge_ssa["has_call"] is True
            assert merge_ssa_identity_text == "mncs:0.2:function:demo.dep::answer"
            assert merge_ssa["block_count"] >= 4, merge_ssa
            assert merge_ssa["block_parameter_count"] >= 3, merge_ssa
            merge_functions = flist(merge_ssa["functions"])
            assert len(merge_functions) == 1, merge_functions
            merge_function = merge_functions[0]
            assert merge_function["supported"] is True and merge_function["verified"] is True
            native_root_callable_identity = identity_text(merge_function["identity"])
            merge_blocks = flist(merge_function["blocks"])
            merge_values = flist(merge_function["values"])
            merge_value_ids = [value["id"] for value in merge_values]
            assert len(merge_value_ids) == len(set(merge_value_ids)) == merge_function["value_count"]
            assert set(merge_value_ids) == set(range(merge_function["value_count"]))
            merge_instructions = [
                instruction for block in merge_blocks
                for instruction in flist(block["instructions"])
            ]
            merge_calls = [instruction for instruction in merge_instructions if instruction["kind"] == 3]
            assert len(merge_calls) == 1, merge_calls
            assert merge_calls[0]["argc"] == 1
            assert len(flist(merge_calls[0]["inputs"])) == len(flist(merge_calls[0]["params"])) == 1
            assert identity_text(merge_calls[0]["identity"]) == merge_ssa_identity_text
            terminator_kinds = [block["terminator"]["$v"] for block in merge_blocks]
            assert 0 in terminator_kinds and 1 in terminator_kinds and 2 in terminator_kinds
            assert 3 not in terminator_kinds, terminator_kinds

            # Check verifier rejection paths using the exact wire graph emitted
            # by project compilation. These mutations isolate definition,
            # use-before-definition, call/edge/branch/return types, and targets.
            merge_wire_modules = wire_flist(wire_field(merge_wire_project, "modules"))
            merge_wire_module = next(
                module for module in merge_wire_modules
                if wire_number(wire_field(module, "source_index")) == 1
            )
            merge_wire_ssa = wire_field(merge_wire_module, "value_ssa")
            merge_wire_functions = wire_flist(wire_field(merge_wire_ssa, "functions"))
            assert len(merge_wire_functions) == 1
            merge_wire_function = merge_wire_functions[0]
            wire_blocks = wire_flist(wire_field(merge_wire_function, "blocks"))
            wire_values = wire_flist(wire_field(merge_wire_function, "values"))
            wire_inputs = wire_flist(wire_field(merge_wire_function, "inputs"))
            wire_instructions = [
                instruction for block in wire_blocks
                for instruction in wire_flist(wire_field(block, "instructions"))
            ]
            wire_call = next(inst for inst in wire_instructions if wire_number(wire_field(inst, "kind")) == 3)
            wire_branch_block = next(block for block in wire_blocks if wire_field(block, "terminator")["finite"]["discriminant"] == 0)
            wire_branch = wire_field(wire_branch_block, "terminator")
            wire_jump_block = next(block for block in wire_blocks if wire_field(block, "terminator")["finite"]["discriminant"] == 1)
            wire_jump = wire_field(wire_jump_block, "terminator")
            wire_return_block = next(block for block in wire_blocks if wire_field(block, "terminator")["finite"]["discriminant"] == 2)
            wire_return = wire_field(wire_return_block, "terminator")
            bool_input_id = copy.deepcopy(wire_field(wire_inputs[0], "id"))
            assert wire_number(wire_field(wire_inputs[0], "kind")) == 0
            verifier_rejections = {}

            def rejected(name, mutate):
                candidate = copy.deepcopy(merge_wire_function)
                mutate(candidate)
                accepted = verify_ssa_function(probe, merge_root, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}
                verifier_rejections[name] = "rejected"

            rejected("duplicate_value_id", lambda function: set_wire_field(
                wire_flist(wire_field(function, "values"))[1], "id",
                copy.deepcopy(wire_field(wire_flist(wire_field(function, "values"))[0], "id")),
            ))

            def invalidate_function_identity(function):
                identity = wire_field(function, "identity")
                set_wire_field(identity, "valid", {"boolean": {"value": False}})

            rejected("invalid_function_identity", invalidate_function_identity)

            def replace_call_input_with_result(function):
                call = next(inst for block in wire_flist(wire_field(function, "blocks"))
                            for inst in wire_flist(wire_field(block, "instructions"))
                            if wire_number(wire_field(inst, "kind")) == 3)
                set_wire_field(wire_flist(wire_field(call, "inputs"))[0], "value",
                               copy.deepcopy(wire_field(call, "result")))

            rejected("call_use_before_definition", replace_call_input_with_result)

            def replace_call_input_with_bool(function):
                call = next(inst for block in wire_flist(wire_field(function, "blocks"))
                            for inst in wire_flist(wire_field(block, "instructions"))
                            if wire_number(wire_field(inst, "kind")) == 3)
                argument = wire_flist(wire_field(call, "inputs"))[0]
                set_wire_field(argument, "value", copy.deepcopy(bool_input_id))

            rejected("imported_call_argument_type", replace_call_input_with_bool)

            def invalidate_call_identity(function):
                call = next(inst for block in wire_flist(wire_field(function, "blocks"))
                            for inst in wire_flist(wire_field(block, "instructions"))
                            if wire_number(wire_field(inst, "kind")) == 3)
                identity = wire_field(call, "identity")
                set_wire_field(identity, "valid", {"boolean": {"value": False}})

            rejected("invalid_call_identity", invalidate_call_identity)

            def replace_branch_condition_with_u64(function):
                block = next(block for block in wire_flist(wire_field(function, "blocks"))
                             if wire_field(block, "terminator")["finite"]["discriminant"] == 0)
                term = wire_field(block, "terminator")
                set_wire_variant_field(term, "condition", copy.deepcopy(wire_field(wire_call, "result")))

            rejected("branch_condition_type", replace_branch_condition_with_u64)

            def replace_branch_argument_with_bool(function):
                blocks = wire_flist(wire_field(function, "blocks"))
                block = next(block for block in blocks
                             if wire_field(block, "terminator")["finite"]["discriminant"] == 0)
                term = wire_field(block, "terminator")
                payload = {key: value for key, value in term["finite"]["payload"]}
                target = wire_number(payload["yes"])
                target_block = next(candidate for candidate in blocks
                                    if wire_number(wire_field(candidate, "id")) == target)
                args = wire_flist(payload["yes_args"])
                params = wire_flist(wire_field(target_block, "parameters"))
                bool_type = wire_field(wire_inputs[0], "ty")
                mismatch = next((argument for argument, parameter in zip(args, params)
                                 if wire_field(parameter, "ty") != bool_type), None)
                assert mismatch is not None, "join must carry a non-boolean value to test type rejection"
                set_wire_field(mismatch, "value", copy.deepcopy(bool_input_id))

            rejected("block_argument_type", replace_branch_argument_with_bool)

            def replace_jump_target(function):
                block = next(block for block in wire_flist(wire_field(function, "blocks"))
                             if wire_field(block, "terminator")["finite"]["discriminant"] == 1)
                set_wire_variant_field(wire_field(block, "terminator"), "target", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            rejected("invalid_terminator_target", replace_jump_target)

            def replace_return_with_bool(function):
                block = next(block for block in wire_flist(wire_field(function, "blocks"))
                             if wire_field(block, "terminator")["finite"]["discriminant"] == 2)
                set_wire_variant_field(wire_field(block, "terminator"), "value", copy.deepcopy(bool_input_id))

            rejected("return_type", replace_return_with_bool)
            verifier_positive = verify_ssa_function(probe, merge_root, merge_wire_function)
            assert verifier_positive is True
            assert merge_oracle["valid"] is True, merge_oracle["diagnostics"]
            assert merge_oracle["ssa"] is not None
            merge_oracle_root = next(
                function for function in merge_oracle["ssa"]["functions"]
                if function.get("semantic_identity") == "mncs:0.2:function:demo.merge_root::main"
            )
            merge_oracle_blocks = merge_oracle_root["blocks"]
            merge_oracle_normal_blocks = [block for block in merge_oracle_blocks if block["path"] == "normal"]
            merge_oracle_terminators = [next(iter(block["terminator"])) for block in merge_oracle_normal_blocks]
            merge_stage0_callable = next(
                function for function in merge_oracle["program"]["functions"]
                if function["home_module"] == "demo.dep" and function["name"] == "answer"
            )
            merge_stage0_root_callable = next(
                function for function in merge_oracle["program"]["functions"]
                if (function.get("home_module") or merge_oracle["program"]["module"]) == "demo.merge_root"
                and function["name"] == "main"
            )
            assert merge_oracle["program"] is not None
            assert len(merge_oracle_normal_blocks) == merge_ssa["block_count"], merge_oracle_root
            assert "Branch" in merge_oracle_terminators, merge_oracle_root
            assert "Return" in merge_oracle_terminators, merge_oracle_root
            assert merge_stage0_callable["identity"] == merge_ssa_identity_text
            assert native_root_callable_identity == merge_stage0_root_callable["identity"]
            multi_module_value_ssa = {
                "native_project_valid": merge_native["valid"],
                "native_project_ssa_valid": merge_native["value_ssa_valid"],
                "native_root_ssa_valid": merge_ssa["valid"],
                "native_root_blocks": merge_ssa["block_count"],
                "native_root_block_parameters": merge_ssa["block_parameter_count"],
                "native_root_values": merge_ssa["value_count"],
                "native_root_call_identity": merge_ssa_identity_text,
                "native_root_function_identity": native_root_callable_identity,
                "native_function_values_have_unique_dense_ids": True,
                "native_function_blocks_and_terminators": terminator_kinds,
                "ssa_verifier_positive": verifier_positive,
                "ssa_verifier_negative_checks": verifier_rejections,
                "stage0_project_valid": merge_oracle["valid"],
                "stage0_root_blocks": len(merge_oracle_normal_blocks),
                "stage0_root_terminators": merge_oracle_terminators,
                "stage0_imported_callable_identity": merge_stage0_callable["identity"],
                "stage0_root_callable_identity": merge_stage0_root_callable["identity"],
                "stage0_has_branch": True,
                "stage0_has_return": True,
                "claim": "bounded imported scalar call plus structured branch/join/return verified by native value SSA; not full SSA parity",
            }

            # Imported parameter types participate in the same argument
            # obligation/span proof as local signatures.
            mismatched_caller = "mncs 0.18; module demo.imported_call; use demo.dep as dep; fn main() -> (r: u64) { return dep.answer(true); }"
            (project_root / "b-root.mncs").write_text(mismatched_caller)
            mismatch_sources = discover_sources(project_root)
            mismatch_native = probe.native(request_value(identities, mismatch_sources))
            mismatch_modules = flist(mismatch_native["modules"])
            mismatch_root = next(module for module in mismatch_modules if module["source_index"] == 1)
            mismatch_obligations = flist(mismatch_root["flow"]["proof"]["obls"])
            mismatch = next(item for item in mismatch_obligations if item["kind"] == 26)
            mismatch_oracle = probe.send({"project_oracle": {
                "root": mismatched_caller,
                "modules": {"demo.dep": dependency},
            }})
            arg_start = mismatched_caller.index("true")
            assert mismatch_native["valid"] is False
            assert [mismatch["start"], mismatch["end"]] == [arg_start, arg_start + 4]
            # The failing proof stage explains itself at project level: one
            # ProofFailed diagnostic (discriminant 5) naming the module and
            # the proof error span.
            mismatch_diags = flist(mismatch_native["diagnostics"])
            proof_failed = [d for d in mismatch_diags if d["$v"] == 5]
            assert len(proof_failed) == 1, mismatch_diags
            assert proof_failed[0]["$p"]["source_index"] == 1, proof_failed
            assert [proof_failed[0]["$p"]["start"], proof_failed[0]["$p"]["end"]] == [arg_start, arg_start + 4], proof_failed
            assert mismatch_oracle["valid"] is False
            stage0_arg_mismatch = next(item for item in mismatch_oracle["diagnostics"] if item["code"] == "MNE133")
            assert [stage0_arg_mismatch["span"]["start"], stage0_arg_mismatch["span"]["end"]] == [arg_start, arg_start + 4]
            imported_argument_check = {
                "native_obligation_kind": mismatch["kind"],
                "native_span": [mismatch["start"], mismatch["end"]],
                "stage0_diagnostic": stage0_arg_mismatch["code"],
                "stage0_span": [stage0_arg_mismatch["span"]["start"], stage0_arg_mismatch["span"]["end"]],
                "matching": True,
            }

            # Distinguish same-spelled declarations by canonical module and
            # function identity, independent of source ordering.
            flags = "mncs 0.18; module demo.flags; fn answer(value: bool) -> (r: bool) { return value; }"
            names_root = (
                "mncs 0.18; module demo.names; use demo.dep as dep; use demo.flags as flags; "
                "fn answer(value: u64) -> (r: u64) { return value; } "
                "fn local_call() -> (r: u64) { return answer(2); } "
                "fn dep_call() -> (r: u64) { return dep.answer(7); } "
                "fn flag_call() -> (r: bool) { return flags.answer(true); }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-flags.mncs").write_text(flags)
            (project_root / "b-dep.mncs").write_text(dependency)
            (project_root / "c-root.mncs").write_text(names_root)
            names_sources = discover_sources(project_root)
            names_native = probe.native(request_value(identities, names_sources))
            names_modules = flist(names_native["modules"])
            names_root_module = next(module for module in names_modules if module["source_index"] == 2)
            names_calls = []
            for tops in flist(names_root_module["flow"]["proof"]["tops"]):
                for operation in flist(tops):
                    payload = operation.get("$p", {})
                    if "identity" not in payload:
                        continue
                    names_calls.append(identity_text(payload["identity"]))
            names_oracle = probe.send({"project_oracle": {
                "root": names_root,
                "modules": {"demo.dep": dependency, "demo.flags": flags},
            }})
            assert names_native["valid"] is True, names_native["diagnostics"]
            assert names_root_module["flow"]["proof"]["ok"] is True
            assert set(names_calls) == {
                "mncs:0.2:function:demo.names::answer",
                "mncs:0.2:function:demo.dep::answer",
                "mncs:0.2:function:demo.flags::answer",
            }, names_calls
            assert names_oracle["valid"] is True, names_oracle["diagnostics"]
            namespace_collision_case = {
                "native_valid": names_native["valid"],
                "stage0_valid": names_oracle["valid"],
                "canonical_call_identities": sorted(set(names_calls)),
                "local_and_two_imported_answers_resolved_separately": True,
                "stage0_function_count": len(names_oracle["program"]["functions"]),
            }

            # Keep explicit reference observations for resolver edge cases.
            # Missing modules are diagnosed natively. Imported nominal types
            # are current known gaps and are recorded against Stage-0 rather
            # than hidden behind a scalar-only success case.
            missing_root = "mncs 0.18; module demo.missing; use demo.absent as absent; fn main() -> (r: u64) { return 1; }"
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-root.mncs").write_text(missing_root)
            missing_sources = discover_sources(project_root)
            missing_native = probe.native(request_value(identities, missing_sources))
            missing_oracle = probe.send({"project_oracle": {"root": missing_root, "modules": {}}})
            assert missing_native["valid"] is False and flist(missing_native["diagnostics"])
            assert missing_oracle["valid"] is False, missing_oracle
            missing_import_case = {
                "native_valid": missing_native["valid"],
                "native_diagnostics": flist(missing_native["diagnostics"]),
                "stage0_valid": missing_oracle["valid"],
                "stage0_diagnostics": missing_oracle["diagnostics"],
            }

            duplicate_leaf = "mncs 0.18; module demo.duplicate; fn answer() -> (r: u64) { return 1; }"
            duplicate_root = "mncs 0.18; module demo.duplicate_user; use demo.duplicate as dup; fn main() -> (r: u64) { return dup.answer(); }"
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-first.mncs").write_text(duplicate_leaf)
            (project_root / "b-second.mncs").write_text(duplicate_leaf)
            (project_root / "c-root.mncs").write_text(duplicate_root)
            duplicate_sources = discover_sources(project_root)
            duplicate_native = probe.native(request_value(identities, duplicate_sources))
            duplicate_oracle = probe.send({"project_oracle": {
                "root": duplicate_root,
                # Stage-0's host Sources map has one source per module key, so
                # the native project case also records its duplicate-file
                # limitation instead of pretending the oracle saw both files.
                "modules": {"demo.duplicate": duplicate_leaf},
            }})
            assert duplicate_native["valid"] is False
            assert flist(duplicate_native["diagnostics"])
            assert duplicate_oracle["valid"] is True, duplicate_oracle
            duplicate_module_case = {
                "native_valid": duplicate_native["valid"],
                "native_diagnostics": flist(duplicate_native["diagnostics"]),
                "stage0_single_key_source_valid": duplicate_oracle["valid"],
                "oracle_duplicate_key_limit": "Stage-0 resolver map represents one source per declared module name",
            }

            missing_member_root = "mncs 0.18; module demo.missing_member; use demo.dep as dep; fn main() -> (r: u64) { return dep.no_such_function(7); }"
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-dep.mncs").write_text(dependency)
            (project_root / "b-root.mncs").write_text(missing_member_root)
            missing_member_sources = discover_sources(project_root)
            missing_member_native = probe.native(request_value(identities, missing_member_sources))
            missing_member_root_module = next(
                module for module in flist(missing_member_native["modules"])
                if module["source_index"] == 1
            )
            missing_member_oracle = probe.send({"project_oracle": {
                "root": missing_member_root,
                "modules": {"demo.dep": dependency},
            }})
            assert missing_member_native["valid"] is False
            assert missing_member_oracle["valid"] is False, missing_member_oracle
            missing_member_case = {
                "native_valid": missing_member_native["valid"],
                "native_proof_obligations": flist(missing_member_root_module["flow"]["proof"]["obls"]),
                "stage0_valid": missing_member_oracle["valid"],
                "stage0_diagnostics": missing_member_oracle["diagnostics"],
            }

            # A module that does not parse is invalid with a ParseFailed
            # diagnostic (discriminant 4), never invalid without a reason.
            broken_root = "mncs 0.18; module demo.broken; fn main( -> (r: u64) { return 1; }"
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-root.mncs").write_text(broken_root)
            broken_sources = discover_sources(project_root)
            broken_native = probe.native(request_value(identities, broken_sources))
            broken_oracle = probe.send({"project_oracle": {"root": broken_root, "modules": {}}})
            broken_diags = flist(broken_native["diagnostics"])
            parse_failed = [d for d in broken_diags if d["$v"] == 4]
            assert broken_native["valid"] is False
            assert len(parse_failed) == 1, broken_diags
            assert parse_failed[0]["$p"]["source_index"] == 0, parse_failed
            assert broken_oracle["valid"] is False, broken_oracle
            parse_failure_case = {
                "native_valid": broken_native["valid"],
                "native_diagnostics": broken_diags,
                "stage0_valid": broken_oracle["valid"],
                "stage0_diagnostics": broken_oracle["diagnostics"],
            }

            nominal_leaf = (
                "mncs 0.18; module demo.nominal_leaf; record Inner { value: u64 } "
                "enum Choice { Some { value: u64 }, None }"
            )
            nominal_types = (
                "mncs 0.18; module demo.nominal; use demo.nominal_leaf as inner; "
                "record Outer { inner: inner.Inner, entries: [inner.Inner; 2] } "
                "fn echo_record(value: Outer) -> (r: Outer) { let copy: Outer = value; return copy; } "
                "fn echo_inner(value: inner.Inner) -> (r: inner.Inner) { return value; } "
                "fn echo_batch(value: [inner.Inner; 2]) -> (r: [inner.Inner; 2]) { return value; } "
                "fn echo_choice(value: inner.Choice) -> (r: inner.Choice) { return value; }"
            )
            nominal_root = (
                "mncs 0.18; module demo.nominal_user; use demo.nominal as types; "
                "use demo.nominal_leaf as values; "
                "fn record_call(value: types.Outer) -> (r: types.Outer) { let copy: types.Outer = value; return types.echo_record(copy); } "
                "fn inner_call(value: values.Inner) -> (r: values.Inner) { return types.echo_inner(value); } "
                "fn batch_call(value: [values.Inner; 2]) -> (r: [values.Inner; 2]) { return types.echo_batch(value); } "
                "fn finite_call(value: values.Choice) -> (r: values.Choice) { return types.echo_choice(value); }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-nominal-bridge.mncs").write_text(nominal_types)
            (project_root / "b-nominal-leaf.mncs").write_text(nominal_leaf)
            (project_root / "c-root.mncs").write_text(nominal_root)
            nominal_sources = discover_sources(project_root)
            nominal_native = probe.native(request_value(identities, nominal_sources))
            nominal_root_module = next(
                module for module in flist(nominal_native["modules"])
                if module["source_index"] == 2
            )
            nominal_oracle = probe.send({"project_oracle": {
                "root": nominal_root,
                "modules": {
                    "demo.nominal": nominal_types,
                    "demo.nominal_leaf": nominal_leaf,
                },
            }})
            assert nominal_oracle["valid"] is True, nominal_oracle
            assert nominal_native["valid"] is True, nominal_native["diagnostics"]
            assert nominal_native["value_ssa_valid"] is True, nominal_native
            nominal_type_case = {
                "native_valid": nominal_native["valid"],
                "native_value_ssa_valid": nominal_native["value_ssa_valid"],
                "native_diagnostics": flist(nominal_native["diagnostics"]),
                "native_proof_obligations": flist(nominal_root_module["flow"]["proof"]["obls"]),
                "stage0_valid": nominal_oracle["valid"],
                "stage0_module_resolution_count": len(nominal_oracle["module_resolutions"]),
                "coverage": [
                    "imported record through a module that imports its nested field type",
                    "imported finite identity",
                    "imported exact sequence of a nominal type",
                    "let-annotation nominal ownership",
                ],
                "coverage_gap": False,
            }

            # Enum values retain their canonical nominal and variant identity
            # in value SSA. Keep construction and finite-match cases in small
            # project requests: the selected native runtime has a bounded
            # canonical arena, and each request is independently installed.
            enum_probe = Probe()
            constructor_source = (
                "mncs 0.18; module demo.construct; "
                "record Box { value: u64 } "
                "enum Pair { P { left: u64, right: bool }, Empty } "
                "enum Wrapped { W { item: Box }, Empty } "
                "enum Flag { Yes { set: bool }, No } "
                "fn yes(value: bool) -> (r: Flag) { return Flag.Yes { set: value }; } "
                "fn no() -> (r: Flag) { return Flag.No; } "
                "fn no_braces() -> (r: Flag) { return Flag.No { }; } "
                "fn pair(left: u64, right: bool) -> (r: Pair) { return Pair.P { left: left, right: right }; } "
                "fn wrapped(value: Box) -> (r: Wrapped) { return Wrapped.W { item: value }; } "
                "fn repeat(value: u64, wrong: bool) -> (r: [u64; 3]) { return [value; 3]; }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-construct.mncs").write_text(constructor_source)
            constructor_native = enum_probe.native(request_value(identities, discover_sources(project_root)))
            constructor_wire_project = copy.deepcopy(enum_probe.last_response["returned"][0])
            constructor_modules = flist(constructor_native["modules"])
            constructor_module = next(module for module in constructor_modules if module["source_index"] == 0)
            constructor_ssa = constructor_module["value_ssa"]
            assert constructor_native["valid"] is True, constructor_native["diagnostics"]
            assert constructor_native["value_ssa_valid"] is True, constructor_ssa
            assert constructor_ssa["valid"] is True and constructor_ssa["verified_function_count"] == 6, constructor_ssa
            constructor_functions = flist(constructor_ssa["functions"])
            constructor_instructions = [
                instruction for function in constructor_functions
                for block in flist(function["blocks"])
                for instruction in flist(block["instructions"])
                if instruction["kind"] == 5
            ]
            assert len(constructor_instructions) == 5, constructor_instructions
            assert all(instruction["nominal_identity"]["valid"] for instruction in constructor_instructions)
            assert all(instruction["variant_end"] > instruction["variant_start"] for instruction in constructor_instructions)
            pair_function = next(function for function in constructor_functions
                                 if identity_text(function["identity"]).endswith("::pair"))
            pair_inst = next(instruction for block in flist(pair_function["blocks"])
                             for instruction in flist(block["instructions"])
                             if instruction["kind"] == 5)
            assert pair_inst["argc"] == 2
            assert len(flist(pair_inst["inputs"])) == len(flist(pair_inst["params"])) == 2
            repeat_instructions = [
                instruction for function in constructor_functions
                for block in flist(function["blocks"])
                for instruction in flist(block["instructions"])
                if instruction["kind"] == 7
            ]
            assert len(repeat_instructions) == 1, repeat_instructions
            repeat_instruction = repeat_instructions[0]
            assert repeat_instruction["operator"] == 3 and repeat_instruction["argc"] == 1
            assert repeat_instruction["ty"]["$v"] == 5 and repeat_instruction["ty"]["$p"]["length"] == 3
            constructor_oracle = probe.send({"project_oracle": {"root": constructor_source, "modules": {}}})
            assert constructor_oracle["valid"] is True, constructor_oracle

            match_source = (
                "mncs 0.18; module demo.matches; "
                "enum Flag { Yes { set: bool }, No } "
                "enum Pair { P { left: u64, right: bool }, Empty } "
                "fn is_set(value: Flag) -> (r: bool) { return match value { Yes { set: bound } => bound, No => false }; } "
                "fn ignore_set(value: Flag) -> (r: bool) { return match value { Yes { set: bound } => true, No => false }; } "
                "fn pair_left(value: Pair) -> (r: u64) { return match value { P { left: left, right: _ } => left, Empty => 0 }; } "
                "fn normalize(value: Flag) -> (r: Flag) { return match value { Yes { set: bound } => Flag.Yes { set: bound }, No => Flag.No }; } "
                "fn mixed_match(extra: u64, value: Flag) -> (r: u64) { return match value { Yes { set: _ } => extra, No => 0 }; }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-match.mncs").write_text(match_source)
            match_native = enum_probe.native(request_value(identities, discover_sources(project_root)))
            match_wire_project = copy.deepcopy(enum_probe.last_response["returned"][0])
            match_modules = flist(match_native["modules"])
            match_module = next(module for module in match_modules if module["source_index"] == 0)
            match_ssa = match_module["value_ssa"]
            assert match_native["valid"] is True, match_native["diagnostics"]
            assert match_native["value_ssa_valid"] is True, match_ssa
            assert match_ssa["valid"] is True and match_ssa["verified_function_count"] == 5, match_ssa
            match_oracle = probe.send({"project_oracle": {"root": match_source, "modules": {}}})
            assert match_oracle["valid"] is True, match_oracle
            match_functions = flist(match_ssa["functions"])
            enum_functions = constructor_functions + match_functions
            enum_instructions = [
                instruction for function in enum_functions
                for block in flist(function["blocks"])
                for instruction in flist(block["instructions"])
                if instruction["kind"] == 5
            ]
            assert len(enum_instructions) == 7, enum_instructions
            assert all(instruction["nominal_identity"]["valid"] for instruction in enum_instructions)
            assert all(instruction["variant_end"] > instruction["variant_start"] for instruction in enum_instructions)
            enum_switch_blocks = [
                (function, block) for function in match_functions
                for block in flist(function["blocks"])
                if block["terminator"]["$v"] == 4
            ]
            enum_switches = [block["terminator"]["$p"] for _, block in enum_switch_blocks]
            assert len(enum_switches) == 5, enum_switches
            assert all(item["arm_count"] == 2 and len(flist(item["cases"])) == 2 for item in enum_switches)
            payload_instructions = [
                instruction for function in enum_functions
                for block in flist(function["blocks"])
                for instruction in flist(block["instructions"])
                if instruction["kind"] == 6
            ]
            assert len(payload_instructions) == 4, payload_instructions

            constructor_wire_modules = wire_flist(wire_field(constructor_wire_project, "modules"))
            constructor_wire_module = next(module for module in constructor_wire_modules
                                    if wire_number(wire_field(module, "source_index")) == 0)
            constructor_wire_ssa = wire_field(constructor_wire_module, "value_ssa")
            constructor_wire_functions = wire_flist(wire_field(constructor_wire_ssa, "functions"))
            match_wire_modules = wire_flist(wire_field(match_wire_project, "modules"))
            match_wire_module = next(module for module in match_wire_modules
                                     if wire_number(wire_field(module, "source_index")) == 0)
            match_wire_ssa = wire_field(match_wire_module, "value_ssa")
            match_wire_functions = wire_flist(wire_field(match_wire_ssa, "functions"))

            def enum_rejected(name, mutate):
                candidate = copy.deepcopy(next(
                    function for function in constructor_wire_functions
                    if identity_text(decode(wire_field(function, "identity"))).endswith("::pair")
                ))
                mutate(candidate)
                accepted = verify_ssa_function(enum_probe, constructor_source, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}

            def mutate_enum_identity(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 5)
                identity = wire_field(instruction, "nominal_identity")
                set_wire_field(identity, "valid", {"boolean": {"value": False}})

            def mutate_enum_variant(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 5)
                set_wire_field(instruction, "variant_start", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_enum_arity(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 5)
                set_wire_field(instruction, "argc", {"integer": {"type": {"bits": 64, "signed": False}, "value": 1}})

            def mutate_enum_payload_type(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 5)
                inputs = wire_flist(wire_field(function, "inputs"))
                expected_type = wire_field(wire_flist(wire_field(instruction, "params"))[0], "ty")
                wrong_type_input = next(value for value in inputs
                                        if wire_field(value, "ty") != expected_type)
                set_wire_field(wire_flist(wire_field(instruction, "inputs"))[0], "value",
                               copy.deepcopy(wire_field(wrong_type_input, "id")))

            enum_rejected("invalid_enum_identity", mutate_enum_identity)
            enum_rejected("unknown_enum_variant", mutate_enum_variant)
            enum_rejected("enum_construction_arity", mutate_enum_arity)
            enum_rejected("enum_payload_type", mutate_enum_payload_type)

            def repeat_rejected(name, mutate):
                candidate = copy.deepcopy(next(
                    function for function in constructor_wire_functions
                    if identity_text(decode(wire_field(function, "identity"))).endswith("::repeat")
                ))
                mutate(candidate)
                accepted = verify_ssa_function(enum_probe, constructor_source, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}

            def mutate_repeat_count(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 7)
                set_wire_field(instruction, "operator", {"integer": {"type": {"bits": 64, "signed": False}, "value": 2}})

            def mutate_repeat_operand_type(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 7)
                wrong_input = next(value for value in wire_flist(wire_field(function, "inputs"))
                                   if wire_field(value, "ty")["finite"]["discriminant"] == 0)
                set_wire_field(wire_flist(wire_field(instruction, "inputs"))[0],
                               "value", copy.deepcopy(wire_field(wrong_input, "id")))

            repeat_rejected("repeat_count", mutate_repeat_count)
            repeat_rejected("repeat_operand_type", mutate_repeat_operand_type)

            def enum_match_rejected(name, source_function, mutate):
                candidate = copy.deepcopy(next(
                    function for function in match_wire_functions
                    if identity_text(decode(wire_field(function, "identity"))).endswith(f"::{source_function}")
                ))
                mutate(candidate)
                accepted = verify_ssa_function(enum_probe, match_source, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}

            def mutate_match_variant(function):
                term = next(wire_field(block, "terminator")
                            for block in wire_flist(wire_field(function, "blocks"))
                            if wire_field(block, "terminator")["finite"]["discriminant"] == 4)
                payload = {key: value for key, value in term["finite"]["payload"]}
                case = wire_flist(payload["cases"])[0]
                set_wire_field(case, "variant_start", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_match_target(function):
                term = next(wire_field(block, "terminator")
                            for block in wire_flist(wire_field(function, "blocks"))
                            if wire_field(block, "terminator")["finite"]["discriminant"] == 4)
                set_wire_variant_field(term, "join_target", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_match_case_arity(function):
                term = next(wire_field(block, "terminator")
                            for block in wire_flist(wire_field(function, "blocks"))
                            if wire_field(block, "terminator")["finite"]["discriminant"] == 4)
                payload = {key: value for key, value in term["finite"]["payload"]}
                case = wire_flist(payload["cases"])[0]
                first_argument = wire_flist(wire_field(case, "arguments"))[0]
                set_wire_field(first_argument, "value", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_payload_declared_type(function):
                instruction = next(inst for block in wire_flist(wire_field(function, "blocks"))
                                   for inst in wire_flist(wire_field(block, "instructions"))
                                   if wire_number(wire_field(inst, "kind")) == 6)
                subject_type = copy.deepcopy(wire_field(wire_flist(wire_field(function, "inputs"))[0], "ty"))
                set_wire_field(instruction, "ty", subject_type)
                field_parameter = wire_flist(wire_field(instruction, "params"))[0]
                set_wire_field(field_parameter, "ty", copy.deepcopy(subject_type))
                result_id = wire_number(wire_field(instruction, "result"))
                result_value = next(value for value in wire_flist(wire_field(function, "values"))
                                    if wire_number(wire_field(value, "id")) == result_id)
                set_wire_field(result_value, "ty", copy.deepcopy(subject_type))

            enum_match_rejected("finite_match_variant", "is_set", mutate_match_variant)
            enum_match_rejected("finite_match_join_target", "is_set", mutate_match_target)
            enum_match_rejected("finite_match_edge_arity", "is_set", mutate_match_case_arity)
            enum_match_rejected("finite_payload_declared_type", "ignore_set", mutate_payload_declared_type)
            enum_construction_case = {
                "native_project_valid": constructor_native["valid"] and match_native["valid"],
                "native_value_ssa_valid": constructor_native["value_ssa_valid"] and match_native["value_ssa_valid"],
                "stage0_project_valid": constructor_oracle["valid"] and match_oracle["valid"],
                "verified_functions": constructor_ssa["verified_function_count"] + match_ssa["verified_function_count"],
                "enum_constructor_instructions": len(enum_instructions),
                "sequence_repeat_instructions": len(repeat_instructions),
                "finite_match_switches": len(enum_switches),
                "payload_extractions": len(payload_instructions),
                "covered": ["zero payload", "zero payload with braces", "one payload", "multiple ordered fields", "nested nominal payload", "exact sequence repeat", "finite match payload binding", "wildcard payload binding", "unused payload binding", "arm result joins", "constructor inside match arm", "heterogeneous-type env across match edges"],
                "corruption_rejections": ["invalid enum identity", "unknown variant", "arity", "payload type", "finite match variant", "join target", "edge value", "payload declared type", "repeat count", "repeat operand type"],
            }
            enum_construction_case["requests"] = enum_probe.requests
            enum_construction_case["result_sha256"] = enum_probe.digest.hexdigest()
            enum_construction_case["native_execution_steps_total"] = sum(enum_probe.steps)
            enum_probe.close()

            # Integer scalar matches lower to verified SSA switches with
            # pattern, default-target, and edge checks. Small request again:
            # each project request is independently installed.
            scalar_source = (
                "mncs 0.18; module demo.scalar_matches; "
                "fn classify(value: u64) -> (r: u64) { return match value { 0 => 10, 1 => 20, _ => 30 }; } "
                "fn negated(value: i64) -> (r: i64) { return match value { -5 => 1, 0 => 0, _ => 2 }; } "
                "fn flag(value: u64) -> (r: bool) { return match value { 7 => true, _ => false }; } "
                "fn doubled(value: u64) -> (r: u64) { let twice: u64 = value + value; return match twice { 2 => 200, _ => twice }; } "
                "fn called(value: u64) -> (r: u64) { return match value { 3 => classify(value), _ => 0 }; } "
                "fn mixed_count(value: u64, flag: bool) -> (r: u64) { return match value { 0 => 10, _ => 20 }; }"
            )
            assert len(scalar_source.encode()) <= 1024, len(scalar_source.encode())
            scalar_probe = Probe()
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-scalar-match.mncs").write_text(scalar_source)
            scalar_native = scalar_probe.native(request_value(identities, discover_sources(project_root)))
            scalar_wire_project = copy.deepcopy(scalar_probe.last_response["returned"][0])
            scalar_modules = flist(scalar_native["modules"])
            scalar_module = next(module for module in scalar_modules if module["source_index"] == 0)
            scalar_ssa = scalar_module["value_ssa"]
            assert scalar_native["valid"] is True, scalar_native["diagnostics"]
            assert scalar_native["value_ssa_valid"] is True, scalar_ssa
            assert scalar_ssa["valid"] is True and scalar_ssa["verified_function_count"] == 6, scalar_ssa
            scalar_oracle = probe.send({"project_oracle": {"root": scalar_source, "modules": {}}})
            assert scalar_oracle["valid"] is True, scalar_oracle
            scalar_functions = flist(scalar_ssa["functions"])
            scalar_switch_blocks = [
                (function, block) for function in scalar_functions
                for block in flist(function["blocks"])
                if block["terminator"]["$v"] == 5
            ]
            scalar_switches = [block["terminator"]["$p"] for _, block in scalar_switch_blocks]
            assert len(scalar_switches) == 6, scalar_switches
            assert all(len(flist(item["cases"])) == item["arm_count"] for item in scalar_switches)
            for item in scalar_switches:
                cases = flist(item["cases"])
                defaults = [case for case in cases if case["is_default"] is True]
                assert len(defaults) == 1, item
                assert defaults[0]["target"] == item["default_target"], item
            negated_switch = next(item for item in scalar_switches
                                  if any(case["negative"] is True for case in flist(item["cases"])))
            assert negated_switch["subject_ty"]["$v"] == 2 and negated_switch["subject_ty"]["$p"]["signed"] is True

            scalar_wire_modules = wire_flist(wire_field(scalar_wire_project, "modules"))
            scalar_wire_module = next(module for module in scalar_wire_modules
                                      if wire_number(wire_field(module, "source_index")) == 0)
            scalar_wire_ssa = wire_field(scalar_wire_module, "value_ssa")
            scalar_wire_functions = wire_flist(wire_field(scalar_wire_ssa, "functions"))

            def scalar_match_rejected(name, source_function, mutate):
                candidate = copy.deepcopy(next(
                    function for function in scalar_wire_functions
                    if identity_text(decode(wire_field(function, "identity"))).endswith(f"::{source_function}")
                ))
                mutate(candidate)
                accepted = verify_ssa_function(scalar_probe, scalar_source, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}

            def scalar_switch_term(function):
                return next(wire_field(block, "terminator")
                            for block in wire_flist(wire_field(function, "blocks"))
                            if wire_field(block, "terminator")["finite"]["discriminant"] == 5)

            def mutate_scalar_pattern(function):
                term = scalar_switch_term(function)
                payload = {key: value for key, value in term["finite"]["payload"]}
                cases = wire_flist(payload["cases"])
                set_wire_field(cases[0], "value", copy.deepcopy(wire_field(cases[1], "value")))

            def mutate_scalar_default_target(function):
                term = scalar_switch_term(function)
                set_wire_variant_field(term, "default_target", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_scalar_case_arity(function):
                term = scalar_switch_term(function)
                payload = {key: value for key, value in term["finite"]["payload"]}
                case = wire_flist(payload["cases"])[0]
                first_argument = wire_flist(wire_field(case, "arguments"))[0]
                set_wire_field(first_argument, "value", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_scalar_default_missing(function):
                term = scalar_switch_term(function)
                payload = {key: value for key, value in term["finite"]["payload"]}
                case = next(case for case in wire_flist(payload["cases"])
                            if decode(wire_field(case, "is_default")) is True)
                set_wire_field(case, "is_default", {"boolean": {"value": False}})

            scalar_match_rejected("scalar_match_pattern", "classify", mutate_scalar_pattern)
            scalar_match_rejected("scalar_match_default_target", "classify", mutate_scalar_default_target)
            scalar_match_rejected("scalar_match_edge_arity", "flag", mutate_scalar_case_arity)
            scalar_match_rejected("scalar_match_default_missing", "flag", mutate_scalar_default_missing)
            scalar_match_case = {
                "native_project_valid": scalar_native["valid"],
                "native_value_ssa_valid": scalar_native["value_ssa_valid"],
                "stage0_project_valid": scalar_oracle["valid"],
                "verified_functions": scalar_ssa["verified_function_count"],
                "scalar_match_switches": len(scalar_switches),
                "covered": ["multi-pattern u64 match", "negative i64 pattern", "bool result", "let-bound subject referenced in arm", "call inside arm", "arm result joins", "heterogeneous-type env across match edges"],
                "corruption_rejections": ["duplicate pattern", "default target", "edge value", "missing default"],
            }
            scalar_match_case["requests"] = scalar_probe.requests
            scalar_match_case["result_sha256"] = scalar_probe.digest.hexdigest()
            scalar_match_case["native_execution_steps_total"] = sum(scalar_probe.steps)
            scalar_probe.close()

            # Record field projections lower to verified SSA instructions
            # with declared-field, identity, and provenance checks.
            projection_source = (
                "mncs 0.18; module demo.projection; "
                "record Point { x: u64, y: u64 } "
                "record Wrap { flag: bool, point: Point } "
                "fn get_x(p: Point) -> (r: u64) { return p.x; } "
                "fn get_y(p: Point) -> (r: u64) { return p.y; } "
                "fn get_flag(w: Wrap) -> (r: bool) { return w.flag; } "
                "fn nested(w: Wrap) -> (r: u64) { let inner: Point = w.point; return inner.y; } "
                "fn in_match(p: Point, c: u64) -> (r: u64) { return match c { 0 => p.x, _ => p.y }; }"
            )
            assert len(projection_source.encode()) <= 1024, len(projection_source.encode())
            projection_probe = Probe()
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-projection.mncs").write_text(projection_source)
            projection_native = projection_probe.native(request_value(identities, discover_sources(project_root)))
            projection_wire_project = copy.deepcopy(projection_probe.last_response["returned"][0])
            projection_modules = flist(projection_native["modules"])
            projection_module = next(module for module in projection_modules if module["source_index"] == 0)
            projection_ssa = projection_module["value_ssa"]
            assert projection_native["valid"] is True, projection_native["diagnostics"]
            assert projection_native["value_ssa_valid"] is True, projection_ssa
            assert projection_ssa["valid"] is True and projection_ssa["verified_function_count"] == 5, projection_ssa
            projection_oracle = probe.send({"project_oracle": {"root": projection_source, "modules": {}}})
            assert projection_oracle["valid"] is True, projection_oracle
            projection_functions = flist(projection_ssa["functions"])

            def projection_instructions(function_name):
                function = next(item for item in projection_functions
                                if identity_text(item["identity"]).endswith(f"::{function_name}"))
                return [instruction for block in flist(function["blocks"])
                        for instruction in flist(block["instructions"])
                        if instruction["kind"] == 8]

            projection_operators = {
                name: [instruction["operator"] for instruction in projection_instructions(name)]
                for name in ("get_x", "get_y", "get_flag", "nested", "in_match")
            }
            assert projection_operators == {
                "get_x": [0], "get_y": [1], "get_flag": [0],
                "nested": [1, 1], "in_match": [0, 1],
            }, projection_operators

            projection_wire_modules = wire_flist(wire_field(projection_wire_project, "modules"))
            projection_wire_module = next(module for module in projection_wire_modules
                                          if wire_number(wire_field(module, "source_index")) == 0)
            projection_wire_ssa = wire_field(projection_wire_module, "value_ssa")
            projection_wire_functions = wire_flist(wire_field(projection_wire_ssa, "functions"))

            def projection_rejected(name, source_function, mutate):
                candidate = copy.deepcopy(next(
                    function for function in projection_wire_functions
                    if identity_text(decode(wire_field(function, "identity"))).endswith(f"::{source_function}")
                ))
                mutate(candidate)
                accepted = verify_ssa_function(projection_probe, projection_source, candidate)
                assert accepted is False, {"case": name, "accepted": accepted}

            def project_instruction(function):
                return next(inst for block in wire_flist(wire_field(function, "blocks"))
                            for inst in wire_flist(wire_field(block, "instructions"))
                            if wire_number(wire_field(inst, "kind")) == 8)

            def mutate_project_index(function):
                set_wire_field(project_instruction(function), "operator",
                               {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            def mutate_project_identity_invalid(function):
                identity = wire_field(project_instruction(function), "nominal_identity")
                set_wire_field(identity, "valid", {"boolean": {"value": False}})

            def mutate_project_identity_mismatch(function):
                instructions = [inst for block in wire_flist(wire_field(function, "blocks"))
                                for inst in wire_flist(wire_field(block, "instructions"))
                                if wire_number(wire_field(inst, "kind")) == 8]
                assert len(instructions) == 2, len(instructions)
                first = copy.deepcopy(wire_field(instructions[0], "nominal_identity"))
                set_wire_field(instructions[0], "nominal_identity",
                               copy.deepcopy(wire_field(instructions[1], "nominal_identity")))
                set_wire_field(instructions[1], "nominal_identity", first)

            def mutate_project_provenance(function):
                instruction = project_instruction(function)
                end = wire_number(wire_field(instruction, "binding_end"))
                set_wire_field(instruction, "binding_end",
                               {"integer": {"type": {"bits": 64, "signed": False}, "value": end + 1}})

            def mutate_project_result_type(function):
                instruction = project_instruction(function)
                base_ref = wire_flist(wire_field(instruction, "inputs"))[0]
                base_value = next(value for value in wire_flist(wire_field(function, "values"))
                                  if wire_number(wire_field(value, "id")) == wire_number(wire_field(base_ref, "value")))
                record_type = copy.deepcopy(wire_field(base_value, "ty"))
                set_wire_field(instruction, "ty", record_type)
                result_id = wire_number(wire_field(instruction, "result"))
                result_value = next(value for value in wire_flist(wire_field(function, "values"))
                                    if wire_number(wire_field(value, "id")) == result_id)
                set_wire_field(result_value, "ty", copy.deepcopy(record_type))

            def mutate_project_base(function):
                instruction = project_instruction(function)
                first_input = wire_flist(wire_field(instruction, "inputs"))[0]
                set_wire_field(first_input, "value", {"integer": {"type": {"bits": 64, "signed": False}, "value": 999}})

            projection_rejected("project_index", "get_x", mutate_project_index)
            projection_rejected("project_identity_invalid", "get_x", mutate_project_identity_invalid)
            projection_rejected("project_identity_mismatch", "nested", mutate_project_identity_mismatch)
            projection_rejected("project_provenance", "get_y", mutate_project_provenance)
            projection_rejected("project_result_type", "get_flag", mutate_project_result_type)
            projection_rejected("project_base", "get_x", mutate_project_base)
            projection_case = {
                "native_project_valid": projection_native["valid"],
                "native_value_ssa_valid": projection_native["value_ssa_valid"],
                "stage0_project_valid": projection_oracle["valid"],
                "verified_functions": projection_ssa["verified_function_count"],
                "projection_operators": projection_operators,
                "covered": ["first/second field index", "bool field", "record-typed field", "let-bound record", "projection in match arms"],
                "corruption_rejections": ["index", "identity invalid", "identity mismatch", "provenance", "result type", "base"],
            }
            projection_case["requests"] = projection_probe.requests
            projection_case["result_sha256"] = projection_probe.digest.hexdigest()
            projection_case["native_execution_steps_total"] = sum(projection_probe.steps)
            projection_probe.close()

            # Compiler operations reach proof, typed TOp, CFG, kind-9
            # SSA, and SSA verification; see check_operations.
            operation_case = check_operations(project_root, probe, identities)

            # Native project resolution follows transitive `use` edges in the
            # same source snapshot, even when the filesystem walk created the
            # modules in reverse path order.
            middle = "mncs 0.18; module demo.mid; use demo.dep as dep; fn middle() -> (r: u64) { return 7; }"
            root_transitive = "mncs 0.18; module demo.transitive; use demo.mid as mid; fn main() -> (r: u64) { return 1; }"
            root_transitive += " " * (1024 - len(root_transitive.encode()))
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-dep.mncs").write_text(dependency)
            (project_root / "c-mid.mncs").write_text(middle)
            (project_root / "b-root.mncs").write_text(root_transitive)
            transitive_sources = discover_sources(project_root)
            transitive_request = request_value(identities, transitive_sources)
            transitive = probe.native(transitive_request)
            assert transitive["valid"] is True, transitive["diagnostics"]
            transitive_modules = flist(transitive["modules"])
            transitive_imports = flist(transitive["imports"])
            assert len(transitive_modules) == 3
            assert len(transitive_imports) == 2
            assert [(item["source_index"], item["target_index"]) for item in transitive_imports] == [(1, 2), (2, 0)]
            transitive_oracle = probe.send({"project_oracle": {
                "root": root_transitive,
                "modules": {"demo.mid": middle, "demo.dep": dependency},
            }})
            assert transitive_oracle["valid"] is True, transitive_oracle["diagnostics"]
            assert transitive_oracle["program"] is not None
            assert len(transitive_oracle["program"]["functions"]) == 3
            return {
                "requests": probe.requests,
                "result_sha256": probe.digest.hexdigest(),
                "native_execution_steps_total": sum(probe.steps),
                "native_execution_steps_max": max(probe.steps),
                "execution_mode": "retained_cranelift" if execution_status["retained_sessions"] else "reference_interpreter",
                "requested_execution_backend": os.environ.get("MNCS_PROBE_BACKEND"),
                "retained_execution_sessions": execution_status["retained_sessions"],
                "snapshot_source_count": len(discovered),
                "snapshot_bytes": sum(len(text) for _, _, text in discovered),
                "former_unit_boundary_crossed": len(root.encode()) > 256,
                "current_profile_source_nat_ceiling": 1024,
                "native_modules": native_flow,
                "native_imports": [{"source_index": item["source_index"], "target_index": item["target_index"], "has_alias": item["has_alias"]} for item in imports],
                "stage0_module_resolutions": len(oracle["module_resolutions"]),
                "stage0_linked_functions": len(oracle["program"]["functions"]),
                "stage0_ssa_functions": len(oracle["ssa"]["functions"]),
                "native_signatures_matched_stage0": signature_matches,
                "transitive_import_links": [(item["source_index"], item["target_index"]) for item in transitive_imports],
                "stage0_transitive_linked_functions": len(transitive_oracle["program"]["functions"]),
                "resolved_imported_call": imported_call_identity,
                "multi_module_value_ssa": multi_module_value_ssa,
                "imported_argument_check": imported_argument_check,
                "resolution_edge_cases": {
                    "same_short_name_owners": namespace_collision_case,
                    "missing_import": missing_import_case,
                    "duplicate_module_definitions": duplicate_module_case,
                    "missing_imported_member": missing_member_case,
                    "parse_failure_explains_itself": parse_failure_case,
                    "imported_record_finite_and_nested_nominal_types": nominal_type_case,
                },
                "enum_construction_value_ssa": enum_construction_case,
                "scalar_match_value_ssa": scalar_match_case,
                "projection_value_ssa": projection_case,
                "compiler_operation_value_ssa": operation_case,
                "deterministic_repetitions": 2,
            }
        finally:
            if enum_probe is not None:
                enum_probe.close()
            if scalar_probe is not None:
                scalar_probe.close()
            if projection_probe is not None:
                projection_probe.close()
            probe.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "operations":
        started = time.monotonic()
        focused_probe = Probe()
        try:
            focused_identities = identity_map(focused_probe)
            with tempfile.TemporaryDirectory(prefix="mncs-project-operations-") as directory:
                focused_case = check_operations(Path(directory), focused_probe, focused_identities)
        finally:
            focused_probe.close()
        focused_case["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(focused_case, indent=2))
        sys.exit(0)
    started = time.monotonic()
    result = run()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads(Path("mncs-language.lock.json").read_text())["revision"],
        "stage0_reference_mode": os.environ.get("MNCS_PROBE_REFERENCE_MODE", "locked"),
        "source_profile": "0.18",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "ordered current-profile project snapshot, native module/import/member resolution, canonical callable identity, scalar imported-signature proof, and verified value SSA; a multi-module imported call crosses a control-flow merge and is compared with locked Rust Stage-0 body/SSA facts",
        "imported_signature_scope": {
            "supported": "scalar, effect-free imported callable signatures with argument/result proof and compiler-emitted canonical Stage-0 callable identity; nominal imported type ownership and nested nominal resolution reach verified SSA per campaign-20261001-imported-nominal-ssa.json",
            "remaining": ["imported effect/capability identity and coverage"],
        },
        "project_fingerprint": {
            "authenticated": False,
            "authority": "host-supplied advisory metadata; native validity does not depend on an unchecked digest",
        },
        **result,
    }
    artifact_root = Path(os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR", ROOT / "evidence"))
    artifact_root.mkdir(parents=True, exist_ok=True)
    out = artifact_root / f"campaign-{CAMPAIGN_ID}-project-results.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
