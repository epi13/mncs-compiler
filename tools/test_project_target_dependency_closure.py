#!/usr/bin/env python3
"""Target-only lowering keeps imported schemas inside the transitive closure."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time

import test_project as project
from probe_compiler_module_frontier import _seed_target_specialization
from test_project_target_module import _target_request


def _unrelated_sources() -> list[tuple[str, Path, bytes]]:
    sources = []
    for index in range(20):
        fields = ", ".join(f"f{field}: u64" for field in range(8))
        text = f"mncs 0.18; module demo.unused{index:02}; record Record{index:02} {{ {fields} }}"
        source_id = f"{index:02}-unused.mncs"
        sources.append((source_id, Path(source_id), text.encode()))
    return sources


def _measure_target(probe, identities, sources, target_index):
    request = _target_request(project.request_value(identities, sources), target_index)
    started = time.perf_counter()
    response = probe.send(request)
    elapsed = time.perf_counter() - started
    assert response.get("status") == "returned", response
    return project.decode(response["returned"][0]), elapsed, probe.request_observations[-1]


def _snapshot_fingerprint(sources: list[tuple[str, Path, bytes]]) -> str:
    digest = hashlib.sha256()
    for _, _, source in sources:
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()


def run() -> dict:
    _seed_target_specialization()
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        irrelevant = _unrelated_sources()

        # This fixture matches the pre-change measurement: twenty unrelated
        # record modules and one selected, import-free target.
        simple_root = (
            "mncs 0.18; module demo.root; "
            "fn main() -> (r: u64) { return 1; }"
        )
        simple_sources = [
            *irrelevant,
            ("zz-root.mncs", Path("zz-root.mncs"), simple_root.encode()),
        ]
        simple, simple_wall, simple_observation = _measure_target(
            probe, identities, simple_sources, len(simple_sources) - 1
        )
        assert simple["valid"] and simple["value_ssa_valid"], simple
        assert len(project.flist(simple["modules"])) == 1
        assert simple["module_count"] == len(simple_sources)

        dependency = (
            "mncs 0.18; module demo.dep; record Box { value: u64 }"
        )
        middle = (
            "mncs 0.18; module demo.mid; use demo.dep as dep; "
            "record Wrap { inner: dep.Box }"
        )
        root = (
            "mncs 0.18; module demo.root; use demo.mid as mid; "
            "fn read(value: mid.Wrap) -> (r: u64) { return value.inner.value; }"
        )
        closure_sources = [
            *irrelevant,
            ("a-dep.mncs", Path("a-dep.mncs"), dependency.encode()),
            ("b-mid.mncs", Path("b-mid.mncs"), middle.encode()),
            ("zz-root.mncs", Path("zz-root.mncs"), root.encode()),
        ]
        target_index = len(closure_sources) - 1
        native, closure_wall, closure_observation = _measure_target(
            probe, identities, closure_sources, target_index
        )
        assert native["valid"] and native["value_ssa_valid"], native
        assert native["source_count"] == len(closure_sources)
        assert native["module_count"] == len(closure_sources)
        modules = project.flist(native["modules"])
        assert len(modules) == 1 and modules[0]["source_index"] == target_index, modules
        assert modules[0]["flow"]["proof"]["ok"] is True, modules[0]["flow"]["proof"]
        assert modules[0]["value_ssa"]["valid"] is True, modules[0]["value_ssa"]

        oracle = probe.send({"project_oracle": {
            "root": root,
            "modules": {"demo.mid": middle, "demo.dep": dependency},
        }})
        assert oracle.get("valid") is True, oracle
        assert oracle.get("program") is not None and oracle.get("ssa") is not None, oracle

        bad_root = root.replace("value.inner.value", "value.inner.absent")
        bad_sources = [
            *irrelevant,
            ("a-dep.mncs", Path("a-dep.mncs"), dependency.encode()),
            ("b-mid.mncs", Path("b-mid.mncs"), middle.encode()),
            ("zz-root.mncs", Path("zz-root.mncs"), bad_root.encode()),
        ]
        rejected, rejected_wall, _ = _measure_target(
            probe, identities, bad_sources, len(bad_sources) - 1
        )
        assert rejected["valid"] is False, rejected
        rejected_modules = project.flist(rejected["modules"])
        assert len(rejected_modules) == 1
        proof = rejected_modules[0]["flow"]["proof"]
        assert proof["ok"] is False, proof
        missing_span = (bad_root.index("absent"), bad_root.index("absent") + len("absent"))
        assert (proof["err_start"], proof["err_end"]) == missing_span, proof
        bad_oracle = probe.send({"project_oracle": {
            "root": bad_root,
            "modules": {"demo.mid": middle, "demo.dep": dependency},
        }})
        assert bad_oracle.get("valid") is False, bad_oracle

        return {
            "schema_version": 1,
            "status": "verified",
            "backend": execution["backend"],
            "unrelated_record_modules": 20,
            "target_source_count": len(simple_sources),
            "target_lowered_module_count": len(project.flist(simple["modules"])),
            "simple_snapshot_fingerprint_sha256": _snapshot_fingerprint(simple_sources),
            "post_change_simple_target_wall_seconds": round(simple_wall, 6),
            "post_change_simple_target_resources": simple_observation.get("resource_summary"),
            "transitive_nominal_target_wall_seconds": round(closure_wall, 6),
            "transitive_snapshot_fingerprint_sha256": _snapshot_fingerprint(closure_sources),
            "transitive_nominal_target_resources": closure_observation.get("resource_summary"),
            "transitive_nominal_verified_functions": modules[0]["value_ssa"]["verified_function_count"],
            "stage0_transitive_nominal_parity": True,
            "fresh_missing_field_rejected_by_native_and_stage0": True,
            "fresh_missing_field_span": list(missing_span),
            "negative_target_wall_seconds": round(rejected_wall, 6),
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
