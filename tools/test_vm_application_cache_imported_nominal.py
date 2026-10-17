#!/usr/bin/env python3
"""Check application artifact invalidation for an imported nominal interface edit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_project_imported_flow_ssa as support
from test_vm_segment import test_vm_emit

ROOT = support.ROOT
ROOT_SOURCE = (
    "mncs 0.18; module demo.root; use demo.dep as dep; "
    "fn read(x: dep.Box) -> (r: u64) { return x.value; }"
)
BASE_DEPENDENCY = "mncs 0.18; module demo.dep; record Box { value: u64, padding: bool }"
EDITED_DEPENDENCY = "mncs 0.18; module demo.dep; record Box { value: u64, padding: u8 }"
INVALID_DEPENDENCY = "mncs 0.18; module demo.dep; record Box { value: bool, padding: u8 }"
EVIDENCE_PATH = ROOT / "evidence/self-host-imported-nominal-app-cache-invalidation-20261017.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def request(dependency: str) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "demo/root.mncs",
        "root_source": ROOT_SOURCE,
        "entry": {"module": "demo.root", "function": "read"},
        "modules": {"demo.dep": dependency},
        "generic_seeds": [],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def record_identity(build: dict) -> str:
    interface = build["entry"]["interface"]
    records = [row for row in interface["types"]["records"] if row["name"] == "Box"]
    if len(records) != 1:
        raise AssertionError(f"expected exactly one imported Box interface: {records}")
    return records[0]["identity"]


def artifact_identity(build: dict) -> str:
    artifact = (build.get("artifact") or {}).get("value") or {}
    identity = artifact.get("artifact_id")
    if not isinstance(identity, str) or not identity.startswith("sha256:"):
        raise AssertionError(f"build has no verified artifact identity: {artifact}")
    return identity


def build_observation(label: str, source: dict, build: dict, metrics: dict) -> dict:
    artifact = (build.get("artifact") or {}).get("value") or {}
    cache = build.get("cache") or {}
    work = cache.get("verification_work") or {}
    return {
        "label": label,
        "source_snapshot_identity": (build.get("source_snapshot") or {}).get("identity"),
        "dependency_source_sha256": sha(source["modules"]["demo.dep"].encode()),
        "artifact_identity": artifact.get("artifact_id"),
        "status": build.get("status"),
        "entry_status": (build.get("entry") or {}).get("status"),
        "cache_status": cache.get("status"),
        "reuse_basis": cache.get("reuse_basis"),
        "backend_artifact": work.get("backend_artifact"),
        "front_end_invocations": work.get("front_end_invocations"),
        "recomputed_units": cache.get("recomputed_units", []),
        "diagnostic_codes": sorted(
            row.get("code") for row in build.get("diagnostics", [])
            if isinstance(row.get("code"), str)
        ),
        "measurement": metrics,
    }


def project_oracle(probe: support.Probe, label: str, dependency: str) -> dict:
    result, metrics = probe.send({"project_oracle": {
        "root": ROOT_SOURCE,
        "modules": {"demo.dep": dependency},
    }})
    return {
        "label": label,
        "valid": result.get("valid") is True,
        "record_identity": next((
            row.get("identity") for row in (result.get("program") or {}).get("record_types", [])
            if row.get("name") == "Box"
        ), None),
        "diagnostic_codes": sorted(
            row.get("code") for row in result.get("diagnostics", [])
            if isinstance(row.get("code"), str)
        ),
        "measurement": metrics,
    }


def vm_input(identity: str, padding: dict) -> dict:
    return {"record": {
        "name": "Box",
        "type_identity": identity,
        "fields": [
            ["padding", padding],
            ["value", {"integer": {
                "type": {"bits": 64, "signed": False},
                "value": 42,
            }}],
        ],
    }}


def canonical_vm_call(build: dict, padding: dict, label: str, directory: Path) -> dict:
    artifact = (build.get("artifact") or {}).get("value") or {}
    identity = record_identity(build)
    artifact_path = directory / f"{label}-artifact.json"
    calls_path = directory / f"{label}-calls.json"
    output_path = directory / f"{label}-results.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
    calls_path.write_text(json.dumps([{
        "id": label,
        "request": {
            "schema_version": "0.1",
            "target": {"module": "demo.root", "function": "read"},
            "arguments": [vm_input(identity, padding)],
            "type_arguments": [],
            "step_budget": 100_000,
        },
    }]), encoding="utf-8")
    started = time.monotonic()
    completed = subprocess.run([
        "/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
        "--artifact", str(artifact_path), "--calls", str(calls_path),
        "--output", str(output_path),
    ], capture_output=True, text=True, check=False)
    wall = time.monotonic() - started
    if completed.returncode != 0:
        raise AssertionError(
            f"canonical VM rejected {label}: {completed.stderr[-3000:]}"
        )
    document = json.loads(output_path.read_text(encoding="utf-8"))
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError(f"canonical VM admitted a different artifact for {label}")
    results = document.get("results", [])
    if len(results) != 1 or results[0].get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"canonical VM did not complete {label}: {results}")
    returned = results[0].get("record", {}).get("returned", [])
    if returned != [{"Integer": {"bits": 64, "signed": False, "value": 42}}]:
        raise AssertionError(f"canonical VM returned the wrong value for {label}: {returned}")
    rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
    user = re.search(r"User time \(seconds\): ([0-9.]+)", completed.stderr)
    system = re.search(r"System time \(seconds\): ([0-9.]+)", completed.stderr)
    return {
        "case_id": label,
        "artifact_identity": artifact.get("artifact_id"),
        "imported_record_identity": identity,
        "outcome": results[0]["outcome"],
        "returned": returned,
        "wall_seconds": round(wall, 6),
        "cpu_seconds": round(float(user.group(1)) + float(system.group(1)), 6)
        if user and system else None,
        "peak_rss_kib": int(rss.group(1)) if rss else None,
        "steps": results[0].get("record", {}).get("usage", {}).get("steps"),
    }


def main() -> None:
    build_root = ROOT / ".build/native-app-readiness"
    build_root.mkdir(parents=True, exist_ok=True)
    host_before = support.memory_snapshot()
    cgroup_before = support.cgroup_snapshot()
    started = time.monotonic()
    app_builds: list[dict] = []
    oracle_rows: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="imported-nominal-cache-", dir=build_root) as cache:
        probe = support.Probe(Path(cache) / "application-cache")
        try:
            oracle_base = project_oracle(probe, "base", BASE_DEPENDENCY)
            oracle_edit = project_oracle(probe, "field_interface_edit", EDITED_DEPENDENCY)
            oracle_invalid = project_oracle(probe, "invalid_value_field", INVALID_DEPENDENCY)
            oracle_rows.extend([oracle_base, oracle_edit, oracle_invalid])
            if not oracle_base["valid"] or not oracle_edit["valid"] or oracle_invalid["valid"]:
                raise AssertionError(f"Stage-0 oracle returned unexpected results: {oracle_rows}")
            if oracle_base["record_identity"] == oracle_edit["record_identity"]:
                raise AssertionError("Stage-0 did not change Box identity after its field edit")

            base_request = request(BASE_DEPENDENCY)
            base_build, base_metrics = probe.send({"compile_application": base_request})
            base = build_observation("cold", base_request, base_build, base_metrics)
            if (
                base["status"] != "verified"
                or base["cache_status"] != "miss"
                or base["backend_artifact"] != "generated"
                or base["front_end_invocations"] != 1
                or record_identity(base_build) != oracle_base["record_identity"]
            ):
                raise AssertionError(f"base imported-record application did not verify: {base}")
            app_builds.append(base)

            warm_build, warm_metrics = probe.send({"compile_application": base_request})
            warm = build_observation("warm_unchanged", base_request, warm_build, warm_metrics)
            if (
                warm["status"] != "verified"
                or warm["cache_status"] != "hit"
                or warm["backend_artifact"] != "reused"
                or warm["front_end_invocations"] != 0
                or warm["artifact_identity"] != base["artifact_identity"]
            ):
                raise AssertionError(f"unchanged application did not reuse exact artifact: {warm}")
            app_builds.append(warm)

            edited_request = request(EDITED_DEPENDENCY)
            edited_build, edited_metrics = probe.send({"compile_application": edited_request})
            edited = build_observation("imported_nominal_field_interface_edit", edited_request,
                                       edited_build, edited_metrics)
            if (
                edited["status"] != "verified"
                or edited["cache_status"] != "miss"
                or edited["backend_artifact"] != "generated"
                or edited["front_end_invocations"] != 1
                or edited["artifact_identity"] == base["artifact_identity"]
                or record_identity(edited_build) != oracle_edit["record_identity"]
            ):
                raise AssertionError(f"imported field edit did not invalidate application: {edited}")
            app_builds.append(edited)

            invalid_request = request(INVALID_DEPENDENCY)
            invalid_build, invalid_metrics = probe.send({"compile_application": invalid_request})
            invalid = build_observation("invalid_imported_value_field", invalid_request,
                                        invalid_build, invalid_metrics)
            if (
                invalid["status"] != "rejected"
                or invalid["cache_status"] != "bypass"
                or invalid["backend_artifact"] != "not_generated"
                or invalid["front_end_invocations"] != 1
                or invalid["artifact_identity"] is not None
                or invalid["diagnostic_codes"] != oracle_invalid["diagnostic_codes"]
            ):
                raise AssertionError(f"invalid source was masked by prior cache entries: {invalid}")
            app_builds.append(invalid)
            probe_stderr = probe.close()
        except BaseException:
            if probe.proc.poll() is None:
                probe.close()
            raise

    test_vm_emit.ensure_vm()
    with tempfile.TemporaryDirectory(prefix="imported-nominal-cache-vm-") as vm_directory:
        vm_root = Path(vm_directory)
        base_vm = canonical_vm_call(
            base_build, {"boolean": {"value": False}}, "base_record_read", vm_root,
        )
        edit_vm = canonical_vm_call(
            edited_build,
            {"integer": {"type": {"bits": 8, "signed": False}, "value": 7}},
            "edited_record_read",
            vm_root,
        )
    if base_vm["returned"] != edit_vm["returned"]:
        raise AssertionError("imported field interface edit changed the selected read result")

    head = git(ROOT, "rev-parse", "HEAD")
    source_files = [
        "tools/stage0-probe/src/application.rs",
        "tools/test_vm_project_imported_flow_ssa.py",
        "tools/test_vm_application_cache_imported_nominal.py",
    ]
    source_hashes = {name: sha((ROOT / name).read_bytes()) for name in source_files}
    status = git(ROOT, "status", "--porcelain")
    cgroup_after = support.cgroup_snapshot()
    report = {
        "schema_version": "mncs.compiler.imported-nominal-application-cache/1",
        "kind": "imported-nominal-application-cache-invalidation",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "compiler": {
            "revision": head,
            "branch": git(ROOT, "branch", "--show-current"),
            "worktree": str(ROOT),
            "source_tree_dirty_at_test": bool(status),
            "source_tree_files_sha256": source_hashes,
            "probe_executable_sha256": sha(support.PROBE.read_bytes()),
            "harness_sha256": source_hashes["tools/test_vm_application_cache_imported_nominal.py"],
        },
        "toolchains": {
            "stage0_probe_sha256": sha(support.PROBE.read_bytes()),
            "canonical_vm_revision": git(test_vm_emit.VM_REPO, "rev-parse", "HEAD"),
            "canonical_vm_executable_sha256": sha(test_vm_emit.VM_BIN.read_bytes()),
        },
        "workload": {
            "entry": "demo.root::read",
            "modules": ["demo.root", "demo.dep"],
            "record": "demo.dep::Box",
            "changed_source": "demo.dep::Box.padding: bool -> u8",
            "stable_projected_field": "demo.dep::Box.value: u64",
            "stage0_record_identity_changed": (
                oracle_base["record_identity"] != oracle_edit["record_identity"]
            ),
        },
        "stage0_oracle": oracle_rows,
        "application_builds": app_builds,
        "probe_stderr_sha256": sha(probe_stderr.encode()),
        "canonical_vm": {
            "runs": [base_vm, edit_vm],
            "equivalent_returned_value": base_vm["returned"] == edit_vm["returned"],
        },
        "semantic_verification": {
            "language_verified_facts_reused": 0,
            "changed_valid_source_frontend_invocations": [
                app_builds[2]["front_end_invocations"],
            ],
            "invalid_source_frontend_invocations": app_builds[3]["front_end_invocations"],
            "invalid_source_diagnostics_match_stage0": (
                app_builds[3]["diagnostic_codes"] == oracle_invalid["diagnostic_codes"]
            ),
        },
        "cache_contract": {
            "application_cache_environment": "MNCS_PROBE_APPLICATION_CACHE_DIR",
            "program_cache_environment": "MNCS_PROBE_CACHE_DIR",
            "imported_field_edit_regenerated_artifact": (
                app_builds[2]["artifact_identity"] != app_builds[0]["artifact_identity"]
            ),
            "invalid_source_bypassed_prior_success": app_builds[3]["status"] == "rejected",
        },
        "measurements": {
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "host_memory_before": host_before,
            "cgroup_memory_before": cgroup_before,
            "cgroup_memory_after": cgroup_after,
            "cgroup_peak_attributable_to_test": False,
        },
    }
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": str(EVIDENCE_PATH.relative_to(ROOT)),
        "evidence_sha256": sha(EVIDENCE_PATH.read_bytes()),
        "application_builds": [
            {
                "label": row["label"],
                "status": row["status"],
                "cache_status": row["cache_status"],
                "front_end_invocations": row["front_end_invocations"],
                "artifact_identity": row["artifact_identity"],
            }
            for row in app_builds
        ],
        "canonical_vm_returns": [row["returned"] for row in (base_vm, edit_vm)],
        "language_verified_facts_reused": 0,
        "whole_frontier_seconds": report["measurements"]["whole_frontier_seconds"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
