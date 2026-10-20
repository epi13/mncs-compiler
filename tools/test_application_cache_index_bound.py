#!/usr/bin/env python3
"""Verify oversized application cache indexes fall back on real digest builds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = next(
    parent for parent in ROOT.parents
    if (parent / "mncs-test/native/mncs/test/digest.mncs").is_file()
)
os.environ.setdefault("MNCS_PROJECTS_ROOT", str(WORKSPACE))
sys.path.insert(0, str(ROOT / "tools"))
import test_vm_native_application as support
import test_vm_test_digest_incremental as digest

INDEX_LIMIT = 8 * 1024
ENTRY_MODULE = "mncs.test.digest"
ENTRY_FUNCTION = "build_digest"
ROOT_SOURCE_PATH = WORKSPACE / "mncs-test/native/mncs/test/digest.mncs"
MODULE_PATHS = {
    "mncs.test.assertions": WORKSPACE / "mncs-test/native/mncs/test/assertions.mncs",
    "mncs.test.suite": WORKSPACE / "mncs-test/native/mncs/test/suite.mncs",
    "mncs.std.application.v1": WORKSPACE / "mncs-stdlib/library/std/application.mncs",
}
PROBE = Path(os.environ.get(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / ".build/native-app-readiness/stage0-target/release/mncs-compiler-stage0-probe"),
))
EVIDENCE_PATH = ROOT / "evidence" / (
    f"application-cache-index-digest-integration-"
    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{os.getpid()}.json"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def application_request(root_source: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/digest.mncs",
        "root_source": root_source,
        "entry": {"module": ENTRY_MODULE, "function": ENTRY_FUNCTION},
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def compile_with_trace(request: dict, cache_dir: Path, trace_path: Path) -> tuple[dict, dict, list[dict]]:
    os.environ["MNCS_PROBE_TELEMETRY"] = "1"
    os.environ["MNCS_PROBE_TRACE_PATH"] = str(trace_path)
    probe = support.Probe(cache_dir, executable=PROBE)
    try:
        response, metrics = probe.send({"compile_application": request}, timeout=90)
        probe.close()
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise
    finally:
        os.environ.pop("MNCS_PROBE_TRACE_PATH", None)
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line]
    return response, metrics, events


def assert_full_frontend(response: dict, label: str) -> dict:
    if response.get("status") != "verified":
        raise AssertionError(f"{label} was not verified: {response.get('diagnostics')}")
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    if cache.get("schema_version") != "mncs.compiler.application-build-cache-observation/4":
        raise AssertionError(f"{label} omitted current cache observation: {cache}")
    if work.get("schema_version") != "mncs.compiler.application-build-verification-work/2":
        raise AssertionError(f"{label} omitted verification work: {work}")
    if work.get("front_end_invocations") != 1 or work.get("front_end_outcome") != "verified":
        raise AssertionError(f"{label} did not report one verified frontend invocation: {work}")
    for stage in (
        "source_parsing", "declaration_resolution", "signature_verification",
        "function_body_verification",
    ):
        if work.get(stage) != "recomputed_by_full_frontend":
            raise AssertionError(f"{label} did not report fresh {stage}: {work}")
    return work


def main() -> None:
    if not PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit("current Stage-0 probe or canonical VM executable is missing")
    root_source = ROOT_SOURCE_PATH.read_text()
    modules = {name: path.read_text() for name, path in MODULE_PATHS.items()}
    base_request = application_request(root_source, modules)
    with tempfile.TemporaryDirectory(prefix="mncs-cache-index-bound-") as raw_cache:
        cache_dir = Path(raw_cache)
        first_trace = cache_dir / "first-trace.jsonl"
        cold, cold_metrics, cold_events = compile_with_trace(
            base_request, cache_dir, first_trace
        )
        cold_work = assert_full_frontend(cold, "cold digest build")
        if cold.get("cache", {}).get("status") != "miss":
            raise AssertionError("cold digest build did not miss the empty cache")
        indexes = list((cache_dir / "application-build-indexes").glob("*.json"))
        if len(indexes) != 1:
            raise AssertionError(f"expected one cache index, found {len(indexes)}")
        index_path = indexes[0]
        original_index = json.loads(index_path.read_bytes())
        if len(index_path.read_bytes()) > INDEX_LIMIT:
            raise AssertionError("freshly published index unexpectedly exceeds its bound")

        # The index key is rooted in the unchanged entry source; changing an
        # imported module keeps the lookup path stable while forcing a miss.
        edited_modules = dict(modules)
        edited_modules["mncs.test.suite"] += "\n// exercise bounded index rejection\n"
        edited_request = application_request(root_source, edited_modules)
        oversized_bytes = INDEX_LIMIT + 1
        index_path.write_bytes(b"x" * oversized_bytes)
        second_trace = cache_dir / "oversized-index-trace.jsonl"
        edited, edited_metrics, edited_events = compile_with_trace(
            edited_request, cache_dir, second_trace
        )
        edited_work = assert_full_frontend(edited, "oversized-index fallback build")
        if edited.get("cache", {}).get("status") != "miss":
            raise AssertionError("oversized index did not fall back to a fresh build")
        if edited.get("cache", {}).get("publication") != "stored":
            raise AssertionError("fallback build did not publish a complete cache entry")
        lookup = [
            event for event in edited_events
            if event.get("phase") == "application_cache_semantic_candidate_lookup"
        ]
        if len(lookup) != 1 or lookup[0].get("candidate_loaded") is not False:
            raise AssertionError(f"oversized index candidate was not rejected: {lookup}")
        if lookup[0].get("index_configured") is not True:
            raise AssertionError("the fallback did not exercise the configured index path")

        rewritten_index_bytes = index_path.read_bytes()
        rewritten_index = json.loads(rewritten_index_bytes)
        if len(rewritten_index_bytes) > INDEX_LIMIT:
            raise AssertionError("fallback did not replace the oversized index with a bounded one")
        if rewritten_index.get("schema_version") != "mncs.compiler.application-build-cache-index/2":
            raise AssertionError("fallback wrote an unexpected index schema")
        if rewritten_index.get("key") != original_index.get("key"):
            raise AssertionError("fallback changed the stable root-source index identity")
        if rewritten_index.get("candidate_key") != edited.get("cache", {}).get("key"):
            raise AssertionError("fallback index does not point at the complete edited build")

        fixture = json.loads((ROOT / "evidence/native-app-readiness-test-digest.json").read_text())
        argument = fixture["execution"]["inputs"][0]
        cold_argument = digest.bind_artifact(argument, cold["artifact"]["identity"])
        edited_argument = digest.bind_artifact(argument, edited["artifact"]["identity"])
        cold_actual, cold_vm_metrics, cold_receipt = digest.run_vm(
            cold["artifact"]["value"], cold["artifact"]["identity"], cold_argument
        )
        edited_actual, edited_vm_metrics, edited_receipt = digest.run_vm(
            edited["artifact"]["value"], edited["artifact"]["identity"], edited_argument
        )
        if cold_actual != edited_actual:
            raise AssertionError("oversized-index fallback changed canonical VM behavior")

        evidence = {
            "schema_version": "mncs.compiler.application-cache-index-digest-integration/1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": "passed",
            "contract": {
                "maximum_index_bytes": INDEX_LIMIT,
                "oversized_index_bytes": oversized_bytes,
                "oversized_index_is_ignored": True,
                "fallback_recomputes_complete_frontend": True,
                "fallback_republishes_bounded_index": True,
                "canonical_vm_outputs_equivalent": True,
            },
            "application": {
                "entry": f"{ENTRY_MODULE}::{ENTRY_FUNCTION}",
                "root_source_sha256": sha(root_source.encode()),
                "changed_dependency": "mncs.test.suite",
                "changed_dependency_source_sha256": sha(edited_modules["mncs.test.suite"].encode()),
            },
            "producer": {
                "compiler_revision": git(ROOT, "rev-parse", "HEAD"),
                "language_revision": git(WORKSPACE / "mncs-language", "rev-parse", "HEAD"),
                "test_revision": git(WORKSPACE / "mncs-test", "rev-parse", "HEAD"),
                "stdlib_revision": git(WORKSPACE / "mncs-stdlib", "rev-parse", "HEAD"),
                "vm_revision": git(WORKSPACE / "mncs-vm", "rev-parse", "HEAD"),
                "probe_sha256": sha(PROBE.read_bytes()),
                "vm_sha256": sha(support.VM_BIN.read_bytes()),
                "test_script_sha256": sha(Path(__file__).read_bytes()),
            },
            "experiments": {
                "cold_build": {
                    "cache": cold.get("cache"),
                    "verification_work": cold_work,
                    "metrics": cold_metrics,
                    "trace_event_count": len(cold_events),
                },
                "oversized_index_fallback": {
                    "index_bytes_before_read": oversized_bytes,
                    "semantic_candidate_lookup": lookup[0],
                    "cache": edited.get("cache"),
                    "verification_work": edited_work,
                    "metrics": edited_metrics,
                    "trace_event_count": len(edited_events),
                    "rewritten_index_bytes": len(rewritten_index_bytes),
                    "rewritten_index_sha256": sha(rewritten_index_bytes),
                },
                "canonical_vm": {
                    "cold_artifact_identity": cold["artifact"]["identity"],
                    "fallback_artifact_identity": edited["artifact"]["identity"],
                    "cold_result_sha256": sha(json.dumps(cold_actual, sort_keys=True).encode()),
                    "fallback_result_sha256": sha(json.dumps(edited_actual, sort_keys=True).encode()),
                    "cold_receipt_artifact_id": cold_receipt.get("artifact_id"),
                    "fallback_receipt_artifact_id": edited_receipt.get("artifact_id"),
                    "cold_metrics": cold_vm_metrics,
                    "fallback_metrics": edited_vm_metrics,
                },
            },
            "limitations": [
                "This verifies bounded index reading and safe full-frontend fallback; it does not implement Language-owned verified-fact reuse.",
                "The measured build timings describe this digest workload on this machine only.",
            ],
        }
        EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(json.dumps({
            "evidence": str(EVIDENCE_PATH),
            "cold_wall_seconds": cold_metrics["wall_seconds"],
            "fallback_wall_seconds": edited_metrics["wall_seconds"],
            "cold_peak_rss_bytes": cold_metrics["peak_rss_bytes"],
            "fallback_peak_rss_bytes": edited_metrics["peak_rss_bytes"],
            "cache_index_size_after_fallback": len(rewritten_index_bytes),
            "canonical_vm_outputs_equivalent": True,
        }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
