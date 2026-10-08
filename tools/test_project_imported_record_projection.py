#!/usr/bin/env python3
"""Imported record field proof and verified SSA against the Stage-0 oracle."""

import json
import tempfile
from pathlib import Path

import test_project as project


DEPENDENCY = (
    "mncs 0.18; module demo.record_dep; "
    "record Inner { value: u64 } "
    "record Token { kind: u64, start: u64, inner: Inner }"
)
ROOT = (
    "mncs 0.18; module demo.record_root; use demo.record_dep as dep; "
    "fn kind(token: dep.Token) -> (r: u64) { return token.kind; } "
    "fn start(token: dep.Token) -> (r: u64) { return token.start; } "
    "fn nested(token: dep.Token) -> (r: u64) { "
    "let inner: dep.Inner = token.inner; return inner.value; }"
)
BAD_ROOT = (
    "mncs 0.18; module demo.record_root; use demo.record_dep as dep; "
    "fn missing(token: dep.Token) -> (r: u64) { return token.absent; }"
)


def _project_module(native, source_index):
    return next(
        module for module in project.flist(native["modules"])
        if module["source_index"] == source_index
    )


def _project_operator_indices(module, source, names):
    found = {}
    functions = project.flist(module["value_ssa"]["functions"])
    for name in names:
        function = next(
            item for item in functions
            if project.identity_text(item["identity"]).endswith(f"::{name}")
        )
        found[name] = [
            instruction["operator"]
            for block in project.flist(function["blocks"])
            for instruction in project.flist(block["instructions"])
            if instruction["kind"] == 8
        ]
    return found


def run():
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        with tempfile.TemporaryDirectory(prefix="mncs-imported-record-projection-") as directory:
            root = Path(directory)
            (root / "a-dep.mncs").write_text(DEPENDENCY)
            (root / "b-root.mncs").write_text(ROOT)
            native = probe.native(project.request_value(identities, project.discover_sources(root)))
            caller = _project_module(native, 1)
            oracle = probe.send({"project_oracle": {
                "root": ROOT,
                "modules": {"demo.record_dep": DEPENDENCY},
            }})
            operators = _project_operator_indices(caller, ROOT, ("kind", "start", "nested"))
            assert native["valid"] is True, native["diagnostics"]
            assert native["value_ssa_valid"] is True, caller["value_ssa"]
            assert caller["flow"]["proof"]["ok"] is True
            assert caller["value_ssa"]["verified_function_count"] == 3
            assert operators == {"kind": [0], "start": [1], "nested": [2, 0]}, operators
            assert oracle["valid"] is True, oracle["diagnostics"]
            assert oracle["program"] is not None and oracle["ssa"] is not None

            (root / "b-root.mncs").write_text(BAD_ROOT)
            rejected = probe.native(project.request_value(identities, project.discover_sources(root)))
            rejected_caller = _project_module(rejected, 1)
            rejected_oracle = probe.send({"project_oracle": {
                "root": BAD_ROOT,
                "modules": {"demo.record_dep": DEPENDENCY},
            }})
            bad_field_start = BAD_ROOT.index("absent")
            bad_field_end = bad_field_start + len("absent")
            proof = rejected_caller["flow"]["proof"]
            assert rejected["valid"] is False, rejected
            assert proof["ok"] is False, proof
            assert (proof["err_start"], proof["err_end"]) == (bad_field_start, bad_field_end), proof
            assert rejected_oracle["valid"] is False, rejected_oracle
            oracle_spans = [
                (item.get("span", {}).get("start"), item.get("span", {}).get("end"))
                for item in rejected_oracle["diagnostics"]
                if item.get("code") == "MNE162"
            ]
            assert (bad_field_start, bad_field_end) in oracle_spans, rejected_oracle["diagnostics"]

            return {
                "schema_version": 1,
                "status": "verified",
                "stage0_revision": json.loads(
                    (project.ROOT / "mncs-language.lock.json").read_text()
                )["revision"],
                "execution_backend": execution["backend"],
                "native_valid": native["valid"],
                "native_proof_ok": caller["flow"]["proof"]["ok"],
                "native_verified_functions": caller["value_ssa"]["verified_function_count"],
                "stage0_valid": oracle["valid"],
                "stage0_linked_functions": len(oracle["program"]["functions"]),
                "projection_indices": operators,
                "missing_field_rejected_by_native_and_stage0": True,
                "missing_field_span": [bad_field_start, bad_field_end],
                "negative_native_span": [proof["err_start"], proof["err_end"]],
                "negative_stage0_diagnostics": rejected_oracle["diagnostics"],
                "requests": probe.requests,
                "result_sha256": probe.digest.hexdigest(),
                "native_execution_steps_total": sum(probe.steps),
            }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
