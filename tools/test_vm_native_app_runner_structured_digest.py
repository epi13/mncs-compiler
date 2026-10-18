#!/usr/bin/env python3
"""Exercise the native app runner's bounded structured-digest provider."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
import test_vm_native_application as support
import test_vm_test_digest_app as digest_app

MODULE = "compiler.structured_digest_probe"
FUNCTION = "digest_payload"
SOURCE = """mncs 0.18;
module compiler.structured_digest_probe;

record DigestPayload {
    digest: [byte; 32],
    label: [byte; up_to 64],
    count: u64
}

fn digest_payload(value: DigestPayload) -> (result: [byte; up_to 64])
    capability digest
    effect structured_digest authorized_by digest
{
    return structured_digest(value);
}
"""
EVIDENCE_PATH = ROOT / "evidence/native-app-readiness-structured-digest-provider.json"


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def wire_bytes(payload: bytes) -> dict:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in payload]}}


def main() -> None:
    request = {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "tests/structured_digest_probe.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": ["digest"],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }
    cache = ROOT / ".build/native-app-readiness/structured-digest-probe-cache"
    cache.mkdir(parents=True, exist_ok=True)
    probe = support.Probe(cache / "program-cache", application_cache_dir=cache / "application-cache")
    try:
        built, build_metrics = probe.send({"compile_application": request}, timeout=120)
        interface = built.get("entry", {}).get("interface", {})
        if (built.get("schema_version") != "mncs.compiler.application-build/4"
                or built.get("status") != "verified"
                or built.get("unresolved_obligations") != []
                or interface.get("status") != "verified"):
            raise AssertionError(json.dumps({
                "schema_version": built.get("schema_version"),
                "status": built.get("status"),
                "interface": interface,
                "unresolved_obligations": built.get("unresolved_obligations"),
                "diagnostics": built.get("diagnostics"),
            }, indent=2)[:8000])
        artifact = built.get("artifact", {}).get("value")
        if not isinstance(artifact, dict):
            raise AssertionError("verified structured-digest build omitted its VM artifact")
        artifact_identity = built["artifact"]["identity"]
        dispatch = built.get("capability_dispatch", {})
        binding = next((row for row in dispatch.get("bindings", [])
                        if row.get("operation") == "structured_digest"), None)
        if binding is None or binding.get("runtime_capability_key") != "digest":
            raise AssertionError(f"compiler omitted the structured digest dispatch binding: {dispatch}")
        capability_identity = binding.get("capability_identity")
        if capability_identity not in artifact.get("requirements", {}).get("capabilities", []):
            raise AssertionError("VM artifact requirements omit the verified digest capability identity")

        record = next((row for row in interface.get("types", {}).get("records", [])
                       if row.get("name") == "DigestPayload"), None)
        if not record or not isinstance(record.get("identity"), str):
            raise AssertionError("verified callable interface omitted DigestPayload identity")
        digest_bytes = bytes(range(32))
        label = "MNCS λ"
        argument = {"record": {
            "name": "DigestPayload",
            "type_identity": record["identity"],
            "fields": [
                ["count", {"integer": {"type": {"bits": 64, "signed": False}, "value": 42}}],
                ["digest", wire_bytes(digest_bytes)],
                ["label", wire_bytes(label.encode("utf-8"))],
            ],
        }}
        call = {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": FUNCTION},
            "arguments": [argument],
            "type_arguments": [],
            "step_budget": 100_000,
        }
        oracle_call = {
            **call,
            "policy": {"effects": "realize"},
            "host_grants": [{
                "capability": "digest",
                "locator": "bounded-structured-digest-conformance-fixture",
                "bytes": [],
            }],
        }
        oracle, oracle_metrics = probe.send({"project_oracle": {
            "root": SOURCE,
            "modules": {},
            "calls": [oracle_call],
        }}, timeout=120)
        oracle_rows = oracle.get("executions", [])
        if (not oracle.get("valid") or len(oracle_rows) != 1
                or oracle_rows[0].get("status") != "returned"
                or len(oracle_rows[0].get("returned", [])) != 1):
            raise AssertionError(f"Stage-0 structured-digest oracle failed: {oracle}")
        expected_wire = oracle_rows[0]["returned"][0]

        binary, runner_sha, runner_lock_sha = digest_app.build_runner()
        with tempfile.TemporaryDirectory(prefix="compiler-structured-digest-") as raw:
            directory = Path(raw)
            artifact_path = directory / "application.vm.json"
            request_path = directory / "call.json"
            input_path = directory / "unused-input.json"
            output_path = directory / "unused-output.json"
            artifact_bytes = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
            artifact_path.write_bytes(artifact_bytes)
            request_path.write_text(json.dumps(call, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            command = [
                "/usr/bin/time", "-v", str(binary), str(artifact_path), capability_identity,
                "digest", "single", str(request_path), str(input_path), str(output_path), "success",
            ]
            started = time.monotonic()
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            wall_seconds = time.monotonic() - started
            if process.returncode != 0:
                raise AssertionError(f"canonical VM structured digest call failed: {process.stderr[-4000:]}")
            document = json.loads(process.stdout)
            record_result = document.get("record", {})
            returned = document.get("returned_wire", [])
            effects = record_result.get("effects", [])
            if (document.get("outcome", {}).get("kind") != "completed"
                    or returned != [expected_wire]
                    or record_result.get("admitted_capabilities") != ["digest"]
                    or len(effects) != 1
                    or effects[0].get("request", {}).get("operation") != "structured_digest"
                    or effects[0].get("outcome_tag") != "completed"
                    or input_path.exists() or output_path.exists()):
                raise AssertionError(json.dumps({
                    "outcome": document.get("outcome"),
                    "returned": returned,
                    "expected": expected_wire,
                    "admitted_capabilities": record_result.get("admitted_capabilities"),
                    "effects": effects,
                }, indent=2)[:10000])

            expected_external = {
                "count": 42,
                "digest": digest_bytes.hex(),
                "label": label,
            }
            expected_digest = sha(json.dumps(
                expected_external, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8"))
            returned_bytes = bytes(item["byte"]["value"] for item in returned[0]["sequence"]["values"])
            if returned_bytes.hex() != expected_digest:
                raise AssertionError("structured digest does not match the documented external typed projection")

            rss_match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", process.stderr)
            evidence = {
                "schema_version": "mncs.compiler.native-application-proof/2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "objective": "Execute the canonical VM runner's structured_digest provider on a typed MNCS record",
                "source": {
                    "module": MODULE,
                    "function": FUNCTION,
                    "sha256": sha(SOURCE.encode()),
                    "record_type_identity": record["identity"],
                    "field_projection": expected_external,
                },
                "producer": {
                    "compiler_revision": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "language_revision": subprocess.check_output(["git", "-C", str(digest_app.LANGUAGE_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "vm_revision": subprocess.check_output(["git", "-C", str(digest_app.VM_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "stage0_probe_sha256": sha(support.PROBE.read_bytes()),
                    "vm_runner_source_sha256": sha(digest_app.RUNNER_SOURCE.read_bytes()),
                    "runner_binary_sha256": runner_sha,
                    "runner_lockfile_sha256": runner_lock_sha,
                },
                "build": {
                    "schema_version": built["schema_version"],
                    "status": built["status"],
                    "artifact_identity": artifact_identity,
                    "artifact_sha256": sha(artifact_bytes),
                    "capability_dispatch_identity": capability_identity,
                    "metrics": build_metrics,
                },
                "execution": {
                    "outcome": document.get("outcome"),
                    "effects": effects,
                    "admitted_capabilities": record_result.get("admitted_capabilities"),
                    "returned_digest_hex": returned_bytes.hex(),
                    "expected_digest_hex": expected_digest,
                    "stage0_exact_return_match": True,
                    "stage0_oracle_metrics": oracle_metrics,
                    "vm_wall_seconds": round(wall_seconds, 6),
                    "vm_peak_rss_bytes": int(rss_match.group(1)) * 1024 if rss_match else None,
                },
                "language_verified_facts_reused": 0,
                "self_consumption_frontier_advanced": False,
            }
            EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({
                "evidence": str(EVIDENCE_PATH),
                "artifact_identity": artifact_identity,
                "returned_digest_hex": returned_bytes.hex(),
                "stage0_exact_return_match": True,
                "vm_wall_seconds": evidence["execution"]["vm_wall_seconds"],
                "vm_peak_rss_bytes": evidence["execution"]["vm_peak_rss_bytes"],
            }, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
