#!/usr/bin/env python3
"""Verify the production Test provider application's multi-capability boundary.

This slice compiles the real provider entry and executes its invalid-argument
path through the canonical VM with no capabilities admitted. It proves that
the VM runner can validate one capability identity in an artifact whose other
host calls use a separate identity, while the MNCS application rejects the
bad context before performing effects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_native_application as support
import test_vm_test_digest_app as digest_app

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = digest_app.TEST_ROOT
STDLIB_ROOT = digest_app.STDLIB_ROOT
ENTRY_MODULE = "mncs.test.provider"
ENTRY_FUNCTION = "provider_batch_app"
ROOT_SOURCE_PATH = TEST_ROOT / "native/mncs/test/provider.mncs"
MODULE_PATHS = {
    "mncs.test.assertions": TEST_ROOT / "native/mncs/test/assertions.mncs",
    "mncs.test.provider_protocol.v1": TEST_ROOT / "native/mncs/test/provider_protocol/v1.mncs",
    "mncs.core.sequences.v1": STDLIB_ROOT / "library/core/sequences.mncs",
    "mncs.std.application.v1": STDLIB_ROOT / "library/std/application.mncs",
}
EVIDENCE_PATH = ROOT / "evidence/native-test-provider-app-boundary-20261017.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def byte_sequence(text: str) -> dict:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in text.encode()]}}


def unsigned(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def wire_record(name: str, fields: dict, identity: str) -> dict:
    return {
        "record": {
            "name": name,
            "type_identity": identity,
            "fields": [[key, value] for key, value in sorted(fields.items())],
        },
    }


def compile_request(root: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/provider.mncs",
        "root_source": root,
        "entry": {"module": ENTRY_MODULE, "function": ENTRY_FUNCTION},
        "modules": modules,
        "generic_seeds": [],
        "capabilities": ["provider_artifact", "provider_digest"],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def application_context(interface: dict, current_dir: Path) -> dict:
    context_type = next(
        row for row in interface["types"]["records"]
        if row.get("name") == "ApplicationContext"
    )
    return wire_record("ApplicationContext", {
        "application_identity": {"sequence": {"values": [
            {"byte": {"value": 0}} for _ in range(32)
        ]}},
        # The production entry requires three artifact paths. Two are enough
        # to prove its boundary rejects the context before any host effect.
        "argv": {"sequence": {"values": [
            byte_sequence("request.json"),
            byte_sequence("result.json"),
        ]}},
        "argv_count": unsigned(2),
        "current_dir": byte_sequence(str(current_dir)),
        "environment": {"sequence": {"values": []}},
        "environment_count": unsigned(0),
        "stdin": byte_sequence(""),
    }, context_type["identity"])


def call_request(context: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": ENTRY_MODULE, "function": ENTRY_FUNCTION},
        "arguments": [context],
        "type_arguments": [],
        "step_budget": 200_000,
    }


def publish_environment_artifacts(payload: bytes) -> list[dict]:
    raw_root = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
    if not raw_root:
        return []
    artifact_root = Path(raw_root)
    try:
        root_mode = artifact_root.lstat().st_mode
    except OSError as error:
        raise RuntimeError("Environment artifact directory is unavailable") from error
    if not stat.S_ISDIR(root_mode):
        raise RuntimeError("Environment artifact root must be a real directory")
    name = EVIDENCE_PATH.name
    destination = artifact_root / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise RuntimeError(f"cannot write bounded Environment artifact {name}") from error
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
    return [{"path": name, "byte_count": len(payload), "sha256": sha(payload)}]


def expect_runner_rejection(binary: Path, artifact_path: Path, identity: str,
                            alias: str, request_path: Path, input_path: Path,
                            output_path: Path, expected: str) -> str:
    process = subprocess.run([
        str(binary), str(artifact_path), identity, alias, "single",
        str(request_path), str(input_path), str(output_path), "none",
    ], cwd=ROOT, capture_output=True, text=True, check=False)
    if process.returncode == 0 or expected not in process.stderr:
        raise AssertionError(
            f"runner accepted or misdiagnosed invalid capability binding: "
            f"rc={process.returncode}, stderr={process.stderr[-2000:]}"
        )
    return expected


def main() -> None:
    if not support.PROBE.is_file() or not digest_app.RUNNER_SOURCE.is_file():
        raise SystemExit("pinned Stage-0 probe or native VM runner source is missing")
    root = ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {name: path.read_text(encoding="utf-8") for name, path in MODULE_PATHS.items()}
    request = compile_request(root, modules)

    with tempfile.TemporaryDirectory(prefix="native-provider-app-build-") as raw_cache:
        cache = Path(raw_cache)
        probe = support.Probe(cache / "program-cache", application_cache_dir=cache / "application-cache")
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            interface = built.get("entry", {}).get("interface", {})
            if (built.get("schema_version") != "mncs.compiler.application-build/4"
                    or built.get("status") != "verified"
                    or built.get("unresolved_obligations") != []
                    or interface.get("status") != "verified"):
                raise AssertionError(json.dumps({
                    "schema_version": built.get("schema_version"),
                    "status": built.get("status"),
                    "interface_status": interface.get("status"),
                    "unresolved_obligations": built.get("unresolved_obligations"),
                    "diagnostics": built.get("diagnostics"),
                }, indent=2)[:10000])
            artifact = built.get("artifact", {}).get("value")
            if not isinstance(artifact, dict):
                raise AssertionError("verified provider app build omitted its VM artifact")
            artifact_identity = built["artifact"]["identity"]
            if artifact.get("artifact_id") != artifact_identity:
                raise AssertionError("provider app artifact identity differs from compiler receipt")

            dispatch = built.get("capability_dispatch", {})
            rows = dispatch.get("bindings", [])
            host_rows = [row for row in rows if row.get("operation") in {
                "structured_read", "structured_write", "structured_digest",
            }]
            expected_operations = {
                "provider_artifact": {"structured_read", "structured_write"},
                "provider_digest": {"structured_digest"},
            }
            actual_operations: dict[str, set[str]] = {}
            canonical_identities: dict[str, set[str]] = {}
            for row in host_rows:
                if row.get("runtime_key_kind") != "source-alias":
                    raise AssertionError(f"host operation lacks a source-alias binding: {row}")
                actual_operations.setdefault(row.get("runtime_capability_key"), set()).add(row.get("operation"))
                canonical_identities.setdefault(row.get("runtime_capability_key"), set()).add(
                    row.get("capability_identity")
                )
            if (dispatch.get("schema_version") != "mncs.compiler.capability-dispatch/1"
                    or dispatch.get("status") != "verified"
                    or actual_operations != expected_operations):
                raise AssertionError(f"provider app capability dispatch differs from source contract: {dispatch}")
            artifact_identity_for_files = next(
                row["capability_identity"] for row in host_rows
                if row.get("runtime_capability_key") == "provider_artifact"
                and row.get("operation") == "structured_read"
            )
            if artifact_identity_for_files not in artifact.get("requirements", {}).get("capabilities", []):
                raise AssertionError("artifact requirements omit the provider_artifact host-call identity")
        finally:
            probe.close()

    binary, runner_sha, runner_lock_sha = digest_app.build_runner()
    artifact_fd, artifact_name = tempfile.mkstemp(prefix="provider-app-", suffix=".vm.json")
    os.close(artifact_fd)
    artifact_path = Path(artifact_name)
    try:
        artifact_bytes = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
        artifact_path.write_bytes(artifact_bytes)
        with tempfile.TemporaryDirectory(prefix="native-provider-app-call-") as raw:
            call_dir = Path(raw)
            context = application_context(interface, call_dir)
            call = call_request(context)
            request_path = call_dir / "execution.json"
            request_path.write_text(json.dumps(call, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            absent_input = call_dir / "request.json"
            absent_output = call_dir / "result.json"
            wrong_identity_diagnostic = expect_runner_rejection(
                binary, artifact_path, artifact_identity_for_files + ":mismatch",
                "provider_artifact", request_path, absent_input, absent_output,
                "artifact does not require authorized capability",
            )
            wrong_alias_diagnostic = expect_runner_rejection(
                binary, artifact_path, artifact_identity_for_files,
                "provider_artifact:mismatch", request_path, absent_input, absent_output,
                "application contract dispatch alias differs from admitted SSA",
            )
            command = [
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                artifact_identity_for_files, "provider_artifact", "single",
                str(request_path), str(absent_input), str(absent_output), "none",
            ]
            started = time.monotonic()
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            wall_seconds = time.monotonic() - started
            if process.returncode != 0:
                raise AssertionError(f"canonical VM provider app call failed: {process.stderr[-4000:]}")
            document = json.loads(process.stdout)
            returned_wire = document.get("returned_wire", [])
            returned = [digest_app.support.decode_wire(value) for value in returned_wire]
            record = document.get("record", {})
            outcome = document.get("outcome", {})
            if (outcome.get("kind") != "completed"
                    or record.get("artifact_id") != artifact["artifact_id"]
                    or len(returned) != 1
                    or returned[0].get("exit_code") != 2
                    or bytes(returned[0].get("stderr", [])).decode() != "invalid provider args"
                    or record.get("effects") != []
                    or record.get("admitted_capabilities") != []
                    or absent_output.exists()):
                raise AssertionError(json.dumps({
                    "outcome": outcome,
                    "record": record,
                    "returned": returned,
                    "output_exists": absent_output.exists(),
                }, indent=2)[:10000])

            oracle_cache = ROOT / ".build/native-app-readiness/provider-app-oracle-cache"
            oracle_cache.mkdir(parents=True, exist_ok=True)
            oracle_probe = support.Probe(oracle_cache)
            try:
                oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                    "root": root,
                    "modules": modules,
                    "calls": [call],
                }}, timeout=120)
            finally:
                oracle_probe.close()
            expected_wires = oracle.get("executions", [])
            if (not oracle.get("valid") or len(expected_wires) != 1
                    or expected_wires[0].get("status") != "returned"
                    or expected_wires[0].get("returned") != returned_wire):
                raise AssertionError(f"Stage-0 provider app rejection differs from canonical VM: {oracle}")

            rss_match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", process.stderr)
            user_match = re.search(r"User time \(seconds\): ([0-9.]+)", process.stderr)
            sys_match = re.search(r"System time \(seconds\): ([0-9.]+)", process.stderr)
            source_rows = [
                {"module": name, "path": str(path), "sha256": sha(path.read_bytes())}
                for name, path in sorted(MODULE_PATHS.items())
            ]
            source_rows.append({"module": ENTRY_MODULE, "path": str(ROOT_SOURCE_PATH),
                                "sha256": sha(ROOT_SOURCE_PATH.read_bytes())})
            evidence = {
                "schema_version": "mncs.compiler.native-application-proof/2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "objective": "Compile the production mncs-test provider application and verify its multi-capability dispatch boundary and native invalid-argument behavior",
                "application": {
                    "root_source_id": "native/mncs/test/provider.mncs",
                    "entry": f"{ENTRY_MODULE}::{ENTRY_FUNCTION}",
                    "module_sources": source_rows,
                    "target": "mncs-vm-0.1",
                    "capability_dispatch": {
                        "status": dispatch.get("status"),
                        "host_operation_aliases": {
                            alias: sorted(operations) for alias, operations in sorted(actual_operations.items())
                        },
                        "canonical_capability_identities": {
                            alias: sorted(identities)
                            for alias, identities in sorted(canonical_identities.items())
                        },
                        "validated_artifact_identity": artifact_identity,
                    },
                },
                "producer": {
                    "compiler_revision": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "language_revision": subprocess.check_output(["git", "-C", str(digest_app.LANGUAGE_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "test_revision": subprocess.check_output(["git", "-C", str(TEST_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "stdlib_revision": subprocess.check_output(["git", "-C", str(STDLIB_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "vm_revision": subprocess.check_output(["git", "-C", str(digest_app.VM_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "stage0_probe_sha256": sha(support.PROBE.read_bytes()),
                    "vm_runner_source_sha256": sha(digest_app.RUNNER_SOURCE.read_bytes()),
                    "verification_harness_sha256": sha(Path(__file__).read_bytes()),
                    "vm_runner_binary_sha256": runner_sha,
                    "vm_runner_lockfile_sha256": runner_lock_sha,
                },
                "build": {
                    "request_schema": request["schema_version"],
                    "result_schema": built["schema_version"],
                    "status": built["status"],
                    "artifact_identity": artifact_identity,
                    "artifact_sha256": sha(artifact_bytes),
                    "artifact_bytes": len(artifact_bytes),
                    "unresolved_obligations": built.get("unresolved_obligations"),
                    "metrics": build_metrics,
                },
                "execution": {
                    "outcome": outcome,
                    "application_exit": returned[0],
                    "effects": record.get("effects"),
                    "admitted_capabilities": record.get("admitted_capabilities"),
                    "no_output_publication": not absent_output.exists(),
                    "stage0_exact_return_match": True,
                    "stage0_oracle_metrics": oracle_metrics,
                    "vm_wall_seconds": round(wall_seconds, 6),
                    "vm_user_seconds": float(user_match.group(1)) if user_match else None,
                    "vm_system_seconds": float(sys_match.group(1)) if sys_match else None,
                    "vm_peak_rss_bytes": int(rss_match.group(1)) * 1024 if rss_match else None,
                    "capability_admission": "none; the application rejects argv_count=2 before any host call",
                    "wrong_identity_rejected": wrong_identity_diagnostic,
                    "wrong_alias_rejected": wrong_alias_diagnostic,
                },
                "limitations": [
                    "This verifies only the native invalid-argument path. The positive provider batch still requires an exact structured_digest provider and the authorized Forge/Environment/Store route."
                ],
                "language_verified_facts_reused": 0,
                "self_consumption_frontier_advanced": False,
            }
            evidence_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
            EVIDENCE_PATH.write_bytes(evidence_bytes)
            environment_artifacts = publish_environment_artifacts(evidence_bytes)
            print(json.dumps({
                "evidence": str(EVIDENCE_PATH),
                "environment_artifacts": environment_artifacts,
                "artifact_identity": artifact_identity,
                "vm_wall_seconds": evidence["execution"]["vm_wall_seconds"],
                "vm_peak_rss_bytes": evidence["execution"]["vm_peak_rss_bytes"],
                "capability_aliases": evidence["application"]["capability_dispatch"]["host_operation_aliases"],
                "stage0_exact_return_match": True,
            }, indent=2))
    finally:
        artifact_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
