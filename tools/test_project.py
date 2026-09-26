#!/usr/bin/env python3
"""Native bounded project snapshot vs current Stage-0 module resolver.

Python only builds typed request values and compares compiler outputs. The
project/module/import interpretation remains in MNCS or the Rust oracle.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.project.v1"
os.environ.setdefault("MNCS_PROBE_BACKEND", "cranelift")
os.environ.setdefault("MNCS_PROBE_GENERIC_SEEDS", json.dumps([
    {"module": "mncs.compiler.project.v1", "function": "compile_project",
     "type_arguments": [
         {"kind": "nat", "value": 1},
         {"kind": "nat", "value": 1024},
     ]},
    {"module": "mncs.compiler.project.v1", "function": "compile_project",
     "type_arguments": [
         {"kind": "nat", "value": 2},
         {"kind": "nat", "value": 1024},
     ]},
    {"module": "mncs.compiler.project.v1", "function": "compile_project",
     "type_arguments": [
         {"kind": "nat", "value": 3},
         {"kind": "nat", "value": 1024},
     ]},
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

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"Stage-0 probe terminated: {self.proc.poll()}"
        result = json.loads(line)
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


def source_value(identities, source_id, path):
    return record(identities, "ProjectSource", {
        "source_id": byte_sequence(source_id.encode()),
        "source_path": byte_sequence(path.encode()),
    })


def request_value(identities, sources):
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
            {"kind": "nat", "value": len(sources)},
            # Per-file values remain exact length. N is their common Profile
            # 0.18 sequence ceiling, not padding or semantic source content.
            {"kind": "nat", "value": 1024},
        ],
        "step_budget": 8_000_000,
    }


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
        try:
            execution_status = probe.send({"execution_status": True})
            if os.environ.get("MNCS_PROBE_BACKEND") == "cranelift":
                assert execution_status["backend"] == "cranelift", execution_status
                assert execution_status["retained_sessions"] == 1, execution_status
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

            # The project resolver supplies the imported, scalar signature to
            # native proof and the resulting typed call retains its defining
            # source index and declaration span as an artifact-bound identity.
            imported_caller = "mncs 0.18; module demo.imported_call; use demo.dep as dep; fn main() -> (r: u64) { return dep.answer(42); }"
            (project_root / "b-root.mncs").write_text(imported_caller)
            imported_sources = discover_sources(project_root)
            imported_native = probe.native(request_value(identities, imported_sources))
            imported_modules = flist(imported_native["modules"])
            imported_root = next(module for module in imported_modules if module["source_index"] == 1)
            imported_obligations = flist(imported_root["flow"]["proof"]["obls"])
            imported_tops = flist(imported_root["flow"]["proof"]["tops"])
            imported_ops = [op for body in imported_tops for op in flist(body)]
            imported_call_ops = [op["$p"] for op in imported_ops if "owner" in op.get("$p", {})]
            imported_oracle = probe.send({"project_oracle": {
                "root": imported_caller,
                "modules": {"demo.dep": dependency},
            }})
            assert imported_native["import_count"] == 1
            assert imported_native["valid"] is True, imported_native["diagnostics"]
            assert imported_root["flow"]["proof"]["ok"] is True, imported_obligations
            assert len(imported_call_ops) == 1, imported_ops
            resolved_call = imported_call_ops[0]
            assert resolved_call["owner"] == 1, resolved_call
            assert resolved_call["argc"] == 1, resolved_call
            dep_text = next(text for source_id, _, text in imported_sources if source_id == "a-dep.mncs")
            resolved_member = dep_text[resolved_call["decl_start"]:resolved_call["decl_end"]].decode()
            assert resolved_member == "answer", resolved_call
            assert imported_oracle["valid"] is True, imported_oracle["diagnostics"]
            assert imported_oracle["program"] is not None
            assert len(imported_oracle["program"]["functions"]) == 2
            assert imported_oracle["ssa"] is not None
            stage0_callable = next(
                function for function in imported_oracle["program"]["functions"]
                if function["home_module"] == "demo.dep" and function["name"] == resolved_member
            )
            imported_call_identity = {
                "native_project_valid": imported_native["valid"],
                "native_import_count": imported_native["import_count"],
                "native_root_proof_ok": imported_root["flow"]["proof"]["ok"],
                "native_callable_owner_source_index": resolved_call["owner"] - 1,
                "native_callable_member": resolved_member,
                "native_argument_count": resolved_call["argc"],
                "stage0_callable_identity": stage0_callable["identity"],
                "identity_binding": "native source-index plus defining declaration span maps to the Stage-0 home-module/name identity",
                "stage0_valid": imported_oracle["valid"],
                "stage0_diagnostics": imported_oracle["diagnostics"],
                "stage0_linked_functions": len(imported_oracle["program"]["functions"]),
                "stage0_ssa_functions": len(imported_oracle["ssa"]["functions"]),
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

            # Distinguish imported short names by their resolved source owner,
            # including a local declaration with the same name. This catches
            # accidental text-only dispatch when two imports export `answer`.
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
                    if "owner" not in payload:
                        continue
                    owner = payload["owner"]
                    owner_text = names_root.encode() if owner == 0 else names_sources[owner - 1][2]
                    member = owner_text[payload["decl_start"]:payload["decl_end"]].decode()
                    names_calls.append((owner, member))
            names_oracle = probe.send({"project_oracle": {
                "root": names_root,
                "modules": {"demo.dep": dependency, "demo.flags": flags},
            }})
            assert names_native["valid"] is True, names_native["diagnostics"]
            assert names_root_module["flow"]["proof"]["ok"] is True
            assert set(names_calls) == {(0, "answer"), (1, "answer"), (2, "answer")}, names_calls
            assert names_oracle["valid"] is True, names_oracle["diagnostics"]
            namespace_collision_case = {
                "native_valid": names_native["valid"],
                "stage0_valid": names_oracle["valid"],
                "same_short_name_owners": sorted([list(item) for item in set(names_calls)]),
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

            nominal_types = (
                "mncs 0.18; module demo.nominal; record Inner { value: u64 } "
                "record Outer { inner: Inner } enum Choice { Some { value: u64 }, None } "
                "fn echo_record(value: Outer) -> (r: Outer) { return value; } "
                "fn echo_choice(value: Choice) -> (r: Choice) { return value; }"
            )
            nominal_root = (
                "mncs 0.18; module demo.nominal_user; use demo.nominal as types; "
                "fn record_call(value: types.Outer) -> (r: types.Outer) { return types.echo_record(value); } "
                "fn finite_call(value: types.Choice) -> (r: types.Choice) { return types.echo_choice(value); }"
            )
            for path in project_root.glob("*.mncs"):
                path.unlink()
            (project_root / "a-nominal.mncs").write_text(nominal_types)
            (project_root / "b-root.mncs").write_text(nominal_root)
            nominal_sources = discover_sources(project_root)
            nominal_native = probe.native(request_value(identities, nominal_sources))
            nominal_root_module = next(
                module for module in flist(nominal_native["modules"])
                if module["source_index"] == 1
            )
            nominal_oracle = probe.send({"project_oracle": {
                "root": nominal_root,
                "modules": {"demo.nominal": nominal_types},
            }})
            assert nominal_oracle["valid"] is True, nominal_oracle
            nominal_type_case = {
                "native_valid": nominal_native["valid"],
                "native_diagnostics": flist(nominal_native["diagnostics"]),
                "native_proof_obligations": flist(nominal_root_module["flow"]["proof"]["obls"]),
                "stage0_valid": nominal_oracle["valid"],
                "stage0_record_and_finite_types_valid": True,
                "coverage_gap": not nominal_native["valid"],
            }

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
                "imported_argument_check": imported_argument_check,
                "resolution_edge_cases": {
                    "same_short_name_owners": namespace_collision_case,
                    "missing_import": missing_import_case,
                    "duplicate_module_definitions": duplicate_module_case,
                    "missing_imported_member": missing_member_case,
                    "imported_record_finite_and_nested_nominal_types": nominal_type_case,
                },
                "deterministic_repetitions": 2,
            }
        finally:
            probe.close()


if __name__ == "__main__":
    started = time.monotonic()
    result = run()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads(Path("mncs-language.lock.json").read_text())["revision"],
        "stage0_reference_mode": os.environ.get("MNCS_PROBE_REFERENCE_MODE", "locked"),
        "source_profile": "0.18",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "ordered current-profile source snapshot, native module/import/member resolution, scalar imported-signature proof and typed CFG; the tested successful and negative imported calls are compared with locked Rust Stage-0",
        "imported_signature_scope": {
            "supported": "scalar, effect-free imported callable signatures with argument/result proof and snapshot-bound callable identity",
            "remaining": ["nominal imported type ownership and nested type resolution", "imported effect/capability identity and coverage"],
        },
        **result,
    }
    out = ROOT / "evidence" / "campaign-20260925-project-results.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
