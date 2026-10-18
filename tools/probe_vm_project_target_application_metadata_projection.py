#!/usr/bin/env python3
"""Measure selected-SSA metadata projection on the Project application closure."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import probe_vm_project_target_application_closure as probe  # noqa: E402

probe.BUILD_CACHE = ROOT / ".build/project-target-application-metadata-projection-verified-cache"
probe.ARTIFACT_PATH = ROOT / ".build/project-target-application-metadata-projection.vm.json"
probe.EVIDENCE_PATH = ROOT / "evidence/project-target-application-metadata-projection-20261018.json"


def build_stage0_probe() -> dict:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(ROOT / "tools/stage0-probe/target")
    started = time.perf_counter()
    completed = subprocess.run(
        ["cargo", "build", "--release", "--manifest-path", str(ROOT / "tools/stage0-probe/Cargo.toml")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    binary = ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"
    return {
        "wall_seconds": round(time.perf_counter() - started, 6),
        "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "stdout_tail": completed.stdout[-1200:],
        "stderr_tail": completed.stderr[-1200:],
    }


def main() -> dict:
    previous_report = (
        json.loads(probe.EVIDENCE_PATH.read_text())
        if probe.EVIDENCE_PATH.is_file()
        else None
    )
    stage0_build = build_stage0_probe()
    report = probe.main()
    report.update({
        "test_harness": "tools/probe_vm_project_target_application_metadata_projection.py",
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "baseline_application_artifact_json_bytes": 132910815,
        "baseline_application_artifact_identity": "sha256:76e70078e0dffc0e81bd1dbcd20cb4b9c2d24374f939759eef70c0e1c7108b57",
        "projection_contract": "selected functions, their obligation dependency closure, selected source trace and transformation records, and reachable generic specializations; whole-program binding table omitted while original full SSA fingerprint remains in artifact source",
        "stage0_probe_build": stage0_build,
        "evidence_scope": "The Compiler verified a selected callable closure containing module_resolution_cache_lookup_done and a concrete compile_project_target specialization. The independent Stage-0 Project oracle was valid on the same fixture. Canonical VM execution completed and its full returned Project result matched the Cranelift result. This does not establish Language verified-fact reuse or Stage-1/Stage-2 succession.",
    })
    metrics = report.get("application_build_metrics", {})
    current_measurement = {
        "cache_status": report.get("application_build_cache_status"),
        "application_build_identity": report.get("application_build_identity"),
        "application_artifact_identity": report.get("application_artifact_identity"),
        "application_artifact_json_bytes": report.get("application_artifact_json_bytes"),
        "application_artifact_json_sha256": report.get("application_artifact_json_sha256"),
        "wall_seconds": report.get("application_build_wall_seconds"),
        "cpu_seconds": metrics.get("cpu_seconds"),
        "peak_rss_bytes": metrics.get("peak_rss_bytes"),
        "peak_cgroup_memory_bytes": metrics.get("peak_cgroup_memory_bytes"),
    }
    if previous_report and previous_report.get("application_build_cache_status") == "miss":
        cold_identity = previous_report.get("application_artifact_identity")
        same_artifact = cold_identity == report.get("application_artifact_identity")
        if report.get("application_build_cache_status") == "hit" and not same_artifact:
            raise AssertionError("fresh-process cache hit changed the application artifact identity")
        cold_metrics = previous_report.get("application_build_metrics", {})
        cold_measurement = {
            "cache_status": previous_report.get("application_build_cache_status"),
            "application_build_identity": previous_report.get("application_build_identity"),
            "application_artifact_identity": cold_identity,
            "application_artifact_json_bytes": previous_report.get("application_artifact_json_bytes"),
            "application_artifact_json_sha256": previous_report.get("application_artifact_json_sha256"),
            "wall_seconds": previous_report.get("application_build_wall_seconds"),
            "cpu_seconds": cold_metrics.get("cpu_seconds"),
            "peak_rss_bytes": cold_metrics.get("peak_rss_bytes"),
            "peak_cgroup_memory_bytes": cold_metrics.get("peak_cgroup_memory_bytes"),
            "canonical_vm_outcome": previous_report.get("canonical_vm_outcome"),
            "canonical_vm_semantics_executed": previous_report.get("canonical_vm_semantics_executed"),
            "cranelift_canonical_vm_equivalent": previous_report.get("cranelift_canonical_vm_equivalent"),
        }
        report["cold_build_measurement"] = cold_measurement
        report["initial_cache_miss_measurement"] = cold_measurement
        report["fresh_process_cache_reuse"] = {
            "status": "passed" if same_artifact and report.get("application_build_cache_status") == "hit" else "not_confirmed",
            "cache_status": report.get("application_build_cache_status"),
            "application_artifact_identity_matches_cold": same_artifact,
            "application_artifact_identity": report.get("application_artifact_identity"),
            "wall_seconds": current_measurement["wall_seconds"],
            "cpu_seconds": current_measurement["cpu_seconds"],
            "peak_rss_bytes": current_measurement["peak_rss_bytes"],
            "peak_cgroup_memory_bytes": current_measurement["peak_cgroup_memory_bytes"],
            "canonical_vm_outcome": report.get("canonical_vm_outcome"),
            "canonical_vm_semantics_executed": report.get("canonical_vm_semantics_executed"),
            "cranelift_canonical_vm_equivalent": report.get("cranelift_canonical_vm_equivalent"),
        }
    else:
        report["cold_build_measurement"] = current_measurement
    probe.EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
