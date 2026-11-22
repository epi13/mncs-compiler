#!/usr/bin/env python3
"""Check the Project input profile ceiling before starting a self-host probe."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import compiler_diagnosis
import probe_compiler_module_frontier as frontier
import test_native_decl_vm_emit as declaration_emit
import test_project as project


IDENTITIES = {
    "ProjectSnapshot": {"identity": "test-project-snapshot"},
    "ProjectSource": {"identity": "test-project-source"},
}


def run():
    small_sources = [
        ("a.mncs", None, b"a"),
        ("b.mncs", None, b"b"),
    ]
    capacity, pages = frontier._project_page_capacity(small_sources)
    assert capacity == project.PAGE_BOUND and pages == 2, (capacity, pages)

    request = project.request_value(IDENTITIES, small_sources)
    assert request["type_arguments"][0]["value"] == project.PAGE_BOUND
    snapshot = project.decode(request["arguments"][0])
    assert [source["page_count"] for source in snapshot["sources"]] == [1, 1]
    assert [source["page_start"] for source in snapshot["sources"]] == [0, 1]

    exact_boundary = [("one-megabyte.mncs", None, b"x" * (1024 * 1024))]
    boundary_capacity, boundary_pages = frontier._project_page_capacity(exact_boundary)
    assert boundary_capacity == 1024 and boundary_pages == 1024

    next_tier = [
        ("a.mncs", None, b"a" * (1024 * 1024)),
        ("b.mncs", None, b"b"),
    ]
    assert frontier._project_page_capacity(next_tier) == (2048, 1025)
    assert frontier._project_page_capacity(exact_boundary * 3) == (4096, 3072)
    try:
        frontier._project_page_capacity(exact_boundary * 4 + small_sources)
    except ValueError as error:
        assert "needs 4098 pages" in str(error), error
        assert "Profile 0.19's generic sequence ceiling of 4096" in str(error), error
    else:
        raise AssertionError("the Project probe accepted 4098 flat pages")

    try:
        frontier._project_page_capacity([
            ("oversized.mncs", None, b"x" * (1024 * 1024 + 1)),
        ])
    except ValueError as error:
        assert "1024-page per-module slice" in str(error), error
    else:
        raise AssertionError("the probe accepted a source larger than 1024 pages")

    actual_sources = frontier._load_sources(None)
    actual_tier, actual_pages = frontier._project_page_capacity(actual_sources)
    assert actual_pages > project.PAGE_BOUND, actual_pages
    assert actual_tier == next(tier for tier in frontier.PROJECT_PROBE_PAGE_TIERS if actual_pages <= tier)

    target, decl_sources = declaration_emit.select_sources("parse_use")
    assert [entry[0] for entry in decl_sources] == sorted(entry[0] for entry in decl_sources)
    assert decl_sources[-1][0] == "06_parse_use_caller.mncs"
    selected, decl_pages = frontier._project_page_capacity(decl_sources)
    assert selected == 2048 and decl_pages > 1024
    encoded = project.encode_project_request_bytes(
        IDENTITIES, small_sources, page_bound=selected, target_index=1,
        target_module=declaration_emit.EMITTER, target_function=declaration_emit.PRODUCER,
        extra_arguments=[{"wrapper_identity": target}],
    )
    wire = json.loads(encoded)
    assert wire["type_arguments"] == [{"kind": "nat", "value": 2048}, {"kind": "nat", "value": 1024}]
    assert wire["target"] == {"module": declaration_emit.EMITTER, "function": declaration_emit.PRODUCER}
    assert wire["arguments"][2:] == [project.integer(1), {"wrapper_identity": target}]

    with tempfile.TemporaryDirectory(prefix="mncs-project-capacity-") as td:
        cache = Path(td) / "selected-callable-diagnosis.json"
        real_write = compiler_diagnosis.write_cached_diagnosis
        real_build = compiler_diagnosis.build_diagnosis_document
        with (
            patch.object(declaration_emit, "run_request", side_effect=AssertionError("capacity refusal launched a probe")),
            patch.object(compiler_diagnosis, "build_diagnosis_document", side_effect=lambda *args, **kwargs:
                real_build(*args, **kwargs, disabled_providers={"language_service", "doctor", "debug"})),
            patch.object(compiler_diagnosis, "write_cached_diagnosis", side_effect=lambda checkout, doc:
                real_write(checkout, doc, cache_path=cache)),
        ):
            refusal = declaration_emit.run_attempt("parse_use", 1024, Path(td) / "artifact.json")["emission"]
        doc = compiler_diagnosis.evaluate_cached_diagnosis(declaration_emit.ROOT, cache_path=cache)
        assert refusal["supplied_capacity"] == 1024 and refusal["required_capacity"] == decl_pages
        assert doc["status"] == "refused" and doc["freshness"] == "current"
        assert doc["compiler_provenance"]["selected_target"] == target
        assert doc["compiler_provenance"]["selected_specialization"].endswith("parse_use<4,32>")
        assert doc["compiler_provenance"]["last_completed_phase"] == "none"
        assert doc["diagnosis"]["request_failure"]["required_capacity"] == decl_pages
        assert doc["diagnosis"]["error_function_index"] is None
        assert doc["measurements"]["heavyweight_compile_launched"] is False

    previous_seeds = os.environ.get("MNCS_PROBE_GENERIC_SEEDS")
    previous_modules = os.environ.get("MNCS_PROBE_EXECUTION_MODULES")
    try:
        frontier._seed_target_specialization(target_only=True)
        seeds = json.loads(os.environ["MNCS_PROBE_GENERIC_SEEDS"])
        seed = next(
            item for item in seeds
            if item.get("module") == project.PROJECT_MODULE
            and item.get("function") == "compile_project_target"
        )
        assert seed["type_arguments"] == [
            {"kind": "nat", "value": 1024},
            {"kind": "nat", "value": 1024},
        ]
        assert not any(
            item.get("module") == project.PROJECT_MODULE
            and item.get("function") == "compile_project"
            for item in seeds
        )
    finally:
        if previous_seeds is None:
            os.environ.pop("MNCS_PROBE_GENERIC_SEEDS", None)
        else:
            os.environ["MNCS_PROBE_GENERIC_SEEDS"] = previous_seeds
        if previous_modules is None:
            os.environ.pop("MNCS_PROBE_EXECUTION_MODULES", None)
        else:
            os.environ["MNCS_PROBE_EXECUTION_MODULES"] = previous_modules

    return {
        "status": "passed",
        "default_page_tier": project.PAGE_BOUND,
        "profile_page_ceiling": frontier.PROJECT_PROBE_PAGE_CAPACITY_LIMIT,
        "small_project_pages": pages,
        "exact_limit_pages": boundary_pages,
        "full_compiler_snapshot_pages": actual_pages,
        "full_compiler_snapshot_sources": len(actual_sources),
        "full_compiler_snapshot_tier": actual_tier,
        "declaration_closure_pages": decl_pages,
        "declaration_closure_tier": selected,
        "undersized_diagnosis_published_before_probe_start": True,
        "oversized_source_rejected": True,
        "target_specialization_stays_within_profile": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
