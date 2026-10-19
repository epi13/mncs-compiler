#!/usr/bin/env python3
"""Check the Project input profile ceiling before starting a self-host probe."""

import json
import os

import probe_compiler_module_frontier as frontier
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

    over_boundary = [
        ("a.mncs", None, b"a" * (1024 * 1024)),
        ("b.mncs", None, b"b"),
    ]
    try:
        frontier._project_page_capacity(over_boundary)
    except ValueError as error:
        assert "needs 1025 pages" in str(error), error
        assert "Profile 0.18's generic sequence ceiling of 1024" in str(error), error
    else:
        raise AssertionError("the Project probe accepted 1025 flat pages")

    try:
        frontier._project_page_capacity([
            ("oversized.mncs", None, b"x" * (1024 * 1024 + 1)),
        ])
    except ValueError as error:
        assert "1024-page per-module slice" in str(error), error
    else:
        raise AssertionError("the probe accepted a source larger than 1024 pages")

    actual_sources = frontier._load_sources(None)
    actual_pages = sum(
        (len(source) + project.STRIDE_DEFAULT - 1) // project.STRIDE_DEFAULT
        for _, _, source in actual_sources
    )
    assert actual_pages > project.PAGE_BOUND, actual_pages
    try:
        frontier._project_page_capacity(actual_sources)
    except ValueError as error:
        assert f"needs {actual_pages} pages" in str(error), error
    else:
        raise AssertionError("the full compiler snapshot passed the 1024-page preflight")

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
        "profile_page_ceiling": project.PAGE_BOUND,
        "small_project_pages": pages,
        "exact_limit_pages": boundary_pages,
        "full_compiler_snapshot_pages": actual_pages,
        "full_compiler_snapshot_sources": len(actual_sources),
        "full_compiler_snapshot_rejected_before_probe_start": True,
        "oversized_source_rejected": True,
        "target_specialization_stays_within_profile": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
