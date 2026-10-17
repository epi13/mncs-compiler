#!/usr/bin/env python3
"""Compile and run Automation's native evaluation through application-build."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import test_vm_native_application as support  # noqa: E402


def find_repository(name: str, env_name: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured).resolve()
        if not (path / ".git").exists():
            raise SystemExit(f"{env_name} is not a Git checkout: {path}")
        return path
    for parent in ROOT.parents:
        candidate = parent / name
        if (candidate / ".git").exists():
            return candidate
    raise SystemExit(f"could not locate sibling repository {name}; set {env_name}")


AUTOMATION_ROOT = find_repository("mncs-automation", "MNCS_AUTOMATION_ROOT")
TEST_ROOT = find_repository("mncs-test", "MNCS_TEST_ROOT")
VM_ROOT = find_repository("mncs-vm", "MNCS_VM_ROOT")
PROBE = Path(os.environ.get(
    "MNCS_AUTOMATION_PROBE_BIN",
    ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe",
))
VM_BIN = Path(os.environ.get("MNCS_AUTOMATION_VM_BIN", VM_ROOT / "target/debug/mncs-vm"))
CACHE_ROOT = ROOT / ".build/native-app-readiness/automation-application-cache"
AUTOMATION_MODULE = "mncs.automation.evaluation.v1"
ROOT_SOURCE_PATH = AUTOMATION_ROOT / "native/mncs/automation/evaluation.mncs"
MODULE_PATHS = {
    # Automation imports the versioned declarations through their stable
    # source paths, so these keys intentionally match the source import names.
    "mncs.automation.schedule": AUTOMATION_ROOT / "native/mncs/automation/schedule.mncs",
    "mncs.automation.trigger": AUTOMATION_ROOT / "native/mncs/automation/trigger.mncs",
    "mncs.test.assertions": TEST_ROOT / "native/mncs/test/assertions.mncs",
    "mncs.test.suite": TEST_ROOT / "native/mncs/test/suite.mncs",
}
ROOTS = [
    {"module": AUTOMATION_MODULE, "function": "evaluate_tick"},
    {"module": AUTOMATION_MODULE, "function": "evaluate_resource_headroom"},
]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def host_memory_observation() -> dict | None:
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                fields[key] = int(raw.strip().split()[0]) * 1024
        if set(fields) != {"MemTotal", "MemAvailable"}:
            return None
        return {"total_bytes": fields["MemTotal"],
                "available_bytes": fields["MemAvailable"]}
    except (OSError, ValueError, IndexError):
        return None


def unsigned(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def signed(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": True}, "value": value}}


def tick_args(values: tuple[int, ...]) -> list[dict]:
    if len(values) != 13:
        raise ValueError("evaluate_tick requires 13 ordered arguments")
    signed_positions = {1, 2, 8, 9}
    return [signed(value) if index in signed_positions else unsigned(value)
            for index, value in enumerate(values)]


def resource_args(values: tuple[int, ...]) -> list[dict]:
    if len(values) != 9:
        raise ValueError("evaluate_resource_headroom requires 9 ordered arguments")
    return [signed(value) for value in values]


def workloads() -> list[dict]:
    tick_cases = [
        ("pinned-first-fire", (0, 1000, 1, 0, 0, 0, 0, 0, 0, 1000, 0, 10, 0)),
        ("pinned-replay", (0, 1000, 1, 0, 0, 0, 0, 1, 1000, 5000, 0, 10, 0)),
        ("interval-first-align", (1, 0, 1000, 0, 0, 0, 0, 0, 0, 5500, 0, 10, 1)),
        ("interval-fire-now", (1, 0, 1000, 0, 0, 0, 0, 2, 2000, 5500, 0, 10, 0)),
        ("interval-catch-up-bound", (1, 0, 1000, 0, 0, 0, 0, 2, 2000, 9000, 2, 3, 0)),
        ("interval-skip", (1, 0, 1000, 0, 0, 0, 0, 0, 0, 5500, 1, 10, 0)),
        ("interval-wait", (1, 0, 1000, 0, 0, 0, 0, 0, 0, 100, 0, 10, 0)),
        ("watch-rising-edge", (0, 0, 0, 1, 0, 0, 1, 4, 0, 9000, 0, 10, 0)),
        ("watch-unknown", (0, 0, 0, 1, 0, 0, 2, 4, 0, 9000, 0, 10, 0)),
        ("daily-utc-first-align", (2, 18000000, 0, 0, 0, 0, 0, 0, 0, 21600000, 0, 10, 1)),
        ("daily-local-defers", (3, 300, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10, 0)),
    ]
    rows = []
    for name, values in tick_cases:
        rows.append({
            "id": name,
            "function": "evaluate_tick",
            "arguments": tick_args(values),
        })
    resource_cases = [
        ("headroom-ready", (10000, 15000, 60000, 8192, 4096, 3000, 3500, 125, 256)),
        ("headroom-envelope-limit", (10000, 15000, 60000, 8192, 4096, 5000, 3500, 125, 256)),
        ("headroom-current-reserve", (10000, 15000, 60000, 4000, 4096, 3000, 3500, 125, 256)),
        ("headroom-future-observation", (20000, 10000, 60000, 8192, 4096, 3000, 3500, 125, 256)),
        ("headroom-stale-observation", (10000, 80000, 60000, 8192, 4096, 3000, 3500, 125, 256)),
        ("headroom-invalid-scale", (10000, 15000, 60000, 8192, 4096, 3000, 3500, 99, 256)),
    ]
    for name, values in resource_cases:
        rows.append({
            "id": name,
            "function": "evaluate_resource_headroom",
            "arguments": resource_args(values),
        })
    return rows


def build_request(root_source: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/automation/evaluation.mncs",
        "root_source": root_source,
        "callable_roots": ROOTS,
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "automation-evaluation-native-app/1",
    }


def app_call(row: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": AUTOMATION_MODULE, "function": row["function"]},
        "arguments": row["arguments"],
        "type_arguments": [],
        "step_budget": 100_000,
    }


def run_vm_batch(artifact: dict, rows: list[dict]) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory(prefix="mncs-automation-app-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        calls = [{"id": row["id"], "request": app_call(row)} for row in rows]
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n")
        started = time.monotonic()
        completed = subprocess.run(
            ["/usr/bin/time", "-v", str(VM_BIN), "batch", "--artifact",
             str(artifact_path), "--calls", str(calls_path), "--output",
             str(output_path)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        wall = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(f"canonical VM batch failed: {completed.stderr[-4000:]}")
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        peak_rss_kib = int(match.group(1)) if match else None
        user = re.search(r"User time \(seconds\): ([0-9.]+)", completed.stderr)
        system = re.search(r"System time \(seconds\): ([0-9.]+)", completed.stderr)
        document = json.loads(output_path.read_text())
        return document.get("results", []), {
            "wall_seconds": round(wall, 6),
            "user_cpu_seconds": float(user.group(1)) if user else None,
            "system_cpu_seconds": float(system.group(1)) if system else None,
            "cpu_seconds": (round(float(user.group(1)) + float(system.group(1)), 6)
                            if user and system else None),
            "peak_rss_kib": peak_rss_kib,
            "result_schema_version": document.get("schema_version"),
            "artifact_id": document.get("artifact_id"),
        }


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing Stage-0 probe or canonical VM binary: {PROBE}, {VM_BIN}")
    root_source = ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {name: path.read_text(encoding="utf-8")
               for name, path in MODULE_PATHS.items()}
    rows = workloads()
    calls = [app_call(row) for row in rows]
    host_memory_before = host_memory_observation()
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    run_cache = CACHE_ROOT / f"run-{os.getpid()}-{time.time_ns()}"
    run_cache.mkdir()

    probe = support.Probe(run_cache / "program-cache", executable=PROBE,
                          application_cache_dir=run_cache / "application-cache")
    try:
        cold, cold_metrics = probe.send(
            {"compile_application": build_request(root_source, modules)}, timeout=240,
        )
        if (cold.get("schema_version") != "mncs.compiler.application-build/5"
                or cold.get("status") != "verified"
                or cold.get("cache", {}).get("status") != "miss"):
            raise AssertionError("Automation application did not produce a verified cold build: "
                                 + json.dumps({"status": cold.get("status"),
                                               "diagnostics": cold.get("diagnostics"),
                                               "obligations": cold.get("unresolved_obligations")},
                                              sort_keys=True))
        artifact = (cold.get("artifact") or {}).get("value")
        artifact_identity = (cold.get("artifact") or {}).get("identity")
        if not isinstance(artifact, dict) or artifact.get("artifact_id") != artifact_identity:
            raise AssertionError("verified Automation build omitted its identity-bound VM artifact")

        oracle, oracle_metrics = probe.send({"project_oracle": {
            "root": root_source,
            "modules": modules,
            "calls": calls,
        }}, timeout=240)
        executions = oracle.get("executions", [])
        if (oracle.get("valid") is not True or len(executions) != len(rows)
                or any(item.get("status") != "returned" for item in executions)):
            raise AssertionError("Stage-0 Automation oracle failed: "
                                 + json.dumps({"valid": oracle.get("valid"),
                                               "diagnostics": oracle.get("diagnostics"),
                                               "statuses": [item.get("status") for item in executions]},
                                              sort_keys=True))
        expected = [support.decode_wire(item["returned"][0])
                    for item in executions]
        oracle_steps = [item.get("steps") for item in executions]

        vm_results, vm_metrics = run_vm_batch(artifact, rows)
        if len(vm_results) != len(rows):
            raise AssertionError(f"canonical VM returned {len(vm_results)} of {len(rows)} cases")
        actual = []
        for row, vm_row, expected_value in zip(rows, vm_results, expected):
            if vm_row.get("id") != row["id"] or vm_row.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"canonical VM call failed for {row['id']}: {vm_row}")
            returned = vm_row.get("record", {}).get("returned", [])
            if len(returned) != 1:
                raise AssertionError(f"canonical VM returned malformed result for {row['id']}")
            value = support.decode_wire(support.vm_to_wire(returned[0]))
            if value != expected_value:
                raise AssertionError(f"Automation Stage-0/VM disagreement for {row['id']}: "
                                     + json.dumps({"vm": value, "stage0": expected_value},
                                                  sort_keys=True))
            actual.append(value)

        probe.close()
        probe = support.Probe(run_cache / "program-cache", executable=PROBE,
                              application_cache_dir=run_cache / "application-cache")
        warm, warm_metrics = probe.send(
            {"compile_application": build_request(root_source, modules)}, timeout=240,
        )
        warm_work = warm.get("cache", {}).get("verification_work", {})
        if (warm.get("status") != "verified"
                or warm.get("cache", {}).get("status") != "hit"
                or warm.get("artifact", {}).get("identity") != artifact_identity
                or warm_work.get("front_end_invocations") != 0):
            raise AssertionError("fresh-process Automation exact-snapshot reuse changed")

        comment_modules = dict(modules)
        comment_modules["mncs.test.assertions"] += "\n// source-only Automation cache check\n"
        changed, changed_metrics = probe.send({
            "compile_application": build_request(root_source, comment_modules),
        }, timeout=240)
        changed_work = changed.get("cache", {}).get("verification_work", {})
        if (changed.get("status") != "verified"
                or changed.get("artifact", {}).get("identity") != artifact_identity
                or changed_work.get("front_end_invocations") != 1
                or changed_work.get("reused_semantic_facts") != []
                or changed_work.get("backend_artifact") != "reused"
                or changed.get("cache", {}).get("reuse_basis")
                != "verified_application_executable_closure"):
            raise AssertionError("comment-only Automation dependency edit failed fresh full "
                                 "verification with executable-closure artifact reuse")
        probe.close()
    except BaseException:
        try:
            if probe.proc.poll() is None:
                probe.proc.kill()
                probe.proc.wait(timeout=5)
        except Exception:
            pass
        raise

    module_sources = {name: {"path": str(path.relative_to(AUTOMATION_ROOT)
                                         if path.is_relative_to(AUTOMATION_ROOT)
                                         else path.relative_to(TEST_ROOT)),
                             "sha256": sha(path.read_bytes()),
                             "bytes": path.stat().st_size}
                      for name, path in sorted(MODULE_PATHS.items())}
    host_memory_after = host_memory_observation()
    evidence = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-automation-evaluation-application",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "environment_session_id": "ses_164edc158d9b1637",
        "compiler": {
            "revision_before_harness_commit": git(ROOT, "rev-parse", "HEAD"),
            "branch": git(ROOT, "branch", "--show-current"),
            "probe_sha256": sha(PROBE.read_bytes()),
            "verification_engine_identity": cold.get("cache", {}).get(
                "verification_work", {}).get("verification_engine_identity"),
        },
        "automation": {
            "revision": git(AUTOMATION_ROOT, "rev-parse", "HEAD"),
            "worktree_status": git(AUTOMATION_ROOT, "status", "--porcelain"),
            "root_module": "mncs.automation.evaluation.v1",
            "root_source_sha256": sha(ROOT_SOURCE_PATH.read_bytes()),
            "modules": module_sources,
        },
        "mncs_test": {
            "revision": git(TEST_ROOT, "rev-parse", "HEAD"),
            "worktree_status": git(TEST_ROOT, "status", "--porcelain"),
        },
        "mncs_vm": {
            "revision": git(VM_ROOT, "rev-parse", "HEAD"),
            "binary_sha256": sha(VM_BIN.read_bytes()),
        },
        "application": {
            "request_schema": "mncs.compiler.application-build-request/2",
            "response_schema": cold.get("schema_version"),
            "selected_callables": ROOTS,
            "source_snapshot_identity": cold.get("source_snapshot", {}).get("identity"),
            "semantic_identity": cold.get("dependency_graph", {}).get("semantic_identity"),
            "artifact_identity": artifact_identity,
            "artifact_bytes": len(json.dumps(artifact, sort_keys=True).encode("utf-8")),
            "cold_verification_work": cold.get("cache", {}).get("verification_work"),
            "fresh_process_reuse": {
                "cache_status": warm.get("cache", {}).get("status"),
                "frontend_invocations": warm_work.get("front_end_invocations"),
                "artifact_identity": warm.get("artifact", {}).get("identity"),
            },
            "comment_dependency_edit": {
                "source_snapshot_identity": changed.get("source_snapshot", {}).get("identity"),
                "cache_status": changed.get("cache", {}).get("status"),
                "reuse_basis": changed.get("cache", {}).get("reuse_basis"),
                "frontend_invocations": changed_work.get("front_end_invocations"),
                "reused_semantic_facts": changed_work.get("reused_semantic_facts"),
                "artifact_decision": changed_work.get("backend_artifact"),
                "artifact_identity": changed.get("artifact", {}).get("identity"),
            },
        },
        "execution": {
            "oracle": "Stage-0 project_oracle",
            "canonical_runtime": "mncs-vm batch",
            "case_count": len(rows),
            "cases": [{"id": row["id"], "function": row["function"],
                       "stage0_steps": steps, "result": value}
                      for row, steps, value in zip(rows, oracle_steps, actual)],
            "exact_stage0_vm_agreement": True,
            "normalized_results_sha256": sha(json.dumps(
                [{"id": row["id"], "result": value}
                 for row, value in zip(rows, actual)],
                sort_keys=True, separators=(",", ":")).encode()),
        },
        "measurements": {
            "cold_build": cold_metrics,
            "stage0_oracle": oracle_metrics,
            "canonical_vm_batch": vm_metrics,
            "fresh_process_exact_snapshot": warm_metrics,
            "comment_only_dependency_edit": changed_metrics,
            "host_memory_before": host_memory_before,
            "host_memory_after": host_memory_after,
            "cgroup_memory_measurement": "unavailable",
        },
        "language_verified_facts_reused": 0,
        "forge_execution_performed": False,
        "canonical_store_publication_performed": False,
        "production_automation_orchestration_replaced": False,
        "scope": "The Compiler built actual mncs-automation schedule, trigger, and evaluation source with its real mncs-test dependencies into one application-build/5 artifact. Seventeen deterministic evaluation inputs matched the independent Stage-0 project oracle in one canonical VM batch. Exact-snapshot fresh-process reuse passed. A comment-only imported-source edit received a fresh full frontend check, reused the verified executable-closure artifact, and preserved the canonical VM artifact identity. Language verified facts remained zero, so changed snapshots still rechecked all source semantics. This is Compiler application and execution evidence; it does not prove Forge/Environment/Store routing or replacement of Automation host orchestration.",
    }
    default_evidence_name = (
        "evidence/native-app-readiness-automation-evaluation-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    evidence_relative_path = os.environ.get(
        "MNCS_AUTOMATION_EVIDENCE_PATH", default_evidence_name
    )
    evidence_path = ROOT / evidence_relative_path
    if evidence_path.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {evidence_path}")
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(evidence_path),
                      "artifact_identity": artifact_identity,
                      "case_count": len(rows),
                      "cold_seconds": cold_metrics.get("wall_seconds"),
                      "vm_seconds": vm_metrics.get("wall_seconds"),
                      "language_verified_facts_reused": 0,
                      "evidence_sha256": sha(evidence_path.read_bytes())}, indent=2))


if __name__ == "__main__":
    main()
