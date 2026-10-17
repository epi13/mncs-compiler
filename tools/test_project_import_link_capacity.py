#!/usr/bin/env python3
"""Keep every bounded project import link available to target closure."""

from __future__ import annotations

import json
from pathlib import Path

import test_project as project
from probe_compiler_module_frontier import _seed_target_specialization
from test_project_target_module import _target_request


def _dense_dag():
    sources = []
    modules = {}
    for index in range(5):
        module_name = f"demo.dep{index}"
        imports = "".join(
            f"use demo.dep{prior} as d{prior};\n"
            for prior in range(index)
        )
        source = (
            f"mncs 0.18; module {module_name};\n"
            f"{imports}record R{index} {{ value: u64 }}\n"
        )
        source_id = f"0{index}-dep{index}.mncs"
        sources.append((source_id, Path(source_id), source.encode()))
        modules[module_name] = source

    root = "mncs 0.18; module demo.root;\n"
    root += "".join(f"use demo.dep{index} as d{index};\n" for index in range(5))
    params = ", ".join(f"v{index}: d{index}.R{index}" for index in range(5))
    values = " + ".join(f"v{index}.value" for index in range(5))
    root += f"fn sum({params}) -> (result: u64) {{ return {values}; }}\n"
    sources.append(("zz-root.mncs", Path("zz-root.mncs"), root.encode()))
    return sources, modules, root


def _repeated_import_source(import_count: int) -> tuple[list, str]:
    dependency = "mncs 0.18; module demo.dep; record R { value: u64 }"
    root = "mncs 0.18; module demo.root;\n"
    root += "".join(
        f"use demo.dep as d{index};\n" for index in range(import_count)
    )
    root += "fn main() -> (result: u64) { return 1; }\n"
    sources = [
        ("00-dep.mncs", Path("00-dep.mncs"), dependency.encode()),
        ("zz-root.mncs", Path("zz-root.mncs"), root.encode()),
    ]
    return sources, root


def run():
    _seed_target_specialization()
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        sources, modules, root = _dense_dag()
        target_index = len(sources) - 1
        request = _target_request(
            project.request_value(identities, sources), target_index,
        )
        response = probe.send(request)
        assert response.get("status") == "returned", response
        native = project.decode(response["returned"][0])
        imports = project.flist(native["imports"])
        expected = {
            (source_index, target_index)
            for source_index in range(1, 5)
            for target_index in range(source_index)
        } | {(5, target_index) for target_index in range(5)}
        observed = {
            (item["source_index"], item["target_index"])
            for item in imports
        }
        assert native["valid"] and native["value_ssa_valid"], native
        assert native["import_count"] == len(expected) == 15, native
        assert len(imports) == native["import_count"], {
            "reported_import_count": native["import_count"],
            "retained_import_links": len(imports),
        }
        assert observed == expected, {"observed": observed, "expected": expected}

        target_module = next(
            item for item in project.flist(native["modules"])
            if item["source_index"] == target_index
        )
        assert target_module["flow"]["proof"]["ok"] is True
        assert target_module["value_ssa"]["valid"] is True
        assert target_module["value_ssa"]["verified_function_count"] == 1

        oracle = probe.send({"project_oracle": {"root": root, "modules": modules}})
        assert oracle.get("valid") is True, oracle
        assert oracle.get("program") is not None and oracle.get("ssa") is not None, oracle

        # Import traversal admits up to the bounded 64-use limit, independent
        # of the number of loaded modules.
        many_sources, many_root = _repeated_import_source(64)
        many_request = _target_request(
            project.request_value(identities, many_sources), 1,
        )
        many_response = probe.send(many_request)
        assert many_response.get("status") == "returned", many_response
        many_native = project.decode(many_response["returned"][0])
        many_imports = project.flist(many_native["imports"])
        assert many_native["valid"] and many_native["value_ssa_valid"], many_native
        assert many_native["import_count"] == len(many_imports) == 64, many_native
        many_oracle = probe.send({"project_oracle": {
            "root": many_root,
            "modules": {"demo.dep": "mncs 0.18; module demo.dep; record R { value: u64 }"},
        }})
        assert many_oracle.get("valid") is True, many_oracle

        # A 65th declaration exceeds the per-module bound. Fail at its source
        # span rather than accepting truncation.
        over_sources, over_root = _repeated_import_source(65)
        over_request = _target_request(
            project.request_value(identities, over_sources), 1,
        )
        over_response = probe.send(over_request)
        assert over_response.get("status") == "returned", over_response
        over_native = project.decode(over_response["returned"][0])
        over_imports = project.flist(over_native["imports"])
        over_diagnostics = project.flist(over_native["diagnostics"])
        assert not over_native["valid"], over_native
        assert not over_native["value_ssa_valid"], over_native
        assert over_native["import_count"] == len(over_imports) == 64, over_native
        assert len(over_diagnostics) == 1, over_diagnostics
        diagnostic = over_diagnostics[0].get("$p", {})
        expected_start = over_root.rfind("use demo.dep as d64;") + len("use ")
        assert diagnostic.get("source_index") == 1, over_diagnostics
        assert diagnostic.get("limit") == 64, over_diagnostics
        assert diagnostic.get("start") == expected_start, over_diagnostics
        assert diagnostic.get("end") == expected_start + len("demo.dep"), over_diagnostics
        over_oracle = probe.send({"project_oracle": {
            "root": over_root,
            "modules": {"demo.dep": "mncs 0.18; module demo.dep; record R { value: u64 }"},
        }})
        assert over_oracle.get("valid") is True, over_oracle

        return {
            "schema_version": "mncs.compiler.project-import-link-capacity/1",
            "status": "verified",
            "backend": execution["backend"],
            "source_count": len(sources),
            "expected_import_links": len(expected),
            "observed_import_links": len(imports),
            "native_project_valid": native["valid"],
            "native_target_ssa_valid": target_module["value_ssa"]["valid"],
            "stage0_oracle_valid": oracle["valid"],
            "all_transitive_record_schemas_verified": True,
            "imports_at_module_limit_resolved": many_native["import_count"] == 64,
            "stage0_accepts_64_imports": many_oracle["valid"],
            "per_module_import_limit": 64,
            "over_limit_import_declaration_count": 65,
            "over_limit_imports_rejected": not over_native["valid"],
            "over_limit_diagnostic": over_diagnostics[0],
            "stage0_accepts_65_imports": over_oracle["valid"],
            "probe_request_count": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
