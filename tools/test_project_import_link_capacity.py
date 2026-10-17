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
            "probe_request_count": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
