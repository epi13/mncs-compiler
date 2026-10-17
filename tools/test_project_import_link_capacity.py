#!/usr/bin/env python3
"""Check per-module and project-wide import bounds against Stage 0."""

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

        # Preserve compatibility with Stage 0 immediately above the old 64
        # bound while confirming the native resolver now accepts it.
        above_old_sources, _above_old_root = _repeated_import_source(65)
        above_old_request = _target_request(
            project.request_value(identities, above_old_sources), 1,
        )
        above_old_response = probe.send(above_old_request)
        assert above_old_response.get("status") == "returned", above_old_response
        above_old_native = project.decode(above_old_response["returned"][0])
        above_old_imports = project.flist(above_old_native["imports"])
        assert above_old_native["valid"] and above_old_native["value_ssa_valid"], above_old_native
        assert above_old_native["import_count"] == len(above_old_imports) == 65
        # The declaration parser and linked-list passes support 1024 imports
        # per module. The native resolver now admits that entire bound.
        module_limit_sources, _module_limit_root = _repeated_import_source(1024)
        module_limit_request = _target_request(
            project.request_value(identities, module_limit_sources), 1,
        )
        module_limit_response = probe.send(module_limit_request)
        assert module_limit_response.get("status") == "returned", module_limit_response
        module_limit_native = project.decode(module_limit_response["returned"][0])
        module_limit_imports = project.flist(module_limit_native["imports"])
        assert module_limit_native["valid"] and module_limit_native["value_ssa_valid"], module_limit_native
        assert module_limit_native["import_count"] == len(module_limit_imports) == 1024
        # A 1025th use exceeds the module bound and reports the exact next
        # source span without dropping any of the admitted links.
        over_module_sources, over_module_root = _repeated_import_source(1025)
        over_module_request = _target_request(
            project.request_value(identities, over_module_sources), 1,
        )
        over_module_response = probe.send(over_module_request)
        assert over_module_response.get("status") == "returned", over_module_response
        over_module_native = project.decode(over_module_response["returned"][0])
        over_module_imports = project.flist(over_module_native["imports"])
        over_module_diagnostics = project.flist(over_module_native["diagnostics"])
        assert not over_module_native["valid"], over_module_native
        assert not over_module_native["value_ssa_valid"], over_module_native
        assert over_module_native["import_count"] == len(over_module_imports) == 1024
        assert len(over_module_diagnostics) == 1, over_module_diagnostics
        module_diagnostic = over_module_diagnostics[0].get("$p", {})
        module_expected_start = over_module_root.rfind("use demo.dep as d1024;") + len("use ")
        assert module_diagnostic.get("source_index") == 1, over_module_diagnostics
        assert module_diagnostic.get("limit") == 1024, over_module_diagnostics
        assert module_diagnostic.get("start") == module_expected_start, over_module_diagnostics
        assert module_diagnostic.get("end") == module_expected_start + len("demo.dep"), over_module_diagnostics

        # The global guard is directly checked on both sides of its threshold;
        # integration traversal continues to use the pre-existing four-chunk
        # 4096-link retention bound.
        limit_results = {}
        for count, expected_reached in ((4095, False), (4096, True)):
            result = probe.send({
                "schema_version": "0.1",
                "target": {
                    "module": project.PROJECT_MODULE,
                    "function": "project_import_limit_reached",
                },
                "arguments": [project.integer(count)],
                "type_arguments": [],
                "step_budget": 8_000_000,
            })
            assert result.get("status") == "returned", result
            reached = project.decode(result["returned"][0])
            assert reached is expected_reached, (count, reached, expected_reached)
            limit_results[count] = reached

        return {
            "schema_version": "mncs.compiler.project-import-link-capacity/2",
            "status": "verified",
            "backend": execution["backend"],
            "source_count": len(sources),
            "expected_import_links": len(expected),
            "observed_import_links": len(imports),
            "native_project_valid": native["valid"],
            "native_target_ssa_valid": target_module["value_ssa"]["valid"],
            "stage0_oracle_valid": oracle["valid"],
            "all_transitive_record_schemas_verified": True,
            "imports_above_old_limit_resolved": above_old_native["import_count"] == 65,
            "imports_at_module_limit_resolved": module_limit_native["import_count"] == 1024,
            "per_module_import_limit": 1024,
            "over_module_limit_import_declaration_count": 1025,
            "over_module_limit_imports_rejected": not over_module_native["valid"],
            "over_module_limit_diagnostic": over_module_diagnostics[0],
            "project_import_link_limit": 4096,
            "global_limit_guard_below_boundary": not limit_results[4095],
            "global_limit_guard_at_boundary": limit_results[4096],
            "probe_request_count": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
