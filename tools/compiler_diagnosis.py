#!/usr/bin/env python3
"""Compiler-owned selected-callable diagnosis, provenance, and bootstrap projection.

Composes authoritative facts from existing MNCS provider contracts without
duplicating provider semantics or running heavyweight compiles on read paths:
  - Compiler (`vm_emit.mncs` `VmEmitResult` + `error_detail`): exact emitter
    failure code, phase, callable identity, SSA operation kind, expected vs
    observed type category, observed nominal identity, and source byte span.
  - Language Service (`mncs-language-service/tools/mnls_provider.py`) with
    bound `mncs-language` (`mncs declaration-inventory`) for authoritative
    0.19 source identities, declaration kinds, and line/column spans.
  - Doctor (`mncs-doctor/tools/compiler_vm.py`): provider executable freshness
    and self-host matrix blocker drift detection.
  - Debug (`mncs-debug/bin/mncs-debug`): demand-driven observation readiness
    and source-correlation handoff (expensive traces stay strictly on-demand).
  - Test (`.mncs/verification-obligations.json`): linked native test suites.
  - Bootstrap status (`evidence/SELF-HOST-MATRIX.json`): reported separately
    from invocation provenance, preserving UNKNOWN wherever evidence ends.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "mncs.compiler.selected-callable-diagnosis/1"
CACHE_REL_PATH = Path(".mncs/cache/selected-callable-diagnosis.json")
MATRIX_REL_PATH = Path("evidence/SELF-HOST-MATRIX.json")
OBLIGATIONS_REL_PATH = Path(".mncs/verification-obligations.json")
EMITTER_REL_PATH = Path("src/compiler/vm_emit.mncs")

ERROR_CODE_LABELS: dict[int, str] = {
    0: "ok",
    1: "unverified_or_empty_ssa_or_project",
    2: "unverified_or_unsupported_function",
    3: "unsupported_effect_or_capability",
    4: "unsupported_or_unresolved_type_or_schema",
    5: "unsupported_instruction_kind_or_operator",
    6: "unsupported_terminator",
    7: "invalid_operand_or_cfg_arity_or_type",
    8: "invalid_module_header_span",
    9: "missing_callee_in_reachable_closure",
    10: "callee_signature_mismatch",
    11: "unresolved_or_mismatched_generic_specialization",
}

PHASE_LABELS: dict[int, str] = {
    0: "none",
    1: "project_proof_or_ssa",
    2: "module_header_or_ssa_verify",
    3: "target_callable_selection",
    4: "specialization_nat_binding",
    5: "signature_type_lowering",
    6: "sequence_type_propagation",
    7: "block_param_type_lowering",
    8: "instruction_lowering",
    9: "terminator_lowering",
    10: "bounded_iteration_lowering",
    11: "cfg_type_verification",
    12: "provider_materialization",
    13: "closure_call_verification",
}

TYPE_KIND_LABELS: dict[int, str] = {
    0: "unknown",
    1: "bool",
    2: "byte",
    3: "integer",
    4: "scalar_sequence",
    5: "nominal_record",
    6: "nested_scalar_sequence",
    7: "nominal_finite_enum",
    8: "supported_vm_type",
}

SSA_KIND_LABELS: dict[int, str] = {
    0: "none",
    1: "Constant",
    2: "Binary",
    3: "Call",
    4: "Unary",
    5: "EnumConstruct",
    6: "PayloadProject",
    7: "SequenceRepeat",
    8: "RecordProject",
    9: "IntrinsicOp",
    10: "FieldProject",
    11: "SequenceProject",
    12: "SequenceLength",
    13: "LoopGuard",
    14: "LoopIndexAdvance",
    15: "LoopIndexInit",
    16: "RecordConstructOrUpdate",
    17: "ViewConstruct",
    18: "SequenceConstruct",
    19: "SequenceReplace",
    101: "Terminator::Branch",
    102: "Terminator::Jump",
    103: "Terminator::Return",
    104: "Terminator::Fail",
    105: "Terminator::FiniteSwitch",
    106: "Terminator::ScalarSwitch",
}

MISSING_STAGE1_STAGE2_EVIDENCE = [
    "full_compiler_closure_emitted_through_native_vm_emitter",
    "stage1_compiler_canonical_vm_admission_and_self_compile_run",
    "stage2_output_reproduced_from_stage1_and_compared_for_fixed_point_equivalence",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def git_head(checkout: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def decode_semantic_id(val: Any) -> str:
    if isinstance(val, str):
        return val
    if not isinstance(val, dict):
        return ""
    if not val.get("valid"):
        return ""
    length = int(val.get("len", 0))
    raw = val.get("bytes")
    if isinstance(raw, list) and length > 0:
        return bytes(raw[:length]).decode("utf-8", errors="replace")
    return ""


def resolve_workspace_root(checkout: Path) -> Path:
    cur = checkout.resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / "mncs-language").is_dir() and (parent / "mncs-vm").is_dir():
            return parent
    return cur.parent


def load_bootstrap_status(checkout: Path) -> dict[str, Any]:
    matrix_path = checkout / MATRIX_REL_PATH
    matrix_sha = file_sha256(matrix_path)
    if not matrix_path.is_file():
        return {
            "stage1_to_stage2_rebuild": "UNKNOWN",
            "stage1_claimed": False,
            "stage2_claimed": False,
            "self_host_matrix": {"path": str(MATRIX_REL_PATH), "sha256": "", "status": "missing"},
            "supporting_evidence": None,
            "recorded_first_remaining_blocker": None,
            "missing_rebuild_evidence": list(MISSING_STAGE1_STAGE2_EVIDENCE),
        }
    try:
        data = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "stage1_to_stage2_rebuild": "UNKNOWN",
            "stage1_claimed": False,
            "stage2_claimed": False,
            "self_host_matrix": {"path": str(MATRIX_REL_PATH), "sha256": matrix_sha, "status": "unreadable"},
            "supporting_evidence": None,
            "recorded_first_remaining_blocker": None,
            "missing_rebuild_evidence": list(MISSING_STAGE1_STAGE2_EVIDENCE),
        }

    campaign_keys = [k for k in data.keys() if k.startswith("current_campaign_")]
    latest_key = campaign_keys[-1] if campaign_keys else None
    latest = data.get(latest_key, {}) if latest_key else {}
    stage1_stage2 = str(latest.get("stage1_to_stage2_rebuild") or "UNKNOWN")
    stage1_claimed = bool(latest.get("stage1_claimed", False))
    stage2_claimed = bool(latest.get("stage2_claimed", False))

    ev_rel = latest.get("evidence_artifact")
    ev_info = None
    if isinstance(ev_rel, str) and ev_rel:
        ev_path = checkout / ev_rel
        ev_info = {
            "path": ev_rel,
            "sha256": file_sha256(ev_path),
            "present": ev_path.is_file(),
        }

    return {
        "stage1_to_stage2_rebuild": stage1_stage2,
        "stage1_claimed": stage1_claimed,
        "stage2_claimed": stage2_claimed,
        "self_host_matrix": {
            "path": str(MATRIX_REL_PATH),
            "sha256": matrix_sha,
            "latest_campaign_key": latest_key,
            "status": "present",
        },
        "supporting_evidence": ev_info,
        "recorded_first_remaining_blocker": latest.get("first_remaining_blocker"),
        "missing_rebuild_evidence": (
            list(MISSING_STAGE1_STAGE2_EVIDENCE) if stage1_stage2 == "UNKNOWN" else []
        ),
    }


def _module_to_source_path(checkout: Path, workspace_root: Path, module_name: str) -> Path | None:
    if module_name.startswith("mncs.compiler.") and module_name.endswith(".v1"):
        stem = module_name[len("mncs.compiler."):-len(".v1")]
        cand = checkout / "src" / "compiler" / f"{stem}.mncs"
        if cand.is_file():
            return cand
    if module_name.startswith("mncs.std.") and module_name.endswith(".v1"):
        stem = module_name[len("mncs.std."):-len(".v1")]
        cand = workspace_root / "mncs-stdlib" / "library" / "std" / f"{stem}.mncs"
        if cand.is_file():
            return cand
    return None


def _identity_module(identity: str) -> str:
    if "::" not in identity:
        return ""
    prefix = identity.split("::", 1)[0]
    parts = prefix.split(":")
    return parts[-1] if parts else ""


def _query_language_service(
    checkout: Path,
    workspace_root: Path,
    source_paths: list[Path],
    identities: list[str],
    disabled: set[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int]:
    """Query Language Service (and bound mncs declaration-inventory for 0.19)."""
    launches = 0
    declarations: dict[str, dict[str, Any]] = {}
    if "language_service" in disabled:
        return (
            {
                "provider": "mncs-language-service",
                "status": "unavailable",
                "reason": "provider disabled or not present in environment",
            },
            declarations,
            launches,
        )

    ls_root = Path(os.environ.get("MNCS_LANGUAGE_SERVICE_ROOT") or (workspace_root / "mncs-language-service"))
    mnls_provider = ls_root / "tools" / "mnls_provider.py"
    mncs_bin = Path(os.environ.get("MNCS_REFERENCE_BIN") or (workspace_root / "mncs-language" / "target" / "release" / "mncs"))
    stdlib_lib = workspace_root / "mncs-stdlib" / "library"

    ls_status = "unavailable"
    ls_detail = ""
    ls_profile_mismatch = False

    if mnls_provider.is_file():
        env = dict(os.environ)
        env["MNCS_LANGUAGE_ROOT"] = str(workspace_root / "mncs-language")
        env["MNCS_STDLIB_ROOT"] = str(workspace_root / "mncs-stdlib")
        env["MNCS_LIBRARY_PATH"] = str(stdlib_lib)
        try:
            launches += 1
            st = subprocess.run(
                [sys.executable, str(mnls_provider), "status", "--workspace", str(checkout)],
                env=env, capture_output=True, text=True, timeout=10, check=False,
            )
            if st.returncode == 0 and st.stdout.strip():
                st_doc = json.loads(st.stdout)
                if st_doc.get("ready"):
                    ls_status = "available"
                    ls_detail = "resident language service ready"
                else:
                    ls_status = "resident_absent"
                    ls_detail = str(st_doc.get("detail") or "resident not running")
                    # Probe one document only when a 0.19 compatibility check is needed,
                    # leaving no stray resident running.
                    if source_paths:
                        first_text = source_paths[0].read_text(encoding="utf-8", errors="replace")[:64]
                        if "mncs 0.19;" in first_text:
                            ls_profile_mismatch = True
                            ls_status = "stale_profile_unsupported"
                            ls_detail = (
                                "resident mnls-language-service-host supports profiles 0.1..0.18 "
                                "(MNP008 on mncs 0.19 source); using bound mncs-language declaration-inventory"
                            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            ls_status = "unavailable"
            ls_detail = f"mnls_provider status failed: {exc}"

    # Authoritative declaration-inventory from bound mncs-language (supports 0.19)
    inv_status = "unavailable"
    if mncs_bin.is_file() and source_paths:
        env_inv = dict(os.environ)
        env_inv["MNCS_LIBRARY_PATH"] = f"{stdlib_lib}:{checkout / 'src'}"
        wanted_set = {i for i in identities if i}
        seen_paths: set[Path] = set()
        for sp in source_paths:
            sp_res = sp.resolve()
            if sp_res in seen_paths or not sp_res.is_file():
                continue
            seen_paths.add(sp_res)
            try:
                launches += 1
                res = subprocess.run(
                    [str(mncs_bin), "declaration-inventory", str(sp_res)],
                    env=env_inv, capture_output=True, text=True, timeout=15, check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    doc = json.loads(res.stdout)
                    if doc.get("valid") and isinstance(doc.get("inventory"), dict):
                        inv_status = "available"
                        rel_file = (
                            str(sp_res.relative_to(checkout.resolve()))
                            if sp_res.is_relative_to(checkout.resolve())
                            else str(sp_res)
                        )
                        for decl_item in doc["inventory"].get("declarations", []):
                            did = str(decl_item.get("identity") or "")
                            if not did:
                                continue
                            # Match exact identity or record-type prefix (before field-hash suffix)
                            matched = did in wanted_set or any(
                                w and (did == w or did.startswith(w + "::") or w.startswith(did + "::"))
                                for w in wanted_set
                            )
                            if matched:
                                span = decl_item.get("source_span") or {}
                                entry = {
                                    "identity": did,
                                    "kind": decl_item.get("kind"),
                                    "module": decl_item.get("module"),
                                    "name": decl_item.get("name"),
                                    "file": rel_file,
                                    "line": span.get("line"),
                                    "column": span.get("column"),
                                    "byte_span": [span.get("start"), span.get("end")] if span else None,
                                    "source_profile": doc["inventory"].get("source_profile"),
                                }
                                existing = declarations.get(did)
                                is_declaring = decl_item.get("module") == _identity_module(did)
                                if existing is None or (entry["line"] is not None and (existing.get("line") is None or is_declaring)):
                                    declarations[did] = entry
            except (OSError, ValueError, subprocess.SubprocessError):
                continue

    info = {
        "provider": "mncs-language-service",
        "status": "available" if ls_status == "available" else (
            "partial_fallback_mncs_language" if inv_status == "available" else "unavailable"
        ),
        "resident_status": ls_status,
        "declaration_inventory_status": inv_status,
        "profile_0_19_mismatch": ls_profile_mismatch,
        "detail": ls_detail or ("resolved via bound mncs declaration-inventory" if inv_status == "available" else "unavailable"),
    }
    return info, declarations, launches


def _query_doctor(
    checkout: Path,
    workspace_root: Path,
    bootstrap: dict[str, Any],
    error_callable: str,
    observed_nominal: str,
    disabled: set[str],
) -> tuple[dict[str, Any], int]:
    launches = 0
    if "doctor" in disabled:
        return (
            {
                "provider": "mncs-doctor",
                "status": "unavailable",
                "reason": "provider disabled or not present in environment",
            },
            launches,
        )

    doctor_root = Path(os.environ.get("MNCS_DOCTOR_ROOT") or (workspace_root / "mncs-doctor"))
    doctor_script = doctor_root / "tools" / "compiler_vm.py"
    findings: list[dict[str, Any]] = []
    status = "unavailable"
    compiler_state = "unknown"
    runtime_status = "unknown"

    if doctor_script.is_file():
        env = dict(os.environ)
        env.update({
            "MNCS_COMPILER_CHECKOUT": str(checkout),
            "MNCS_COMPILER_PROBE": str(checkout / ".bootstrap/target/release/mncs-compiler-stage0-probe"),
            "MNCS_VM_CHECKOUT": str(workspace_root / "mncs-vm"),
            "MNCS_VM_BIN": str(workspace_root / "mncs-vm/target/release/mncs-vm"),
            "MNCS_REFERENCE_BIN": str(workspace_root / "mncs-language/target/release/mncs"),
            "MNCS_LANGUAGE_ROOT": str(workspace_root / "mncs-language"),
        })
        # Use main checkout probe if linked worktree does not have its own .bootstrap build
        if not Path(env["MNCS_COMPILER_PROBE"]).is_file():
            main_probe = workspace_root / "mncs-compiler/.bootstrap/target/release/mncs-compiler-stage0-probe"
            if main_probe.is_file():
                env["MNCS_COMPILER_PROBE"] = str(main_probe)
        try:
            launches += 1
            res = subprocess.run(
                [sys.executable, str(doctor_script), "--compact"],
                env=env, capture_output=True, text=True, timeout=15, check=False,
            )
            if res.stdout.strip():
                doc = json.loads(res.stdout)
                status = "available"
                comp = (doc.get("components") or {}).get("compiler") or {}
                rt = (doc.get("components") or {}).get("runtime") or {}
                compiler_state = str(comp.get("state") or "unknown")
                runtime_status = str((rt.get("build_origin") or {}).get("status") or "unknown")
                if compiler_state != "ready":
                    mism = comp.get("mismatches") or []
                    sample = mism[:4]
                    suffix = f" (+{len(mism) - 4} more)" if len(mism) > 4 else ""
                    findings.append({
                        "code": "stale_stage0_probe_receipt",
                        "severity": "warning",
                        "detail": f"Stage-0 probe build receipt state={compiler_state}, mismatches={sample}{suffix}",
                    })
                if runtime_status != "matches-embedded-inputs":
                    findings.append({
                        "code": "stale_vm_build_receipt",
                        "severity": "warning",
                        "detail": f"canonical mncs-vm build_origin status={runtime_status}",
                    })
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            status = "unavailable"
            findings.append({"code": "doctor_invocation_failed", "severity": "warning", "detail": str(exc)})

    # Check whether recorded SELF-HOST-MATRIX first_remaining_blocker contradicts observed refusal
    recorded_blocker = bootstrap.get("recorded_first_remaining_blocker")
    if isinstance(recorded_blocker, dict) and error_callable:
        rec_op = str(recorded_blocker.get("operation_or_type") or "")
        rec_call = str(recorded_blocker.get("callable") or "")
        if ("[Token; 64]" in rec_op or "lexer.v1::lex_batch_from" in rec_call) and (
            "TokenList" in observed_nominal or "lex_tokens_from" in error_callable
        ):
            findings.append({
                "code": "stale_matrix_blocker_description",
                "severity": "warning",
                "detail": (
                    "SELF-HOST-MATRIX.json first_remaining_blocker claims lexer::lex_batch_from / "
                    "[Token; 64], which contradicts live source (segment.lex_batch_from -> "
                    "lexer.lex_tokens_from, BatchResult.tokens: TokenList)"
                ),
            })

    return (
        {
            "provider": "mncs-doctor",
            "status": status,
            "compiler_probe_state": compiler_state,
            "vm_runtime_build_origin": runtime_status,
            "findings": findings,
        },
        launches,
    )


def _query_debug(
    checkout: Path,
    workspace_root: Path,
    target_callable: str,
    error_callable: str,
    disabled: set[str],
) -> tuple[dict[str, Any], int]:
    """Check Debug availability without running expensive traces automatically."""
    if "debug" in disabled:
        return (
            {
                "provider": "mncs-debug",
                "status": "unavailable",
                "mode": "demand_driven",
                "reason": "provider disabled or not present in environment",
            },
            0,
        )
    debug_root = Path(os.environ.get("MNCS_DEBUG_ROOT") or (workspace_root / "mncs-debug"))
    debug_bin = debug_root / "bin" / "mncs-debug"
    if not debug_bin.is_file():
        return (
            {
                "provider": "mncs-debug",
                "status": "unavailable",
                "mode": "demand_driven",
                "reason": f"executable not found at {debug_bin}",
            },
            0,
        )
    return (
        {
            "provider": "mncs-debug",
            "status": "available_demand_driven",
            "mode": "demand_driven",
            "executable": str(debug_bin),
            "retained_witness": None,
            "demand_command": (
                f"{debug_bin} capabilities  # or import-test / record on a failing native test for "
                f"{error_callable or target_callable}"
            ),
            "note": "Expensive execution traces remain strictly demand-driven; emitter facts already identify phase, callable, SSA kind, and source span.",
        },
        0,
    )


def _linked_native_tests(checkout: Path) -> list[str]:
    path = checkout / OBLIGATIONS_REL_PATH
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for ob in doc.get("obligations", []):
            ex = ob.get("executor") or {}
            if ex.get("kind") == "native_first_class_test":
                oid = ob.get("identity")
                if isinstance(oid, str):
                    out.append(oid)
        return out
    except (OSError, ValueError):
        return []


def compute_invalidation_key(
    checkout: Path,
    target_callable: str,
    source_files: dict[str, str],
    toolchain_revision: str,
) -> dict[str, Any]:
    head = git_head(checkout)
    emitter_sha = file_sha256(checkout / EMITTER_REL_PATH)
    matrix_sha = file_sha256(checkout / MATRIX_REL_PATH)
    payload = {
        "compiler_revision": head,
        "emitter_source_sha256": emitter_sha,
        "matrix_sha256": matrix_sha,
        "source_digests": dict(sorted(source_files.items())),
        "target_callable": target_callable,
        "toolchain_revision": toolchain_revision,
    }
    return {
        "digest": canonical_digest(payload),
        "inputs": payload,
    }


def _infer_last_completed_phase(emit_res: dict[str, Any], phase_id: int, admitted: bool | None, executed: bool | None) -> str:
    if emit_res.get("ok"):
        if executed:
            return "execution"
        if admitted:
            return "vm_admission"
        return "emission"
    if phase_id == 1:
        return "none"
    if phase_id == 2:
        return "proof"
    return "ssa_verification"


def build_diagnosis_document(
    checkout: Path,
    *,
    target_callable: str,
    target_specialization: str,
    emit_res: dict[str, Any],
    source_paths: list[Path],
    compiler_entry: str = "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
    admitted: bool | None = None,
    executed: bool | None = None,
    artifact_id: str | None = None,
    disabled_providers: set[str] | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    disabled = disabled_providers or set()
    workspace_root = resolve_workspace_root(checkout)
    bootstrap = load_bootstrap_status(checkout)

    err_code = int(emit_res.get("error_code", 0))
    err_fn = int(emit_res.get("error_function", 0))
    err_blk = int(emit_res.get("error_block", 0))
    err_inst = int(emit_res.get("error_instruction", 0))

    raw_detail = emit_res.get("error_detail") if isinstance(emit_res.get("error_detail"), dict) else {}
    phase_id = int(raw_detail.get("phase", 0))
    ssa_kind_id = int(raw_detail.get("ssa_kind", 0))
    exp_kind_id = int(raw_detail.get("expected_type_kind", 0))
    obs_kind_id = int(raw_detail.get("observed_type_kind", 0))
    err_callable = decode_semantic_id(raw_detail.get("callable"))
    obs_nominal = decode_semantic_id(raw_detail.get("observed_nominal"))
    src_start = int(raw_detail.get("source_start", 0))
    src_end = int(raw_detail.get("source_end", 0))

    # Build source file map and discover declaring files for Language Service lookup
    candidate_paths: list[Path] = [Path(p) for p in source_paths if Path(p).is_file()]
    for ident in (target_callable, err_callable, obs_nominal):
        mod = _identity_module(ident)
        if mod:
            mp = _module_to_source_path(checkout, workspace_root, mod)
            if mp is not None and mp not in candidate_paths:
                candidate_paths.append(mp)

    source_digests: dict[str, str] = {}
    for p in candidate_paths:
        pr = p.resolve()
        key = str(pr.relative_to(checkout.resolve())) if pr.is_relative_to(checkout.resolve()) else str(pr)
        source_digests[key] = file_sha256(pr)

    toolchain_rev = git_head(workspace_root / "mncs-language")
    inv_key = compute_invalidation_key(checkout, target_callable, source_digests, toolchain_rev)

    ls_info, decls, ls_launches = _query_language_service(
        checkout, workspace_root, candidate_paths,
        [target_callable, err_callable, obs_nominal], disabled,
    )
    doc_info, doc_launches = _query_doctor(
        checkout, workspace_root, bootstrap, err_callable, obs_nominal, disabled,
    )
    dbg_info, dbg_launches = _query_debug(
        checkout, workspace_root, target_callable, err_callable, disabled,
    )

    unavailable_providers: list[str] = []
    if ls_info["status"] == "unavailable":
        unavailable_providers.append("mncs-language-service")
    if doc_info["status"] == "unavailable":
        unavailable_providers.append("mncs-doctor")
    if dbg_info["status"] == "unavailable":
        unavailable_providers.append("mncs-debug")

    last_completed = _infer_last_completed_phase(emit_res, phase_id, admitted, executed)
    failing_phase = None if emit_res.get("ok") else PHASE_LABELS.get(phase_id, f"phase_{phase_id}")

    # Resolve source snippet for byte span if available in declaring module
    err_mod = _identity_module(err_callable)
    err_file_path = _module_to_source_path(checkout, workspace_root, err_mod) if err_mod else None
    source_excerpt = None
    source_line = None
    if err_file_path is not None and src_end > src_start:
        raw_bytes = err_file_path.read_bytes()
        if src_end <= len(raw_bytes):
            source_excerpt = raw_bytes[src_start:src_end].decode("utf-8", errors="replace")[:160]
            source_line = raw_bytes[:src_start].count(b"\n") + 1

    stage0_lock = ""
    lock_path = checkout / "mncs-language.lock.json"
    if lock_path.is_file():
        try:
            stage0_lock = str(json.loads(lock_path.read_text(encoding="utf-8")).get("revision") or "")
        except (OSError, ValueError):
            pass

    if emit_res.get("ok"):
        status_label = "executed" if executed else ("admitted" if admitted else "emitted")
    else:
        status_label = "refused"

    elapsed_s = round(time.monotonic() - t0, 3)

    return {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": utcnow(),
        "status": status_label,
        "delivery_completeness": "partial" if unavailable_providers else "complete",
        "unavailable_providers": unavailable_providers,
        "freshness": "current",
        "reused": False,
        "compiler_provenance": {
            "compiler": compiler_entry,
            "compiler_repository": "mncs-compiler",
            "compiler_revision": inv_key["inputs"]["compiler_revision"],
            "emitter_source": str(EMITTER_REL_PATH),
            "emitter_source_sha256": inv_key["inputs"]["emitter_source_sha256"],
            "host_language_revision": toolchain_rev,
            "stage0_lock_revision": stage0_lock,
            "selected_target": target_callable,
            "selected_specialization": target_specialization,
            "last_completed_phase": last_completed,
            "failing_phase": failing_phase,
            "owner": {
                "repository": "mncs-compiler",
                "component": str(EMITTER_REL_PATH),
            },
            "artifact_id": artifact_id,
        },
        "bootstrap_status": bootstrap,
        "diagnosis": {
            "ok": bool(emit_res.get("ok")),
            "error_code": err_code,
            "error_label": ERROR_CODE_LABELS.get(err_code, f"error_{err_code}"),
            "error_phase": failing_phase,
            "error_callable": err_callable or None,
            "error_function_index": err_fn,
            "error_block": err_blk,
            "error_instruction": err_inst,
            "ssa_operation": {
                "kind_id": ssa_kind_id,
                "kind": SSA_KIND_LABELS.get(ssa_kind_id, f"ssa_kind_{ssa_kind_id}"),
                "source_byte_span": [src_start, src_end] if src_end > src_start else None,
                "source_line": source_line,
                "source_excerpt": source_excerpt,
            },
            "types": {
                "expected": TYPE_KIND_LABELS.get(exp_kind_id, f"type_kind_{exp_kind_id}") if exp_kind_id else None,
                "observed": TYPE_KIND_LABELS.get(obs_kind_id, f"type_kind_{obs_kind_id}") if obs_kind_id else None,
                "observed_nominal_identity": obs_nominal or None,
            },
            "authoritative_declarations": decls,
            "function_count": int(emit_res.get("function_count", 0)),
            "block_count": int(emit_res.get("block_count", 0)),
            "instruction_count": int(emit_res.get("instruction_count", 0)),
            "record_type_count": int(emit_res.get("record_type_count", 0)),
            "finite_type_count": int(emit_res.get("finite_type_count", 0)),
        },
        "providers": {
            "compiler": {"provider": "mncs-compiler", "status": "available"},
            "language_service": ls_info,
            "doctor": doc_info,
            "debug": dbg_info,
            "test": {
                "provider": "mncs-test",
                "status": "linked_not_rerun",
                "native_obligations": _linked_native_tests(checkout),
            },
        },
        "invalidation": inv_key,
        "measurements": {
            "provider_launches": ls_launches + doc_launches + dbg_launches,
            "elapsed_seconds": elapsed_s,
            "heavyweight_compile_launched": False,
        },
    }


def evaluate_cached_diagnosis(
    checkout: Path,
    *,
    cache_path: Path | None = None,
    disabled_providers: set[str] | None = None,
) -> dict[str, Any]:
    """Read-only projection for ordinary entry / session re-entry (0 compiles)."""
    t0 = time.monotonic()
    disabled = disabled_providers or set()
    workspace_root = resolve_workspace_root(checkout)
    target_cache = cache_path or (checkout / CACHE_REL_PATH)
    live_bootstrap = load_bootstrap_status(checkout)
    live_head = git_head(checkout)
    live_emitter_sha = file_sha256(checkout / EMITTER_REL_PATH)
    live_toolchain = git_head(workspace_root / "mncs-language")

    if not target_cache.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "recorded_at": utcnow(),
            "status": "no_recorded_diagnosis",
            "freshness": "absent",
            "reused": False,
            "delivery_completeness": "partial" if disabled else "complete",
            "unavailable_providers": sorted(disabled),
            "compiler_provenance": {
                "compiler": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
                "compiler_repository": "mncs-compiler",
                "compiler_revision": live_head,
                "emitter_source": str(EMITTER_REL_PATH),
                "emitter_source_sha256": live_emitter_sha,
                "host_language_revision": live_toolchain,
                "selected_target": None,
                "last_completed_phase": None,
                "failing_phase": None,
            },
            "bootstrap_status": live_bootstrap,
            "diagnosis": None,
            "measurements": {
                "provider_launches": 0,
                "elapsed_seconds": round(time.monotonic() - t0, 3),
                "heavyweight_compile_launched": False,
            },
        }

    doc = json.loads(target_cache.read_text(encoding="utf-8"))
    rec_inv = (doc.get("invalidation") or {}).get("inputs") or {}
    changed: list[str] = []
    if rec_inv.get("compiler_revision") != live_head:
        changed.append("compiler_revision")
    if rec_inv.get("emitter_source_sha256") != live_emitter_sha:
        changed.append(str(EMITTER_REL_PATH))
    if rec_inv.get("matrix_sha256") != file_sha256(checkout / MATRIX_REL_PATH):
        changed.append(str(MATRIX_REL_PATH))
    if rec_inv.get("toolchain_revision") != live_toolchain:
        changed.append("toolchain_revision")
    for rel_src, expected_sha in (rec_inv.get("source_digests") or {}).items():
        p = Path(rel_src)
        full = p if p.is_absolute() else (checkout / p)
        if file_sha256(full) != expected_sha:
            changed.append(rel_src)

    # Refresh unavailable provider flags if caller explicitly tests degraded providers
    unavail = list(doc.get("unavailable_providers") or [])
    for prov_name, label in (
        ("language_service", "mncs-language-service"),
        ("doctor", "mncs-doctor"),
        ("debug", "mncs-debug"),
    ):
        if prov_name in disabled and label not in unavail:
            unavail.append(label)
            if isinstance(doc.get("providers"), dict) and prov_name in doc["providers"]:
                doc["providers"][prov_name] = {
                    "provider": label,
                    "status": "unavailable",
                    "reason": "provider disabled or not present in environment",
                }

    doc["bootstrap_status"] = live_bootstrap
    doc["unavailable_providers"] = sorted(set(unavail))
    doc["delivery_completeness"] = "partial" if doc["unavailable_providers"] else "complete"
    if changed:
        doc["freshness"] = "invalidated"
        doc["reused"] = False
        doc["invalidated_reasons"] = changed
    else:
        doc["freshness"] = "current"
        doc["reused"] = True
        doc.pop("invalidated_reasons", None)
    doc["measurements"] = {
        "provider_launches": 0,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "heavyweight_compile_launched": False,
    }
    return doc


def write_cached_diagnosis(checkout: Path, doc: dict[str, Any], cache_path: Path | None = None) -> Path:
    target = cache_path or (checkout / CACHE_REL_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MNCS Compiler selected-callable diagnosis & provenance projection.")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--disable-provider", action="append", default=[])
    sub = parser.add_subparsers(dest="command", required=True)

    p_proj = sub.add_parser("project", help="Read-only projection of current diagnosis & bootstrap status (0 compiles).")
    p_proj.add_argument("--compact", action="store_true")
    p_proj.add_argument("--checkout", type=Path, default=None, dest="sub_checkout")
    p_proj.add_argument("--cache", type=Path, default=None, dest="sub_cache")
    p_proj.add_argument("--disable-provider", action="append", default=[], dest="sub_disable_provider")

    p_rec = sub.add_parser("record", help="Compose and cache a diagnosis document from an emit-result JSON file.")
    p_rec.add_argument("--emit-result", type=Path, required=True)
    p_rec.add_argument("--target", required=True)
    p_rec.add_argument("--specialization", default="")
    p_rec.add_argument("--source", action="append", default=[])
    p_rec.add_argument("--admitted", action="store_true")
    p_rec.add_argument("--executed", action="store_true")
    p_rec.add_argument("--artifact-id", default=None)

    args = parser.parse_args(argv)
    effective_checkout = getattr(args, "sub_checkout", None) or args.checkout
    effective_cache = getattr(args, "sub_cache", None) or args.cache
    checkout = effective_checkout.resolve()
    disabled = set(args.disable_provider or []) | set(getattr(args, "sub_disable_provider", None) or [])

    if args.command == "project":
        doc = evaluate_cached_diagnosis(checkout, cache_path=effective_cache, disabled_providers=disabled)
        if args.compact:
            print(json.dumps(doc, sort_keys=True, separators=(",", ":")))
        else:
            print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    if args.command == "record":
        emit_res = json.loads(args.emit_result.read_text(encoding="utf-8"))
        if "immediate_response" in emit_res and isinstance(emit_res["immediate_response"], dict):
            emit_res = emit_res["immediate_response"]
        doc = build_diagnosis_document(
            checkout,
            target_callable=args.target,
            target_specialization=args.specialization,
            emit_res=emit_res,
            source_paths=[Path(s) for s in args.source],
            admitted=args.admitted or None,
            executed=args.executed or None,
            artifact_id=args.artifact_id,
            disabled_providers=disabled,
        )
        write_cached_diagnosis(checkout, doc, cache_path=args.cache)
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
