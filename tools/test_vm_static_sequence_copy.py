#!/usr/bin/env python3
"""Check exact-window copy lowering on a fresh canonical VM application."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_native_application as support


ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.test.vm_static_copy"
FUNCTION = "copy_window"
SOURCE = """mncs 0.18;
module mncs.test.vm_static_copy;
record CopyResult { small: [byte; 8], large: [byte; 320] }
fn copy_window(
    destination: [byte; 8],
    source: [byte; 8],
    large_destination: [byte; 320],
    large_source: [byte; 320]
) -> (result: CopyResult) {
    let unchanged: [byte; 8] = copy_span(destination, 8, source, 8, 0);
    let small: [byte; 8] = copy_span(unchanged, 2, source, 1, 3);
    let large_unchanged: [byte; 320] = copy_span(large_destination, 320, large_source, 320, 0);
    let large: [byte; 320] = copy_span(large_unchanged, 10, large_source, 5, 300);
    return CopyResult { small: small, large: large };
}
"""
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_STATIC_SEQUENCE_COPY_EVIDENCE_PATH",
    "evidence/vm-static-exact-sequence-copy-20261018.json",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def byte_sequence(values: list[int]) -> dict:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in values]}}


def main() -> None:
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing compiler probe or canonical VM: {support.PROBE}, {support.VM_BIN}")
    request = {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/vm_static_copy.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }
    with tempfile.TemporaryDirectory(prefix="mncs-vm-static-copy-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            if built.get("entry", {}).get("status") != "verified":
                raise AssertionError("static copy application was not verified: " + json.dumps(built, indent=2)[:6000])
            artifact = built.get("artifact", {}).get("value")
            if not isinstance(artifact, dict):
                raise AssertionError("compiler did not return a canonical VM artifact")
        finally:
            probe_stderr = probe.close()

        module = artifact.get("code", {}).get("mncs-selected-ssa", {}).get("module", {})
        frozen_module_text = json.dumps(module, sort_keys=True)
        refs = artifact.get("source", {}).get("lowering_refs", [])
        lowering_ref = next((row for row in refs if row.startswith("vm-static-exact-sequence-copy-lowering/2:")), None)
        if lowering_ref is None:
            raise AssertionError("VM artifact omitted its lowered-SSA fingerprint")
        if any(
            rule not in frozen_module_text
            for rule in (
                "vm-static-exact-sequence-copy-to-project-replace/1",
                "vm-static-exact-sequence-copy-to-project-construct/1",
            )
        ):
            raise AssertionError("VM artifact omitted an exact lowering transformation record")

        destination = [10, 11, 12, 13, 14, 15, 16, 17]
        source = [30, 31, 32, 33, 34, 35, 36, 37]
        large_destination = [index % 256 for index in range(320)]
        large_source = [(255 - index) % 256 for index in range(320)]
        expected_small = destination[:2] + source[1:4] + destination[5:]
        expected_large = large_destination[:10] + large_source[5:305] + large_destination[310:]
        expected = {"small": expected_small, "large": expected_large}
        arguments = [
            byte_sequence(destination),
            byte_sequence(source),
            byte_sequence(large_destination),
            byte_sequence(large_source),
        ]
        artifact_path = directory / "application.vm.json"
        arguments_path = directory / "arguments.json"
        type_arguments_path = directory / "type-arguments.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        arguments_path.write_text(json.dumps(arguments, separators=(",", ":")), encoding="utf-8")
        type_arguments_path.write_text("[]", encoding="utf-8")
        started = time.monotonic()
        process = subprocess.run(
            [
                "/usr/bin/time", "-v", str(support.VM_BIN), "run", "--artifact", str(artifact_path),
                "--callable", f"{MODULE}::{FUNCTION}", "--args", str(arguments_path),
                "--type-args", str(type_arguments_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        vm_wall_seconds = time.monotonic() - started
        if process.returncode != 0:
            raise AssertionError(f"canonical VM rejected statically lowered copy: {process.stderr[-3000:]}")
        document = json.loads(process.stdout)
        rss_match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", process.stderr)
        vm_peak_rss_bytes = int(rss_match.group(1)) * 1024 if rss_match else None
        if document.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM did not complete: {document.get('outcome')}")
        if document.get("record", {}).get("artifact_id") != artifact.get("artifact_id"):
            raise AssertionError("VM execution receipt is not bound to the compiled artifact")
        returned = support.decode_wire(support.vm_to_wire(document["record"]["returned"][0]))
        if returned != expected:
            raise AssertionError("copy result differs from source semantics")

        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        vm_revision = subprocess.check_output(["git", "-C", str(support.VM_ROOT), "rev-parse", "HEAD"], text=True).strip()
        report = {
            "schema_version": "mncs.compiler.vm-static-sequence-copy-frontier/1",
            "kind": "compiler-static-exact-sequence-copy-canonical-vm-frontier",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "passed",
            "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
            "compiler": {
                "revision": revision,
                "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
                "worktree": str(ROOT),
                "emitter": "tools/vm_emit.rs",
                "emitter_sha256": sha((ROOT / "tools/vm_emit.rs").read_bytes()),
                "test_harness": "tools/test_vm_static_sequence_copy.py",
                "test_harness_sha256": sha(Path(__file__).read_bytes()),
            },
            "vm": {
                "revision": vm_revision,
                "binary": str(support.VM_BIN),
                "binary_sha256": sha(support.VM_BIN.read_bytes()),
            },
            "source": {
                "module": MODULE,
                "function": FUNCTION,
                "sha256": sha(SOURCE.encode()),
                "destination": destination,
                "source_sequence": source,
                "sequence_copy_operations": [
                    {"destination_offset": 8, "source_offset": 8, "length": 0},
                    {"destination_offset": 2, "source_offset": 1, "length": 3},
                    {"destination_offset": 320, "source_offset": 320, "length": 0},
                    {"destination_offset": 10, "source_offset": 5, "length": 300},
                ],
                "large_destination_length": len(large_destination),
                "large_source_length": len(large_source),
                "large_expected_sha256": sha(bytes(expected_large)),
                "large_actual_sha256": sha(bytes(returned["large"])),
                "small_expected": expected_small,
                "small_actual": returned["small"],
            },
            "verification": {
                "application_build_status": built.get("status"),
                "application_artifact_identity": artifact.get("artifact_id"),
                "application_build_contract": built.get("schema_version"),
                "lowering_ref": lowering_ref,
                "frozen_artifact_contains_lowering_transformation": True,
                "lowering_transformation_recorded": True,
                "vm_outcome": document.get("outcome"),
                "vm_receipt_artifact_identity": document.get("record", {}).get("artifact_id"),
                "result_matches_source_semantics": returned == expected,
                "language_verified_facts_reused": 0,
                "runtime_checked_sequencecopy_transformed": False,
                "large_exact_span_rebuilt_from_projections": True,
            },
            "measurements": {
                "application_build": build_metrics,
                "canonical_vm_wall_seconds": round(vm_wall_seconds, 6),
                "canonical_vm_peak_rss_bytes": vm_peak_rss_bytes,
                "probe_stderr_sha256": sha(probe_stderr.encode()),
            },
            "claim_boundary": (
                "The compiler lowered two static exact-bound byte span copies, including an empty copy at "
                "the end of both exact sequences, into canonical-VM-supported operations. It also lowered a "
                "300-byte exact copy into bounded projections and one sequence construction. The exact "
                "artifact completed on the canonical VM and returned the expected functional-copy result. "
                "Runtime-checked and copies with destinations above the reconstruction limit remain explicit "
                "SequenceCopy instructions, so this "
                "does not complete the dynamic provider application path or add Language-owned "
                "verified-fact reuse."
            ),
        }
        report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
        EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
