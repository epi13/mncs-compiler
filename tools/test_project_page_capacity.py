#!/usr/bin/env python3
"""Exercise bounded dynamic capacity for flat Project page vectors."""

import json
import os

import probe_compiler_module_frontier as frontier
import test_project as project


IDENTITIES = {
    "ProjectSnapshot": {"identity": "test-project-snapshot"},
    "ProjectSource": {"identity": "test-project-source"},
}


def _project_seed(seeds, function):
    return next(
        seed for seed in seeds
        if seed.get("module") == project.PROJECT_MODULE
        and seed.get("function") == function
    )


def run():
    sources = [
        ("a.mncs", None, b"x" * 1024),
        ("b.mncs", None, b"y"),
    ]
    capacity = 1025
    pages = sum(len(source) for _, _, source in sources)
    assert pages == 1025

    request = project.request_value(
        IDENTITIES, sources, stride=1, page_bound=capacity
    )
    assert request["type_arguments"][0]["value"] == 1025
    assert len(request["arguments"][1]["sequence"]["values"]) == 1025
    snapshot = project.decode(request["arguments"][0])
    assert [source["page_count"] for source in snapshot["sources"]] == [1024, 1]
    assert [source["page_start"] for source in snapshot["sources"]] == [0, 1024]

    try:
        project.request_value(IDENTITIES, sources, stride=1, page_bound=1024)
    except ValueError as error:
        assert "cannot hold 1025 pages" in str(error), error
    else:
        raise AssertionError("under-sized flat page capacity was accepted")

    actual_sources = frontier._load_sources(None)
    actual_capacity, actual_pages = frontier._project_page_capacity(actual_sources)
    assert actual_pages > project.PAGE_BOUND, actual_pages
    assert actual_capacity == actual_pages, (actual_capacity, actual_pages)
    assert actual_capacity <= frontier.PROJECT_PROBE_PAGE_CAPACITY_LIMIT

    try:
        frontier._project_page_capacity([
            ("a.mncs", None, b"a" * (1024 * 1024)),
            ("b.mncs", None, b"b" * (1024 * 1024)),
            ("c.mncs", None, b"c"),
        ])
    except ValueError as error:
        assert "limited to 2048 pages" in str(error), error
    else:
        raise AssertionError("the probe accepted more than its 2048-page guard")

    try:
        frontier._project_page_capacity([
            ("oversized.mncs", None, b"x" * (1024 * 1024 + 1)),
        ])
    except ValueError as error:
        assert "1024-page per-module slice" in str(error), error
    else:
        raise AssertionError("the probe accepted a source larger than 1024 pages")

    previous_seeds = os.environ.get("MNCS_PROBE_GENERIC_SEEDS")
    previous_modules = os.environ.get("MNCS_PROBE_EXECUTION_MODULES")
    try:
        frontier._seed_project_specialization(actual_capacity)
        compile_seeds = json.loads(os.environ["MNCS_PROBE_GENERIC_SEEDS"])
        compile_seed = _project_seed(compile_seeds, "compile_project")
        assert compile_seed["type_arguments"][0]["value"] == actual_capacity
        assert not any(
            seed.get("module") == project.PROJECT_MODULE
            and seed.get("function") == "compile_project"
            and seed["type_arguments"][0]["value"] == project.PAGE_BOUND
            for seed in compile_seeds
        )

        frontier._seed_project_specialization(actual_capacity, target_only=True)
        target_seeds = json.loads(os.environ["MNCS_PROBE_GENERIC_SEEDS"])
        target_seed = _project_seed(target_seeds, "compile_project_target")
        assert target_seed["type_arguments"][0]["value"] == actual_capacity
        assert not any(
            seed.get("module") == project.PROJECT_MODULE
            and seed.get("function") == "compile_project"
            for seed in target_seeds
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
        "crossed_default_page_capacity": True,
        "request_pages": pages,
        "request_capacity": capacity,
        "per_source_page_counts": [1024, 1],
        "current_compiler_snapshot_pages": actual_pages,
        "current_compiler_snapshot_capacity": actual_capacity,
        "probe_capacity_limit": frontier.PROJECT_PROBE_PAGE_CAPACITY_LIMIT,
        "dynamic_compile_project_and_target_seeds": True,
        "under_sized_capacity_rejected": True,
        "probe_page_guard_rejected": True,
        "per_source_page_guard_rejected": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
