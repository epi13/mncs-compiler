#!/usr/bin/env python3
"""Prove a changed Test generic body invalidates a native app specialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import test_vm_native_application as support  # noqa: E402


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request_for(modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": support.APP_SOURCE_ID,
        "root_source": support.APP_SOURCE,
        "entry": {"module": support.APP_MODULE, "function": "run"},
        "modules": modules,
        "generic_seeds": [
            {
                "module": "mncs.test.assertions",
                "function": "equals_bytes",
                "type_arguments": [{"kind": "nat", "value": 4}],
            },
            {
                "module": "mncs.test.suite",
                "function": "summarize",
                "type_arguments": [{"kind": "nat", "value": 4}],
            },
        ],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def verification_work(build: dict, label: str) -> dict:
    work = build.get("cache", {}).get("verification_work")
    if not isinstance(work, dict):
        raise AssertionError(f"{label} omitted verification-work telemetry")
    if work.get("schema_version") != "mncs.compiler.application-build-verification-work/1":
        raise AssertionError(f"{label} returned an unexpected work schema: {work}")
    return work


def execute(probe: support.Probe, modules: dict[str, str], build: dict,
            label: str) -> tuple[dict, dict]:
    interface = build.get("entry", {}).get("interface", {})
    request_type = next(
        (row for row in interface.get("types", {}).get("records", [])
         if row.get("name") == "ReadinessRequest"),
        None,
    )
    if not isinstance(request_type, dict):
        raise AssertionError(f"{label} omitted the structured request type")
    identity = request_type.get("identity")
    if isinstance(identity, dict):
        identity = identity.get("0")
    if not isinstance(identity, str):
        raise AssertionError(f"{label} request type has no semantic identity")

    call = support.call_request([
        support.readiness_request(42, 7, 4, identity)
    ])
    oracle, oracle_metrics = probe.send({"project_oracle": {
        "root": support.APP_SOURCE,
        "modules": modules,
        "calls": [call],
    }})
    if not oracle.get("valid"):
        raise AssertionError(f"{label} Stage-0 oracle rejected: " + json.dumps(
            oracle.get("diagnostics"), indent=2))
    oracle_execution = oracle.get("executions", [{}])[0]
    if oracle_execution.get("status") != "returned":
        raise AssertionError(f"{label} Stage-0 execution failed: " + json.dumps(
            oracle_execution, indent=2))

    artifact = build.get("artifact", {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError(f"{label} has no canonical VM artifact")
    document, vm_metrics = support.run_vm(artifact, call["arguments"])
    if document.get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"{label} VM execution did not complete: " + json.dumps(
            document.get("outcome"), indent=2))
    artifact_id = artifact.get("artifact_id")
    if document.get("record", {}).get("artifact_id") != artifact_id:
        raise AssertionError(f"{label} VM receipt does not bind its artifact")

    expected = [support.decode_wire(value)
                for value in oracle_execution.get("returned", [])]
    actual = [support.decode_wire(support.vm_to_wire(value))
              for value in document.get("record", {}).get("returned", [])]
    if actual != expected:
        raise AssertionError(f"{label} VM differs from Stage-0: " + json.dumps({
            "actual": actual, "stage0": expected,
        }, indent=2))
    metrics = {"stage0": oracle_metrics, "canonical_vm": vm_metrics}
    return actual[0], metrics


def main() -> None:
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing pinned probe or VM binary: {support.PROBE}, {support.VM_BIN}")

    modules = {name: path.read_text() for name, path in support.MODULE_FILES.items()}
    original_suite = modules["mncs.test.suite"]
    old_body = "    return summary;\n}"
    new_body = "    return SuiteSummary { ..summary, total: summary.total +% 1 };\n}"
    if original_suite.count(old_body) != 1:
        raise AssertionError("expected one summarize body return anchor")
    changed_modules = dict(modules)
    changed_suite = original_suite.replace(old_body, new_body, 1)
    changed_modules["mncs.test.suite"] = changed_suite

    cache_parent = ROOT / ".build/native-app-readiness"
    cache_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="generic-specialization-invalidation-", dir=cache_parent) as raw:
        cache_root = Path(raw)
        probe = support.Probe(
            cache_root / "probe-cache",
            application_cache_dir=cache_root / "application-cache",
        )
        try:
            baseline_request = request_for(modules)
            baseline, baseline_build_metrics = probe.send({
                "compile_application": baseline_request,
            })
            if (baseline.get("status") != "verified"
                    or baseline.get("entry", {}).get("status") != "verified"):
                raise AssertionError("baseline Test application failed verification: " +
                                     json.dumps(baseline, indent=2)[:6000])
            baseline_work = verification_work(baseline, "baseline")
            if baseline_work.get("front_end_invocations") != 1:
                raise AssertionError(f"baseline did not run the Language frontend: {baseline_work}")
            baseline_value, baseline_execution_metrics = execute(
                probe, modules, baseline, "baseline")

            changed_request = request_for(changed_modules)
            changed, changed_build_metrics = probe.send({
                "compile_application": changed_request,
            })
            if (changed.get("status") != "verified"
                    or changed.get("entry", {}).get("status") != "verified"):
                raise AssertionError("changed Test application failed verification: " +
                                     json.dumps(changed, indent=2)[:6000])
            changed_work = verification_work(changed, "changed specialization")
            if changed.get("cache", {}).get("status") != "miss":
                raise AssertionError("generic body edit reused the source-snapshot cache")
            if changed_work.get("front_end_invocations") != 1:
                raise AssertionError("generic body edit skipped full current-source verification")
            if changed_work.get("reused_semantic_facts") != []:
                raise AssertionError("Compiler reported Language facts without Language evidence")
            if (baseline.get("artifact", {}).get("identity")
                    == changed.get("artifact", {}).get("identity")):
                raise AssertionError("generic specialization edit reused the prior VM artifact")
            changed_value, changed_execution_metrics = execute(
                probe, changed_modules, changed, "changed specialization")

            fresh_probe = support.Probe(
                cache_root / "probe-cache",
                application_cache_dir=cache_root / "application-cache",
            )
            try:
                warm, warm_build_metrics = fresh_probe.send({
                    "compile_application": changed_request,
                })
            finally:
                fresh_probe.close()
            warm_work = verification_work(warm, "fresh-process warm reuse")
            if (warm.get("cache", {}).get("status") != "hit"
                    or warm_work.get("front_end_invocations") != 0
                    or warm.get("artifact", {}).get("identity")
                    != changed.get("artifact", {}).get("identity")):
                raise AssertionError("fresh process did not reuse the exact verified artifact")
            if baseline_value == changed_value:
                raise AssertionError("the generic-body edit did not change the Test result")
            baseline_total = baseline_value["$p"]["report"]["summary"]["total"]
            changed_total = changed_value["$p"]["report"]["summary"]["total"]
            if (baseline_total, changed_total) != (4, 5):
                raise AssertionError(
                    f"expected generic summarize edit to change total 4 -> 5, got "
                    f"{baseline_total} -> {changed_total}"
                )
            probe.close()

        except BaseException:
            try:
                probe.proc.kill()
                probe.proc.wait(timeout=5)
            except Exception:
                pass
            raise

    evidence = {
        "schema_version": 1,
        "kind": "mncs-native-test-generic-specialization-invalidation",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "test_harness": {
            "path": "tools/test_vm_native_generic_specialization_invalidation.py",
            "sha256": sha(Path(__file__).read_bytes()),
        },
        "compiler": {
            "revision": support.git("rev-parse", "HEAD", cwd=ROOT),
            "branch": support.git("branch", "--show-current", cwd=ROOT),
            "stage0_revision": json.loads(
                (ROOT / "mncs-language.lock.json").read_text())["revision"],
            "stage0_probe_sha256": sha(support.PROBE.read_bytes()),
            "vm_revision": support.git("rev-parse", "HEAD", cwd=support.VM_ROOT),
            "vm_sha256": sha(support.VM_BIN.read_bytes()),
            "test_revision": support.git("rev-parse", "HEAD", cwd=support.TEST_ROOT),
        },
        "application": {
            "module": support.APP_MODULE,
            "entry": "run",
            "generic_seed": {
                "module": "mncs.test.suite",
                "function": "summarize",
                "type_arguments": [{"kind": "nat", "value": 4}],
            },
            "edit": {
                "source_module": "mncs.test.suite",
                "function": "summarize<N=4>",
                "before_sha256": sha(original_suite.encode()),
                "after_sha256": sha(changed_suite.encode()),
                "changes_summary_total": [4, 5],
            },
            "baseline_artifact_identity": baseline.get("artifact", {}).get("identity"),
            "changed_artifact_identity": changed.get("artifact", {}).get("identity"),
            "changed_source_snapshot_identity": changed.get("source_snapshot", {}).get("identity"),
            "selected_specialization": [
                row for row in changed.get("artifact", {}).get("value", {}).get("callables", [])
                if row.get("module") == "mncs.test.suite"
                and row.get("name", "").startswith("summarize__spec_")
            ],
        },
        "verification": {
            "baseline": baseline_work,
            "changed_source": changed_work,
            "fresh_process_unchanged_source": warm_work,
            "language_facts_reused": changed_work.get("reused_semantic_facts"),
            "oracle_vm_equivalent": True,
        },
        "measurements": {
            "baseline_build": baseline_build_metrics,
            "baseline_execution": baseline_execution_metrics,
            "changed_build": changed_build_metrics,
            "changed_execution": changed_execution_metrics,
            "fresh_process_warm_build": warm_build_metrics,
        },
        "scope": "Compiler source-snapshot and executable-artifact invalidation for a real Test generic specialization. The changed snapshot still runs the full Stage-0 frontend; no Language verified-fact reuse is claimed.",
        "status": "passed",
    }
    output = ROOT / "evidence/native-app-readiness-test-generic-specialization-invalidation-20261017.json"
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": evidence["status"],
        "baseline_artifact": evidence["application"]["baseline_artifact_identity"],
        "changed_artifact": evidence["application"]["changed_artifact_identity"],
        "changed_frontend_invocations": changed_work.get("front_end_invocations"),
        "fresh_warm_frontend_invocations": warm_work.get("front_end_invocations"),
        "oracle_vm_equivalent": evidence["verification"]["oracle_vm_equivalent"],
        "summary_total": [baseline_total, changed_total],
        "evidence": str(output.relative_to(ROOT)),
    }, indent=2))


if __name__ == "__main__":
    main()
