#!/usr/bin/env python3
"""Admit one Unicode-named module through the native Project compiler."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def main() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("cranelift", "reference_interpreter"),
                        default="cranelift")
    args = parser.parse_args()
    os.environ["MNCS_PROBE_BACKEND"] = args.backend
    os.environ["MNCS_PROBE_TELEMETRY"] = "1"

    import test_project as project

    text = "mncs 0.18; module café; fn value() -> (r: u64) { return 1; }"
    raw = text.encode("utf-8")
    source_id = "café"
    source_path = "café.mncs"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("src/compiler/source.mncs", "src/compiler/lexer.mncs",
                     "src/compiler/parser.mncs", "src/compiler/kernel.mncs",
                     "tools/generate_unicode_classifier.rs")
    }
    probe_started = time.monotonic()
    probe = project.Probe()
    try:
        identities = project.identity_map(probe)
        status = probe.send({"execution_status": True})
        if args.backend == "cranelift" and status.get("backend") != "cranelift":
            raise AssertionError({"execution_status": status})
        oracle = probe.send({"project_oracle": {"root": text, "modules": {}}})
        if oracle.get("valid") is not True or oracle.get("diagnostics"):
            raise AssertionError({"stage0_oracle": oracle})

        request = project.request_value(
            identities, [(source_id, source_path, raw)], stride=1024,
        )
        request_started = time.monotonic()
        native = probe.native(request)
        request_wall_seconds = time.monotonic() - request_started
        probe_wall_seconds = time.monotonic() - probe_started
        if native.get("valid") is not True:
            raise AssertionError({"native_project": native})
        if native.get("module_count") != 1 or native.get("value_ssa_valid") is not True:
            raise AssertionError({"native_project": native})
        diagnostics = native.get("diagnostics")
        if diagnostics not in ([], {"$v": 0, "$p": {}}):
            raise AssertionError({"native_diagnostics": diagnostics})

        observations = probe.request_observations
        resource_summaries = [
            item["resource_summary"] for item in observations
            if isinstance(item.get("resource_summary"), dict)
        ]
        aggregate_resources = {
            key: max((summary[key] for summary in resource_summaries
                      if isinstance(summary.get(key), int)), default=None)
            for key in ("peak_rss_bytes", "max_kernel_rss_high_water_bytes",
                        "peak_fd_count", "peak_threads")
        }
        cpu_deltas = [summary.get("cpu_time_delta_ns")
                      for summary in resource_summaries]
        aggregate_resources["cpu_time_delta_ns"] = (
            sum(cpu_deltas) if cpu_deltas and all(isinstance(value, int)
                                                  for value in cpu_deltas)
            else None
        )
        for field in ("io_read_bytes_delta", "io_write_bytes_delta"):
            deltas = [summary.get(field) for summary in resource_summaries]
            aggregate_resources[field] = (
                sum(deltas) if deltas and all(isinstance(value, int)
                                              for value in deltas)
                else None
            )
        aggregate_resources["request_sample_count"] = len(resource_summaries)
        aggregate_resources["cgroup_status"] = (
            observations[0].get("cgroup_before", {}).get("status")
            if observations else None
        )
        waits_ns = [item.get("wait_elapsed_ns") for item in observations]
        aggregate_resources["total_request_wait_seconds"] = (
            round(sum(waits_ns) / 1_000_000_000, 6)
            if waits_ns and all(isinstance(value, int) for value in waits_ns)
            else None
        )
        result = {
            "schema_version": "mncs.compiler.unicode-project-module/1",
            "status": "verified",
            "compiler_revision": revision,
            "source_identity_capture": "SHA-256 values captured before spawning the verification process",
            "backend": args.backend,
            "source_id": source_id,
            "source_path": source_path,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_hashes": source_hashes,
            "source_hashes_capture_point": "before Probe construction",
            "stage0_oracle": {
                "valid": oracle["valid"],
                "diagnostic_count": len(oracle.get("diagnostics", [])),
            },
            "native_project": {
                "valid": native["valid"],
                "module_count": native["module_count"],
                "value_ssa_valid": native["value_ssa_valid"],
                "diagnostics": diagnostics,
            },
            "step_budget": request["step_budget"],
            "wall_seconds": round(probe_wall_seconds, 6),
            "probe_wall_seconds": round(probe_wall_seconds, 6),
            "native_request_wall_seconds": round(request_wall_seconds, 6),
            "probe_request_count": len(observations),
            "resource_observation": aggregate_resources,
            "execution": "native Project::compile_project produced verified SSA for the Unicode module",
            "limitations": [
                "This is a one-module Project admission and SSA check, not a complete native application execution.",
                "No Language verified facts were reused in this work.",
            ],
        }
        evidence_path = ROOT / "evidence/project-unicode-module-20261019.json"
        evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
