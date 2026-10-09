#!/usr/bin/env python3
"""Compare target-only project lowering with whole-project lowering."""

import copy
import json
import os
from pathlib import Path

import test_project as project
from probe_compiler_module_frontier import (
    _load_sources,
    _module_name_from_source,
    _module_summary,
    _seed_target_specialization,
)


ROOT = Path(__file__).resolve().parents[1]
MODULES = ["source", "lexer", "parser", "kernel"]


def _target_request(request, source_index):
    targeted = copy.deepcopy(request)
    targeted["target"] = {
        "module": project.PROJECT_MODULE,
        "function": "compile_project_target",
    }
    targeted["arguments"].append(project.integer(source_index))
    return targeted


def _export_case(probe, identities, dummy_count, requested_name="late", target_first=False):
    dummies = "\n".join(
        f"fn d{i:04}(x: u64) -> (r: u64) {{ return x; }}"
        for i in range(dummy_count)
    )
    target = "fn late(x: u64) -> (r: u64) { return x; }"
    declarations = f"{target}\n{dummies}" if target_first else f"{dummies}\n{target}"
    dependency = f"mncs 0.18; module demo.dep;\n{declarations}\n"
    root = (
        "mncs 0.18; module demo.root; use demo.dep as dep; "
        f"fn call(x: u64) -> (r: u64) {{ return dep.{requested_name}(x); }}\n"
    )
    sources = [
        ("a_dep.mncs", Path("a_dep.mncs"), dependency.encode()),
        ("b_root.mncs", Path("b_root.mncs"), root.encode()),
    ]
    request = _target_request(project.request_value(identities, sources), 1)
    native_response = probe.send(request)
    assert native_response.get("status") == "returned", native_response
    native = project.decode(native_response["returned"][0])
    oracle = probe.send({"project_oracle": {
        "root": root,
        "modules": {"demo.dep": dependency},
    }})
    return native, oracle, root


def _same_name_two_provider_case(probe, identities):
    first = "mncs 0.18; module demo.alpha; fn answer(x: u64) -> (r: u64) { return x; }"
    second = "mncs 0.18; module demo.beta; fn answer(x: u64) -> (r: u64) { return x; }"
    root = """mncs 0.18; module demo.root;
use demo.alpha as a;
use demo.beta as b;
fn alpha_first(x: u64) -> (r: u64) { return a.answer(x); }
fn beta_first(x: u64) -> (r: u64) { return b.answer(x); }
fn alpha_second(x: u64) -> (r: u64) { return a.answer(x); }
fn beta_second(x: u64) -> (r: u64) { return b.answer(x); }
"""
    sources = [
        ("a-alpha.mncs", Path("a-alpha.mncs"), first.encode()),
        ("b-beta.mncs", Path("b-beta.mncs"), second.encode()),
        ("c-root.mncs", Path("c-root.mncs"), root.encode()),
    ]
    request = _target_request(project.request_value(identities, sources), 2)
    native_response = probe.send(request)
    assert native_response.get("status") == "returned", native_response
    native = project.decode(native_response["returned"][0])
    oracle = probe.send({"project_oracle": {
        "root": root,
        "modules": {"demo.alpha": first, "demo.beta": second},
    }})
    assert native["valid"] and native["value_ssa_valid"], native
    assert oracle.get("valid"), oracle
    root_module = next(
        module for module in project.flist(native["modules"])
        if module["source_index"] == 2
    )
    proof = root_module["flow"]["proof"]
    native_calls = [
        project.identity_text(op["$p"]["identity"])
        for body in project.flist(proof["tops"])
        for op in project.flist(body)
        if "identity" in op.get("$p", {})
    ]
    stage0_by_owner = {
        (function.get("home_module"), function.get("name")): function.get("identity")
        for function in oracle["program"]["functions"]
    }
    expected_calls = [
        stage0_by_owner[("demo.alpha", "answer")],
        stage0_by_owner[("demo.beta", "answer")],
        stage0_by_owner[("demo.alpha", "answer")],
        stage0_by_owner[("demo.beta", "answer")],
    ]
    assert len(native_calls) == 4, native_calls
    assert sorted(native_calls) == sorted(expected_calls), (native_calls, expected_calls)
    assert len(set(expected_calls)) == 2, expected_calls
    return {
        "native_project_valid": native["valid"],
        "native_ssa_valid": native["value_ssa_valid"],
        "stage0_valid": oracle["valid"],
        "same_member_name_provider_count": 2,
        "native_import_call_count": len(native_calls),
        "distinct_provider_callable_identity_count": len(set(native_calls)),
        "all_native_identities_match_stage0": True,
    }


def _imported_enum_case(probe, identities, wrong_qualifier=False):
    dependency = """mncs 0.18; module demo.dep;
enum FnList { DNil, DCons { head: u64, tail: u64 } }
enum Other { DNil, DCons { head: u64, tail: u64 } }
"""
    nil = "dep.Other.DNil" if wrong_qualifier else "dep.FnList.DNil"
    root = f"""mncs 0.18; module demo.root; use demo.dep as dep;
fn inspect(x: dep.FnList) -> (r: u64) {{
    return match x {{
        {nil} => 0,
        dep.FnList.DCons {{ head: h, tail: t }} => match t {{ 1 => h, _ => t }}
    }};
}}
fn ignore_payload(x: dep.FnList) -> (r: u64) {{
    return match x {{
        dep.FnList.DNil => 0,
        dep.FnList.DCons {{ .. }} => 1
    }};
}}
fn bind_one_payload(x: dep.FnList) -> (r: u64) {{
    return match x {{
        dep.FnList.DNil => 0,
        dep.FnList.DCons {{ head: _, tail: t }} => t
    }};
}}
"""
    sources = [
        ("a_dep.mncs", Path("a_dep.mncs"), dependency.encode()),
        ("b_root.mncs", Path("b_root.mncs"), root.encode()),
    ]
    request = _target_request(project.request_value(identities, sources), 1)
    native_response = probe.send(request)
    assert native_response.get("status") == "returned", native_response
    native = project.decode(native_response["returned"][0])
    oracle = probe.send({"project_oracle": {
        "root": root,
        "modules": {"demo.dep": dependency},
    }})
    return native, oracle, root


def run():
    _seed_target_specialization()
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        if os.environ.get("MNCS_PROBE_BACKEND") == "cranelift":
            assert execution["backend"] == "cranelift", execution
        sources = _load_sources(MODULES)
        identities = project.identity_map(probe)
        request = project.request_value(identities, sources)
        target_stem = MODULES[-1]
        target_index = next(i for i, (source_id, _, _) in enumerate(sources)
                            if Path(source_id).stem == target_stem)
        target_response = probe.send(_target_request(request, target_index))
        assert target_response.get("status") == "returned", target_response
        target = project.decode(target_response["returned"][0])
        assert target["valid"] and target["value_ssa_valid"], target
        target_modules = project.flist(target["modules"])
        assert len(target_modules) == 1, len(target_modules)
        source = sources[target_index][2]
        target_summary = _module_summary(target_modules[0], source)
        whole_artifact = ROOT / ".build/campaign-artifacts/compiler-facts" / (
            "campaign-20261008-compiler-module-pipeline-"
            "source-lexer-parser-kernel.json"
        )
        whole = json.loads(whole_artifact.read_text())
        expected_summary = next(item for item in whole["modules"]
                                if item["source_id"] == f"{target_stem}.mncs")
        assert target_summary == expected_summary, (target_summary, expected_summary)
        assert target["source_count"] == whole["compiler_source_count"]
        assert target["module_count"] == whole["compiler_source_count"]

        missing_response = probe.send(_target_request(request, 99))
        assert missing_response.get("status") == "returned", missing_response
        missing = project.decode(missing_response["returned"][0])
        assert not missing["valid"] and not missing["value_ssa_valid"], missing
        assert len(project.flist(missing["diagnostics"])) == 1, missing

        root = sources[target_index][2].decode()
        module_map = {
            _module_name_from_source(text): text.decode()
            for i, (_, _, text) in enumerate(sources) if i != target_index
        }
        oracle = probe.send({"project_oracle": {"root": root, "modules": module_map}})
        assert oracle.get("valid"), oracle

        # The imported target is declaration 1025. Both signature-table
        # construction and export lookup must continue beyond their former
        # one-chunk limit. Stage-0 is the independent authority for the same
        # source pair, and target lowering reaches verified SSA for the caller.
        late_native, late_oracle, late_root = _export_case(
            probe, identities, dummy_count=1024
        )
        assert late_native["valid"] and late_native["value_ssa_valid"], late_native
        late_modules = project.flist(late_native["modules"])
        assert len(late_modules) == 1, late_modules
        late_proof = late_modules[0]["flow"]["proof"]
        assert late_proof["ok"] and late_proof["fn_count"] == 1, late_proof
        assert late_oracle.get("valid"), late_oracle
        late_call_ops = [
            op["$p"]
            for body in project.flist(late_proof["tops"])
            for op in project.flist(body)
            if "identity" in op.get("$p", {})
        ]
        late_stage0_callable = next(
            function for function in late_oracle["program"]["functions"]
            if function.get("home_module") == "demo.dep" and function.get("name") == "late"
        )
        assert len(late_call_ops) == 1, late_call_ops
        assert project.identity_text(late_call_ops[0]["identity"]) == late_stage0_callable["identity"]

        # Collection prepends signatures, so an early declaration sits at the
        # far end of the completed signature table. This case exercises both
        # identity attachment and lookup beyond the first 1024 table entries.
        early_native, early_oracle, _ = _export_case(
            probe, identities, dummy_count=1024, target_first=True
        )
        assert early_native["valid"] and early_native["value_ssa_valid"], early_native
        assert early_oracle.get("valid"), early_oracle

        # Preserve missing-export rejection with the same late-provider shape.
        missing_native, missing_oracle, missing_root = _export_case(
            probe, identities, dummy_count=1024, requested_name="absent"
        )
        assert not missing_native["valid"] and not missing_native["value_ssa_valid"], missing_native
        assert not missing_oracle.get("valid"), missing_oracle
        missing_module = project.flist(missing_native["modules"])[0]
        missing_proof = missing_module["flow"]["proof"]
        failed = [item for item in project.flist(missing_proof["obls"])
                  if item["status"] == 1]
        assert failed and failed[0]["start"] == missing_root.index("dep.absent"), failed
        assert missing_oracle.get("diagnostics"), missing_oracle

        # Repeated spelling from different imported providers must retain
        # provider-scoped cache facts and each provider's canonical identity.
        same_name_providers = _same_name_two_provider_case(probe, identities)

        # Imported finite schemas carry variant identity and payload types
        # into native proof and SSA. The wrong qualifier must still report
        # Stage-0's MNE176 at the full multi-segment type path.
        enum_native, enum_oracle, enum_root = _imported_enum_case(
            probe, identities
        )
        assert enum_native["valid"], enum_native["diagnostics"]
        assert enum_oracle.get("valid"), enum_oracle
        enum_modules = project.flist(enum_native["modules"])
        assert len(enum_modules) == 1, enum_modules
        enum_ssa = enum_modules[0]["value_ssa"]
        if not enum_ssa["valid"]:
            def identity_text(identity):
                return bytes(identity["bytes"][:identity["len"]]).decode()

            functions = []
            for function in project.flist(enum_ssa["functions"]):
                functions.append({
                    "identity": identity_text(function["identity"]),
                    "supported": function["supported"],
                    "verified": function["verified"],
                    "blocks": [
                        {
                            "id": block["id"],
                            "lowering_ok": block["lowering_ok"],
                            "failure_kind": block["lowering_failure_kind"],
                            "instruction_kinds": [
                                instruction["kind"]
                                for instruction in project.flist(block["instructions"])
                            ],
                        }
                        for block in project.flist(function["blocks"])
                    ],
                })
            raise AssertionError({
                "valid": enum_ssa["valid"],
                "verified_function_count": enum_ssa["verified_function_count"],
                "functions": functions,
            })
        assert enum_native["value_ssa_valid"], enum_ssa
        assert enum_ssa["valid"] and enum_ssa["verified_function_count"] == 3, enum_ssa

        wrong_native, wrong_oracle, wrong_root = _imported_enum_case(
            probe, identities, wrong_qualifier=True
        )
        assert not wrong_native["valid"] and not wrong_native["value_ssa_valid"], wrong_native
        wrong_module = project.flist(wrong_native["modules"])[0]
        wrong_proof = wrong_module["flow"]["proof"]
        wrong_failed = [item for item in project.flist(wrong_proof["obls"])
                        if item["status"] == 1]
        assert wrong_failed and wrong_failed[0]["kind"] == 75, wrong_failed
        wrong_diagnostic = next(item for item in wrong_oracle["diagnostics"]
                                if item["code"] == "MNE176")
        assert wrong_failed[0]["start"] == wrong_diagnostic["span"]["start"]
        assert wrong_failed[0]["end"] == wrong_diagnostic["span"]["end"]
        return {
            "status": "passed",
            "target": target_stem,
            "whole_project_ssa_verified": expected_summary["ssa_verified_function_count"],
            "target_ssa_verified": target_summary["ssa_verified_function_count"],
            "target_matches_whole_project": True,
            "missing_target_rejected": True,
            "late_export_declaration_count": 1025,
            "late_export_native_verified_ssa": late_native["value_ssa_valid"],
            "late_export_stage0_valid": late_oracle["valid"],
            "late_export_identity_matches_stage0": True,
            "early_export_native_verified_ssa": early_native["value_ssa_valid"],
            "early_export_stage0_valid": early_oracle["valid"],
            "missing_late_export_rejected": True,
            "missing_late_export_stage0_rejected": True,
            "same_name_two_provider_cache_scope": same_name_providers,
            "imported_enum_payload_native_verified_ssa": enum_ssa["valid"],
            "imported_enum_payload_stage0_valid": enum_oracle["valid"],
            "wrong_imported_enum_qualifier_native_kind": wrong_failed[0]["kind"],
            "wrong_imported_enum_qualifier_stage0_diagnostic": wrong_diagnostic["code"],
            "wrong_imported_enum_qualifier_span_matches": True,
            "stage0_oracle_valid": oracle["valid"],
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "whole_project_artifact": str(whole_artifact.relative_to(ROOT)),
            "whole_project_result_sha256": whole["result_sha256"],
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
