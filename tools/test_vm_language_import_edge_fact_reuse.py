#!/usr/bin/env python3
"""Verify Language fact reuse when a real application adds an import edge."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def workspace_root() -> Path:
    configured = os.environ.get("MNCS_PROJECTS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    for candidate in (ROOT, *ROOT.parents):
        if ((candidate / "mncs-test/native/mncs/test/digest.mncs").is_file()
                and (candidate / "mncs-vm/Cargo.toml").is_file()):
            return candidate
    raise RuntimeError("set MNCS_PROJECTS_ROOT to the MNCS Projects directory")


WORKSPACE = workspace_root()
os.environ.setdefault("MNCS_PROJECTS_ROOT", str(WORKSPACE))
os.environ.setdefault("MNCS_TEST_ROOT", str(WORKSPACE / "mncs-test"))
os.environ.setdefault("MNCS_STDLIB_ROOT", str(WORKSPACE / "mncs-stdlib"))
os.environ.setdefault("MNCS_LANGUAGE_ROOT", str(WORKSPACE / "mncs-language"))
os.environ.setdefault("MNCS_VM_ROOT", str(WORKSPACE / "mncs-vm"))
os.environ.setdefault(
    "MNCS_NATIVE_APP_VM_BIN",
    str(WORKSPACE / "mncs-vm/target/debug/mncs-vm"),
)

from ensure_locked_stage0_probe import ensure_locked_stage0_probe  # noqa: E402

PROBE = ensure_locked_stage0_probe(ROOT)
os.environ["MNCS_COMPILER_PROBE"] = str(PROBE)

import test_vm_language_declaration_fact_reuse as fact_reuse  # noqa: E402

digest_app = fact_reuse.digest_app
IMPORT_MODULE = "mncs.test.import_probe"
IMPORT_SOURCE = (
    "mncs 0.18;\n"
    "module mncs.test.import_probe;\n"
    "fn unused_value() -> (result: u64) { return 7; }\n"
)
EVIDENCE = Path(os.environ.get(
    "MNCS_LANGUAGE_IMPORT_EDGE_EVIDENCE",
    str(ROOT / "evidence/native-app-readiness-import-edge-fact-reuse-20261024.json"),
)).expanduser().resolve()


def git_head(path: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True,
    ).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_build(label: str, request: dict, work_root: Path, cache_dir: Path) -> tuple[dict, float]:
    request_path = work_root / f"{label}.request.json"
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("MNCS_ENV_OUTPUT_ARTIFACT_DIR", None)
    started = time.monotonic()
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/application_build_provider.py"),
            "build",
            "--request-file",
            str(request_path),
            "--cache-dir",
            str(cache_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    wall_seconds = time.monotonic() - started
    if process.returncode:
        raise RuntimeError(
            f"{label} application build failed ({process.returncode}): "
            f"{process.stderr[-1800:]}"
        )
    response = json.loads(process.stdout)
    if response.get("status") != "verified":
        raise AssertionError(f"{label} build was not verified: {response.get('status')}")
    if response.get("schema_version") != "mncs.compiler.application-build/16":
        raise AssertionError(f"{label} returned an unexpected build contract")
    return response, wall_seconds


def main() -> None:
    probe = PROBE.resolve(strict=True)
    vm_bin = Path(os.environ["MNCS_NATIVE_APP_VM_BIN"]).resolve(strict=True)
    if not probe.is_relative_to(ROOT) or not os.access(probe, os.X_OK):
        raise RuntimeError("selected Stage-0 probe is outside Compiler or not executable")
    if not vm_bin.is_relative_to(Path(os.environ["MNCS_VM_ROOT"]).resolve(strict=True)):
        raise RuntimeError("selected canonical VM binary is outside its VM checkout")

    language_root = Path(os.environ["MNCS_LANGUAGE_ROOT"]).resolve(strict=True)
    language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    language_revision = git_head(language_root)
    if language_revision != language_lock.get("revision"):
        raise RuntimeError("selected Language checkout differs from Compiler's lock")

    base_request = fact_reuse.build_digest_app_request()
    base_request["schema_version"] = "mncs.compiler.application-build-request/13"
    variant_request = copy.deepcopy(base_request)
    import_block = "use mncs.test.suite;\n"
    if variant_request["root_source"].count(import_block) != 1:
        raise RuntimeError("digest_app import block changed; update this focused fixture")
    variant_request["root_source"] = variant_request["root_source"].replace(
        import_block, import_block + f"use {IMPORT_MODULE};\n", 1,
    )
    variant_request["modules"][IMPORT_MODULE] = IMPORT_SOURCE

    with tempfile.TemporaryDirectory(
        prefix="application-import-edge-fact-reuse-", dir=ROOT / ".build",
    ) as raw_work:
        work_root = Path(raw_work)
        cache_dir = work_root / "cache"
        baseline, baseline_wall = run_build("baseline", base_request, work_root, cache_dir)
        changed, changed_wall = run_build("import_added", variant_request, work_root, cache_dir)

        baseline_work = baseline["cache"]["verification_work"]
        changed_work = changed["cache"]["verification_work"]
        baseline_facts = set(baseline_work.get("recomputed_semantic_facts", []))
        changed_reused = set(changed_work.get("reused_semantic_facts", []))
        changed_recomputed = set(changed_work.get("recomputed_semantic_facts", []))
        expected_new_fact = f"mncs:0.2:body:{IMPORT_MODULE}::unused_value"
        if (not baseline_facts or changed_reused != baseline_facts
                or changed_recomputed != {expected_new_fact}
                or baseline_work.get("front_end_invocations") != 1
                or changed_work.get("front_end_invocations") != 1):
            raise AssertionError(
                "import edge did not preserve the exact prior verified body facts: "
                f"baseline={len(baseline_facts)} reused={len(changed_reused)} "
                f"recomputed={sorted(changed_recomputed)}"
            )

        base_modules = set(base_request["modules"])
        parse = changed_work["source_parse_cache"]["observation"]
        elaboration = changed_work["module_elaboration_cache"]["observation"]
        if (changed_work.get("resolved_module_count")
                != baseline_work.get("resolved_module_count") + 1
                or len(parse.get("parsed_source_identities", [])) != 2
                or len(parse.get("reused_source_identities", [])) != len(base_modules)
                or set(elaboration.get("linked_module_subjects", [])) != {IMPORT_MODULE}
                or set(elaboration.get("reused_module_subjects", [])) != base_modules):
            raise AssertionError(
                "import edit did not refresh only the changed source and module closure"
            )
        if (baseline["source_snapshot"]["identity"]
                == changed["source_snapshot"]["identity"]
                or baseline["artifact"]["identity"] == changed["artifact"]["identity"]):
            raise AssertionError("import edit did not change source and artifact identities")

        runner_raw = os.environ.get("MNCS_NATIVE_APP_EXECUTION_RUNNER")
        runner_lock_sha256 = None
        if runner_raw:
            runner = Path(runner_raw).resolve(strict=True)
        else:
            runner, _runner_sha256, runner_lock_sha256 = digest_app.build_runner()
        runner = runner.resolve(strict=True)
        if not os.access(runner, os.X_OK):
            raise RuntimeError("selected application execution runner is not executable")

        fixture = json.loads(digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
        artifact_path = work_root / "import-edge-artifact.vm.json"
        execution = fact_reuse.verify_digest_app_variant_execution(
            {"response": changed},
            "import_edge_added",
            runner,
            fixture["execution"]["inputs"][0],
            fixture["execution"]["stage0_results"][0][0],
            artifact_path,
        )
        admission_process = subprocess.run(
            [str(vm_bin), "admit", "--artifact", str(artifact_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        admission = json.loads(admission_process.stdout)
        if (admission_process.returncode != 0 or admission.get("status") != "admitted"
                or admission.get("artifact_id") != changed["artifact"]["identity"]):
            raise AssertionError(f"selected canonical VM refused the changed artifact: {admission}")

        vm_description = json.loads(subprocess.check_output(
            [str(vm_bin), "describe"], text=True, timeout=20,
        ))
        runner_sha256 = sha256(runner)
        evidence = {
            "schema_version": "mncs.compiler.application-import-edge-fact-reuse/1",
            "identity": "ev_compiler_native_application_import_edge_fact_reuse_20261024",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "contracts": {
                "request": base_request["schema_version"],
                "response": changed["schema_version"],
                "verification_work": changed_work["schema_version"],
                "language_function_body_fact": (
                    changed_work["language_function_body_verification_fact_schema"]
                ),
                "source_parse_cache": changed_work["source_parse_cache"]["schema_version"],
                "module_elaboration_cache": (
                    changed_work["module_elaboration_cache"]["schema_version"]
                ),
            },
            "toolchains": {
                "compiler_head": git_head(ROOT),
                "stage0_probe_sha256": sha256(probe),
                "language_head": language_revision,
                "language_lock_revision": language_lock["revision"],
                "test_head": git_head(digest_app.TEST_ROOT),
                "stdlib_head": git_head(digest_app.STDLIB_ROOT),
                "vm_head": git_head(Path(os.environ["MNCS_VM_ROOT"])),
                "vm_binary_sha256": sha256(vm_bin),
                "vm_build_origin_identity": vm_description.get("build_origin", {}).get("identity"),
                "application_runner_sha256": runner_sha256,
                "application_runner_lock_sha256": runner_lock_sha256,
                "application_build_provider_sha256": sha256(
                    ROOT / "tools/application_build_provider.py"
                ),
                "test_harness_sha256": sha256(Path(__file__).resolve()),
            },
            "source_delta": {
                "kind": "add_one_unused_imported_module",
                "new_module": IMPORT_MODULE,
                "new_function": "unused_value",
                "new_body_fact": expected_new_fact,
            },
            "baseline": {
                "status": baseline["status"],
                "source_snapshot_identity": baseline["source_snapshot"]["identity"],
                "artifact_identity": baseline["artifact"]["identity"],
                "resolved_module_count": baseline_work["resolved_module_count"],
                "body_facts_recomputed": sorted(baseline_facts),
                "build_wall_seconds": baseline_wall,
            },
            "import_edit": {
                "status": changed["status"],
                "source_snapshot_identity": changed["source_snapshot"]["identity"],
                "artifact_identity": changed["artifact"]["identity"],
                "resolved_module_count": changed_work["resolved_module_count"],
                "body_facts_reused": sorted(changed_reused),
                "body_facts_recomputed": sorted(changed_recomputed),
                "parsed_source_count": len(parse.get("parsed_source_identities", [])),
                "reused_source_count": len(parse.get("reused_source_identities", [])),
                "linked_modules": sorted(elaboration.get("linked_module_subjects", [])),
                "reused_modules": sorted(elaboration.get("reused_module_subjects", [])),
                "build_wall_seconds": changed_wall,
            },
            "execution": {
                "canonical_vm_runner_status": execution["status"],
                "application_return": execution["application_exit"],
                "result_matches_stage0": execution["result_matches_stage0"],
                "output_sha256": execution["output_sha256"],
                "vm_runner_metrics": execution["vm_metrics"],
                "selected_vm_cli_admission": admission,
            },
            "limitations": [
                "This local selected-toolchain run does not exercise Environment Store publication or the Forge provider route.",
                "The extra imported module is intentionally uncalled; this isolates module-resolution refresh from function dependency invalidation.",
            ],
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "status": "passed",
            "evidence": str(EVIDENCE),
            "reused_body_fact_count": len(changed_reused),
            "recomputed_body_facts": sorted(changed_recomputed),
            "resolved_module_count": changed_work["resolved_module_count"],
            "canonical_vm_result_matches_stage0": execution["result_matches_stage0"],
            "vm_cli_admission": admission["status"],
        }, sort_keys=True))


if __name__ == "__main__":
    main()
