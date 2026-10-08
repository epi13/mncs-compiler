#!/usr/bin/env python3
"""Imported enum constructor proof/SSA against the independent Stage-0 oracle."""

import json
import os
from pathlib import Path

PAGE_WIDTH = int(os.environ.get("MNCS_PROJECT_PAGE_WIDTH", "1024"))
PAGE_CAPACITY = int(os.environ.get("MNCS_PROJECT_PAGE_CAPACITY", "896"))
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
    {"module": "mncs.compiler.project.v1", "function": "compile_project_target",
     "type_arguments": [{"kind": "nat", "value": PAGE_CAPACITY},
                        {"kind": "nat", "value": PAGE_WIDTH}]},
])

import test_project as project
from test_project_target_module import _target_request
from probe_compiler_module_frontier import (
    _load_sources,
    _module_name_from_source,
    _module_summary,
)


DEPENDENCY = """mncs 0.18; module demo.dep;
enum Token { Empty, Pair { first: u64, second: u64 } }
"""


def _case(probe, identities, functions):
    root = """mncs 0.18; module demo.root; use demo.dep as dep;
""" + functions + "\n"
    sources = [
        ("a_dep.mncs", Path("a_dep.mncs"), DEPENDENCY.encode()),
        ("b_root.mncs", Path("b_root.mncs"), root.encode()),
    ]
    request = project.request_value(identities, sources, stride=PAGE_WIDTH)
    request["type_arguments"] = [
        {"kind": "nat", "value": PAGE_CAPACITY},
        {"kind": "nat", "value": PAGE_WIDTH},
    ]
    request = _target_request(request, 1)
    response = probe.send(request)
    assert response.get("status") == "returned", response
    native = project.decode(response["returned"][0])
    oracle = probe.send({"project_oracle": {
        "root": root,
        "modules": {"demo.dep": DEPENDENCY},
    }})
    return native, oracle, root


def _first_target_module(native):
    modules = project.flist(native["modules"])
    assert len(modules) == 1, modules
    return modules[0]


def _failed_obligations(module):
    proof = module["flow"]["proof"]
    return [item for item in project.flist(proof["obls"])
            if item["status"] == 1]


def _flow_frontier(probe, identities):
    modules = ["source", "lexer", "parser", "segment", "decl", "flow"]
    sources = _load_sources(modules)
    request = project.request_value(identities, sources, stride=PAGE_WIDTH)
    page_count = sum((len(text) + PAGE_WIDTH - 1) // PAGE_WIDTH
                     for _, _, text in sources)
    assert page_count <= PAGE_CAPACITY, {"page_count": page_count,
                                          "capacity": PAGE_CAPACITY}
    target_index = next(i for i, (source_id, _, _) in enumerate(sources)
                        if Path(source_id).stem == "flow")
    request["type_arguments"] = [
        {"kind": "nat", "value": PAGE_CAPACITY},
        {"kind": "nat", "value": PAGE_WIDTH},
    ]
    response = probe.send(_target_request(request, target_index))
    assert response.get("status") == "returned", response
    native = project.decode(response["returned"][0])
    assert native["valid"] and native["value_ssa_valid"], native
    target_modules = project.flist(native["modules"])
    assert len(target_modules) == 1, target_modules
    source = sources[target_index][2]
    summary = _module_summary(target_modules[0], source)
    assert summary["proof_ok"] and summary["flow_valid"] and summary["ssa_valid"], summary
    assert summary["ssa_function_count"] == summary["ssa_verified_function_count"], summary

    root = source.decode()
    module_map = {
        _module_name_from_source(text): text.decode()
        for i, (_, _, text) in enumerate(sources) if i != target_index
    }
    oracle = probe.send({"project_oracle": {"root": root, "modules": module_map}})
    assert oracle.get("valid") is True, oracle
    return {
        "selected_modules": modules,
        "page_width": PAGE_WIDTH,
        "page_capacity": PAGE_CAPACITY,
        "page_count": page_count,
        "native_summary": summary,
        "stage0_valid": oracle["valid"],
        "stage0_linked_function_count": len(oracle.get("program", {}).get("functions", [])),
        "stage0_ssa_function_count": len(oracle.get("ssa", {}).get("functions", [])),
    }


def _check_negative(native, oracle, root, field_marker):
    assert not native["valid"] and not native["value_ssa_valid"], native
    assert oracle.get("valid") is False, oracle
    module = _first_target_module(native)
    failed = _failed_obligations(module)
    diagnostics = oracle.get("diagnostics", [])
    assert failed and diagnostics, {"failed": failed, "oracle": oracle}
    first = diagnostics[0]["span"]
    assert any(item["start"] == first["start"] and item["end"] == first["end"]
               for item in failed), {"native": failed, "stage0": first,
                                     "source": root}
    if field_marker is not None:
        assert field_marker in root


def run():
    probe = project.Probe()
    try:
        print("admitting the project and SSA specialization at the project page bound", flush=True)
        execution = probe.send({"execution_status": True})
        if os.environ.get("MNCS_PROBE_BACKEND") == "cranelift":
            assert execution["backend"] == "cranelift", execution
        identities = project.identity_map(probe)

        print("checking valid imported enum constructors", flush=True)
        valid, valid_oracle, _ = _case(probe, identities, """\
fn make_empty() -> (r: dep.Token) { return dep.Token.Empty; }
fn make_pair(x: u64) -> (r: dep.Token) {
    return dep.Token.Pair { first: x, second: x };
}
""")
        if not valid["valid"] or not valid["value_ssa_valid"]:
            module = _first_target_module(valid)
            proof = module["flow"]["proof"]
            raise AssertionError({
                "native_valid": valid["valid"],
                "stage0_valid": valid_oracle.get("valid"),
                "native_diagnostics": valid.get("diagnostics"),
                "proof_ok": proof.get("ok"),
                "failed_obligations": _failed_obligations(module),
                "value_ssa": module.get("value_ssa"),
            })
        assert valid_oracle.get("valid") is True, valid_oracle
        module = _first_target_module(valid)
        assert module["flow"]["proof"]["ok"], module["flow"]["proof"]
        ssa = module["value_ssa"]
        assert ssa["valid"] and ssa["verified_function_count"] == 2, ssa

        negatives = [
            ("unknown_unit_variant", """fn bad() -> (r: dep.Token) {
    return dep.Token.Absent;
}
""", "dep.Token.Absent"),
            ("unit_variant_requires_payload", """fn bad() -> (r: dep.Token) {
    return dep.Token.Pair;
}
""", "dep.Token.Pair"),
            ("missing_field", """fn bad(x: u64) -> (r: dep.Token) {
    return dep.Token.Pair { first: x };
}
""", "first: x"),
            ("unknown_field", """fn bad(x: u64) -> (r: dep.Token) {
    return dep.Token.Pair { first: x, second: x, extra: x };
}
""", "extra: x"),
            ("duplicate_field", """fn bad(x: u64) -> (r: dep.Token) {
    return dep.Token.Pair { first: x, first: x, second: x };
}
""", "first: x, first"),
            ("wrong_field_type", """fn bad(x: u64) -> (r: dep.Token) {
    return dep.Token.Pair { first: true, second: x };
}
""", "first: true"),
        ]
        negative_results = {}
        for name, function, marker in negatives:
            print(f"checking imported enum constructor {name}", flush=True)
            native, oracle, root = _case(probe, identities, function)
            _check_negative(native, oracle, root, marker)
            negative_results[name] = {
                "native_valid": native["valid"],
                "stage0_valid": oracle.get("valid"),
                "diagnostic_span_matches": True,
            }
        if os.environ.get("MNCS_CONSTRUCTORS_ONLY") == "1":
            flow = {"status": "not-run", "reason": "constructors-only differential requested"}
        else:
            print("probing the real flow.mncs module through verified SSA", flush=True)
            flow = _flow_frontier(probe, identities)
        return {
            "status": "passed",
            "valid_imported_enum_constructors": 2,
            "verified_constructor_functions": 2,
            "negative_cases": negative_results,
            "flow_frontier": flow,
            "requests": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
