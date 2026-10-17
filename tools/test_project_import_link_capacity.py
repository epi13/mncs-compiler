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


def _project_boundary_sources(with_overflow: bool) -> list:
    dependency = "mncs 0.18; module demo.dep; record R { value: u64 }\n"
    sources = [("00-dep.mncs", Path("00-dep.mncs"), dependency.encode())]
    imports = "".join(
        f"use demo.dep as d{index};\n" for index in range(1024)
    )
    for index in range(4):
        module_name = f"demo.batch{index}"
        source_id = f"0{index + 1}-batch{index}.mncs"
        source = (
            f"mncs 0.18; module {module_name};\n"
            f"{imports}"
            "fn main() -> (result: u64) { return 1; }\n"
        )
        sources.append((source_id, Path(source_id), source.encode()))
    if with_overflow:
        source_id = "05-overflow.mncs"
        source = (
            "mncs 0.18; module demo.overflow;\n"
            "use demo.dep as extra;\n"
            "fn main() -> (result: u64) { return 1; }\n"
        )
        sources.append((source_id, Path(source_id), source.encode()))
    return sources


def _connected_source_capacity_project(source_count: int = 64) -> tuple[list, dict, str]:
    """Build a fully connected import chain at the Project source bound."""
    sources = []
    modules = {}
    for index in range(source_count):
        module_name = f"demo.chain{index:03d}"
        source_id = f"{index:03d}-chain{index:03d}.mncs"
        if index == 0:
            source = (
                f"mncs 0.18; module {module_name};\n"
                "fn value(x: u64) -> (result: u64) { return x; }\n"
            )
        else:
            prior = f"demo.chain{index - 1:03d}"
            source = (
                f"mncs 0.18; module {module_name};\n"
                f"use {prior} as prior;\n"
                "fn value(x: u64) -> (result: u64) { "
                "return prior.value(x) + 1; }\n"
            )
        sources.append((source_id, Path(source_id), source.encode()))
        if index + 1 < source_count:
            modules[module_name] = source
    return sources, modules, sources[-1][2].decode()


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
        assert module_limit_native["import_module_name_comparisons"] == 1, module_limit_native
        assert module_limit_native["import_resolution_cache_hits"] == 1023, module_limit_native
        expected_links = set()
        cursor = 0
        for index in range(1024):
            use_start = _module_limit_root.index(f"use demo.dep as d{index};", cursor)
            cursor = use_start + 1
            module_start = use_start + len("use ")
            module_end = module_start + len("demo.dep")
            alias_start = module_end + len(" as ")
            alias_end = alias_start + len(f"d{index}")
            expected_links.add((module_start, module_end, alias_start, alias_end, 0))
        observed_links = {
            (
                item["module_start"], item["module_end"],
                item["alias_start"], item["alias_end"], item["target_index"],
            )
            for item in module_limit_imports
        }
        assert observed_links == expected_links, {
            "observed_count": len(observed_links),
            "expected_count": len(expected_links),
            "missing": sorted(expected_links - observed_links)[:8],
            "unexpected": sorted(observed_links - expected_links)[:8],
        }
        assert all(item["source_index"] == 1 and item["target_index"] == 0 for item in module_limit_imports)

        # Equal-length module names with a shared prefix must retain distinct
        # targets when imports alternate within the same consumer module.
        alpha_name = "demo.alpha"
        bravo_name = "demo.bravo"
        assert len(alpha_name) == len(bravo_name)
        interleaved_root = "mncs 0.18; module demo.interleaved;\n"
        interleaved_targets = []
        for index in range(6):
            module_name = alpha_name if index % 2 == 0 else bravo_name
            interleaved_targets.append(0 if index % 2 == 0 else 1)
            interleaved_root += f"use {module_name} as m{index};\n"
        interleaved_root += "fn main() -> (result: u64) { return 1; }\n"
        alpha_source = "mncs 0.18; module demo.alpha; fn value() -> (result: u64) { return 1; }"
        bravo_source = "mncs 0.18; module demo.bravo; fn value() -> (result: u64) { return 2; }"
        interleaved_sources = [
            ("00-alpha.mncs", Path("00-alpha.mncs"), alpha_source.encode()),
            ("01-bravo.mncs", Path("01-bravo.mncs"), bravo_source.encode()),
            ("zz-interleaved.mncs", Path("zz-interleaved.mncs"), interleaved_root.encode()),
        ]
        interleaved_response = probe.send(_target_request(
            project.request_value(identities, interleaved_sources), 2,
        ))
        assert interleaved_response.get("status") == "returned", interleaved_response
        interleaved_native = project.decode(interleaved_response["returned"][0])
        interleaved_imports = project.flist(interleaved_native["imports"])
        expected_interleaved_links = set()
        cursor = 0
        for index, target_index in enumerate(interleaved_targets):
            module_name = alpha_name if index % 2 == 0 else bravo_name
            use_start = interleaved_root.index(f"use {module_name} as m{index};", cursor)
            cursor = use_start + 1
            module_start = use_start + len("use ")
            module_end = module_start + len(module_name)
            alias_start = module_end + len(" as ")
            alias_end = alias_start + len(f"m{index}")
            expected_interleaved_links.add((module_start, module_end, alias_start, alias_end, target_index))
        observed_interleaved_links = {
            (item["module_start"], item["module_end"], item["alias_start"], item["alias_end"], item["target_index"])
            for item in interleaved_imports
        }
        assert interleaved_native["valid"] and interleaved_native["value_ssa_valid"], interleaved_native
        assert interleaved_native["import_count"] == len(interleaved_imports) == 6
        assert interleaved_native["import_module_name_comparisons"] == 3, interleaved_native
        assert interleaved_native["import_resolution_cache_hits"] == 4, interleaved_native
        assert observed_interleaved_links == expected_interleaved_links, {
            "observed": sorted(observed_interleaved_links),
            "expected": sorted(expected_interleaved_links),
        }
        interleaved_oracle = probe.send({
            "project_oracle": {
                "root": interleaved_root,
                "modules": {alpha_name: alpha_source, bravo_name: bravo_source},
            }
        })
        assert interleaved_oracle.get("valid") is True, interleaved_oracle
        assert interleaved_oracle.get("program") is not None and interleaved_oracle.get("ssa") is not None, interleaved_oracle

        # Longer names fall back to the uncached resolver and still link each
        # current declaration to its target with exact source locations.
        long_module_name = "demo." + ("a" * 32) + "." + ("b" * 32)
        assert len(long_module_name) > 64
        long_root = "mncs 0.18; module demo.long_root;\n"
        long_root += "".join(
            f"use {long_module_name} as l{index};\n" for index in range(3)
        )
        long_root += "fn main() -> (result: u64) { return 1; }\n"
        long_sources = [
            ("00-long.mncs", Path("00-long.mncs"), f"mncs 0.18; module {long_module_name}; fn value() -> (result: u64) {{ return 1; }}".encode()),
            ("zz-long-root.mncs", Path("zz-long-root.mncs"), long_root.encode()),
        ]
        long_response = probe.send(_target_request(
            project.request_value(identities, long_sources), 1,
        ))
        assert long_response.get("status") == "returned", long_response
        long_native = project.decode(long_response["returned"][0])
        long_imports = project.flist(long_native["imports"])
        expected_long_links = set()
        cursor = 0
        for index in range(3):
            use_start = long_root.index(f"use {long_module_name} as l{index};", cursor)
            cursor = use_start + 1
            module_start = use_start + len("use ")
            module_end = module_start + len(long_module_name)
            alias_start = module_end + len(" as ")
            alias_end = alias_start + len(f"l{index}")
            expected_long_links.add((module_start, module_end, alias_start, alias_end, 0))
        observed_long_links = {
            (item["module_start"], item["module_end"], item["alias_start"], item["alias_end"], item["target_index"])
            for item in long_imports
        }
        assert long_native["valid"] and long_native["value_ssa_valid"], long_native
        assert long_native["import_count"] == len(long_imports) == 3
        assert long_native["import_module_name_comparisons"] == 3, long_native
        assert long_native["import_resolution_cache_hits"] == 0, long_native
        assert observed_long_links == expected_long_links, {
            "observed": sorted(observed_long_links),
            "expected": sorted(expected_long_links),
        }

        missing_root = "mncs 0.18; module demo.missing_root;\n"
        missing_root += "".join(
            f"use demo.absent as m{index};\n" for index in range(3)
        )
        missing_root += "fn main() -> (result: u64) { return 1; }\n"
        missing_sources = [
            ("00-dep.mncs", Path("00-dep.mncs"), b"mncs 0.18; module demo.dep; fn value() -> (result: u64) { return 1; }"),
            ("zz-missing.mncs", Path("zz-missing.mncs"), missing_root.encode()),
        ]
        missing_response = probe.send(_target_request(
            project.request_value(identities, missing_sources), 1,
        ))
        assert missing_response.get("status") == "returned", missing_response
        missing_native = project.decode(missing_response["returned"][0])
        missing_diagnostics = project.flist(missing_native["diagnostics"])
        expected_missing_spans = set()
        cursor = 0
        for index in range(3):
            use_start = missing_root.index(f"use demo.absent as m{index};", cursor)
            cursor = use_start + 1
            module_start = use_start + len("use ")
            expected_missing_spans.add((module_start, module_start + len("demo.absent")))
        observed_missing_spans = {
            (item.get("$p", {}).get("start"), item.get("$p", {}).get("end"))
            for item in missing_diagnostics
        }
        assert not missing_native["valid"] and not missing_native["value_ssa_valid"], missing_native
        assert missing_native["import_count"] == 0, missing_native
        assert len(missing_diagnostics) == 3, missing_diagnostics
        assert observed_missing_spans == expected_missing_spans, missing_diagnostics
        assert missing_native["import_resolution_cache_hits"] == 0, missing_native
        assert missing_native["import_module_name_comparisons"] == 3, missing_native

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

        # Four independently resolved module lists fill the project-wide
        # allowance exactly. One additional use is rejected with a single
        # diagnostic while retaining all 4096 admitted links.
        at_project_limit_sources = _project_boundary_sources(False)
        at_project_limit_request = _target_request(
            project.request_value(identities, at_project_limit_sources),
            len(at_project_limit_sources) - 1,
        )
        at_project_limit_response = probe.send(at_project_limit_request)
        assert at_project_limit_response.get("status") == "returned", at_project_limit_response
        at_project_limit_native = project.decode(
            at_project_limit_response["returned"][0]
        )
        at_project_limit_imports = project.flist(at_project_limit_native["imports"])
        assert at_project_limit_native["valid"], at_project_limit_native
        assert at_project_limit_native["value_ssa_valid"], at_project_limit_native
        assert at_project_limit_native["import_count"] == 4096
        assert len(at_project_limit_imports) == 4096
        assert at_project_limit_native["import_module_name_comparisons"] == 4, at_project_limit_native
        assert at_project_limit_native["import_resolution_cache_hits"] == 4092, at_project_limit_native

        over_project_sources = _project_boundary_sources(True)
        over_project_request = _target_request(
            project.request_value(identities, over_project_sources),
            len(over_project_sources) - 1,
        )
        over_project_response = probe.send(over_project_request)
        assert over_project_response.get("status") == "returned", over_project_response
        over_project_native = project.decode(
            over_project_response["returned"][0]
        )
        over_project_imports = project.flist(over_project_native["imports"])
        over_project_diagnostics = project.flist(
            over_project_native["diagnostics"]
        )
        assert not over_project_native["valid"], over_project_native
        assert not over_project_native["value_ssa_valid"], over_project_native
        assert over_project_native["import_count"] == 4096
        assert len(over_project_imports) == 4096
        assert len(over_project_diagnostics) == 1, over_project_diagnostics
        project_diagnostic = over_project_diagnostics[0].get("$p", {})
        assert project_diagnostic.get("limit") == 4096, over_project_diagnostics
        project_source_index = project_diagnostic.get("source_index")
        assert isinstance(project_source_index, int), over_project_diagnostics
        project_source = over_project_sources[project_source_index][2].decode()
        project_start = project_diagnostic.get("start")
        project_end = project_diagnostic.get("end")
        assert project_source[project_start:project_end] == "demo.dep", (
            project_diagnostic,
            over_project_sources[project_source_index][0],
        )

        # Exercise the maximum source count through a connected resolver
        # closure. Stage 0 independently checks every imported function while
        # native target-only Flow/SSA checks the selected root call.
        capacity_sources, capacity_modules, capacity_root = (
            _connected_source_capacity_project()
        )
        capacity_target_index = len(capacity_sources) - 1
        capacity_request = _target_request(
            project.request_value(identities, capacity_sources),
            capacity_target_index,
        )
        capacity_response = probe.send(capacity_request)
        assert capacity_response.get("status") == "returned", capacity_response
        capacity_native = project.decode(capacity_response["returned"][0])
        capacity_imports = project.flist(capacity_native["imports"])
        assert capacity_native["valid"] and capacity_native["value_ssa_valid"], capacity_native
        assert capacity_native["source_count"] == 64, capacity_native
        assert capacity_native["module_count"] == 64, capacity_native
        assert capacity_native["import_count"] == 63, capacity_native
        assert len(capacity_imports) == 63, capacity_imports
        capacity_target = project.flist(capacity_native["modules"])
        assert len(capacity_target) == 1, capacity_target
        assert capacity_target[0]["source_index"] == capacity_target_index
        assert capacity_target[0]["flow"]["proof"]["ok"] is True
        assert capacity_target[0]["value_ssa"]["valid"] is True
        capacity_oracle = probe.send({
            "project_oracle": {
                "root": capacity_root,
                "modules": capacity_modules,
            }
        })
        assert capacity_oracle.get("valid") is True, capacity_oracle
        assert len(capacity_oracle["program"]["functions"]) == 64, capacity_oracle
        stage0_chain_calls = {
            (function.get("home_module"), function.get("name")): function.get("identity")
            for function in capacity_oracle["program"]["functions"]
        }
        native_chain_calls = [
            project.identity_text(op["$p"]["identity"])
            for body in project.flist(capacity_target[0]["flow"]["proof"]["tops"])
            for op in project.flist(body)
            if "identity" in op.get("$p", {})
        ]
        assert native_chain_calls == [
            stage0_chain_calls[("demo.chain062", "value")]
        ], (native_chain_calls, stage0_chain_calls)

        # Keep the guard predicate covered at the threshold as a focused check.
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

        resource_measurements = [
            {
                "status": observation.get("status"),
                "sample_count": summary.get("sample_count"),
                "peak_rss_bytes": summary.get("peak_rss_bytes"),
                "max_kernel_rss_high_water_bytes": summary.get("max_kernel_rss_high_water_bytes"),
                "cpu_time_delta_ns": summary.get("cpu_time_delta_ns"),
                "io_read_bytes_delta": summary.get("io_read_bytes_delta"),
                "io_write_bytes_delta": summary.get("io_write_bytes_delta"),
            }
            for observation in probe.process_observations.values()
            if (summary := observation.get("resource_summary")) is not None
        ]
        return {
            "schema_version": "mncs.compiler.project-import-link-capacity/5",
            "status": "verified",
            "resource_measurements": resource_measurements,
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
            "module_limit_candidate_name_comparisons": module_limit_native["import_module_name_comparisons"],
            "module_limit_resolution_cache_hits": module_limit_native["import_resolution_cache_hits"],
            "module_limit_import_locations_preserved": len(observed_links) == 1024,
            "interleaved_distinct_target_header_comparisons": interleaved_native["import_module_name_comparisons"],
            "interleaved_distinct_target_cache_hits": interleaved_native["import_resolution_cache_hits"],
            "interleaved_target_indices_preserved": observed_interleaved_links == expected_interleaved_links,
            "long_module_name_bytes": len(long_module_name),
            "long_module_name_cache_hits": long_native["import_resolution_cache_hits"],
            "long_module_name_fallback_header_comparisons": long_native["import_module_name_comparisons"],
            "long_module_name_import_locations_preserved": observed_long_links == expected_long_links,
            "repeated_missing_import_diagnostics_preserved": len(missing_diagnostics) == 3,
            "repeated_missing_import_cache_hits": missing_native["import_resolution_cache_hits"],
            "per_module_import_limit": 1024,
            "over_module_limit_import_declaration_count": 1025,
            "over_module_limit_imports_rejected": not over_module_native["valid"],
            "over_module_limit_diagnostic": over_module_diagnostics[0],
            "project_boundary_source_count": len(at_project_limit_sources),
            "project_limit_links_accepted": len(at_project_limit_imports),
            "project_limit_project_valid": at_project_limit_native["valid"],
            "project_limit_candidate_name_comparisons": at_project_limit_native["import_module_name_comparisons"],
            "project_limit_resolution_cache_hits": at_project_limit_native["import_resolution_cache_hits"],
            "project_overflow_source_count": len(over_project_sources),
            "project_overflow_links_retained": len(over_project_imports),
            "project_overflow_rejected": not over_project_native["valid"],
            "project_overflow_diagnostic_count": len(over_project_diagnostics),
            "project_overflow_diagnostic": over_project_diagnostics[0],
            "connected_source_capacity": {
                "source_count": capacity_native["source_count"],
                "module_count": capacity_native["module_count"],
                "import_links": capacity_native["import_count"],
                "target_only_module_count": len(capacity_target),
                "target_flow_valid": capacity_target[0]["flow"]["proof"]["ok"],
                "target_ssa_valid": capacity_target[0]["value_ssa"]["valid"],
                "stage0_oracle_valid": capacity_oracle["valid"],
                "stage0_verified_function_count": len(capacity_oracle["program"]["functions"]),
                "root_import_identity_matches_stage0": True,
            },
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
