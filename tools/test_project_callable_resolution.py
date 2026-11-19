#!/usr/bin/env python3
"""Differential regression for pinned Stage-0 callable lookup rules in native
Project (`mncs.compiler.project.v1::compile_project_target`).

Verifies:
- unaliased bare calls (`use demo.leaf; helper(x)`)
- aliased bare calls (`use demo.leaf as lf; helper(x)`)
- qualified transitive re-exports through aliases and unaliased providers
  carrying original declaring-module identity and generic arguments (`<4>`)
- same-identity diamond imports (bare and qualified)
- local shadowing over imported/re-exported callables
- conflicting bare bindings (exact MNE131 / kind 32 and span matching Stage-0)
- unresolved bare and qualified bindings (exact MNE131 / kind 32 and span)
- unchanged real Stdlib `std/utf8_scalar.mncs` with `std/utf8_primitives.mncs`
"""

import argparse
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_project as project
import test_sem as sem


def _find_main_root():
    try:
        common = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        common_path = (ROOT / common).resolve()
        if common_path.name == ".git":
            return common_path.parent
    except (OSError, subprocess.CalledProcessError):
        pass
    return ROOT


MAIN_ROOT = _find_main_root()
STDLIB_ROOT = Path(os.environ.get("MNCS_STDLIB_ROOT", MAIN_ROOT.parent / "mncs-stdlib"))


def _mod_name(raw: bytes) -> str:
    m = re.search(rb"(?m)^\s*module\s+([^;\s]+)\s*;", raw)
    assert m, f"missing module header in {raw[:120]!r}"
    return m.group(1).decode("utf-8")


def _ident_str(ident: dict) -> str:
    if not ident.get("valid"):
        return ""
    return bytes(ident["bytes"][:ident["len"]]).decode("utf-8", errors="replace")


def _collect_call_identities(ssa_summary: dict) -> dict[str, list[str]]:
    by_fn = {}
    for fn in project.flist(ssa_summary["functions"]):
        fn_id = _ident_str(fn["identity"])
        calls = []
        for blk in project.flist(fn["blocks"]):
            for inst in project.flist(blk["instructions"]):
                if inst["kind"] == 3:
                    calls.append(_ident_str(inst["identity"]))
        by_fn[fn_id] = calls
    return by_fn


def _native_fail_diags(proof: dict) -> list[tuple[str, int, int]]:
    out = []
    for obl in project.flist(proof["obls"]):
        if obl["status"] == 1:
            code = sem.KIND_TO_MNE.get(obl["kind"], f"KIND_{obl['kind']}")
            out.append((code, obl["start"], obl["end"]))
    return out


def _stage0_oracle(probe, sources, target_index):
    modules = {
        _mod_name(raw): raw.decode("utf-8")
        for idx, (_, _, raw) in enumerate(sources)
        if idx != target_index
    }
    return probe.send({
        "project_oracle": {
            "root": sources[target_index][2].decode("utf-8"),
            "modules": modules,
        }
    })


def _run_native(probe, identities, sources, target_index):
    encoded = project.encode_project_request_bytes(
        identities, sources, stride=1024, page_bound=1024, target_index=target_index,
    )
    reply = probe.send_encoded_decoded(encoded, module=project.PROJECT_MODULE)
    assert reply.get("status") == "returned", reply
    return reply["returned"][0]


def _make_sources(items: list[tuple[str, str]]):
    # ProjectRequest requires sources ordered lexicographically by source_id.
    return [(f"{idx:02d}_{name}", Path(f"{idx:02d}_{name}"), src.encode("utf-8")) for idx, (name, src) in enumerate(items)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if os.name == "posix":
        budget = int(os.environ.get("MNCS_FRONTIER_MAX_ADDRESS_SPACE_BYTES", str(8 * 1024**3)))
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        for lim in (soft, hard):
            if lim != resource.RLIM_INFINITY:
                budget = min(budget, lim)
        resource.setrlimit(resource.RLIMIT_AS, (budget, hard))
        os.environ.setdefault("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", str(budget))

    os.environ.setdefault("MNCS_LIBRARY_PATH", str(STDLIB_ROOT / "library"))
    os.environ.setdefault("MNCS_PROBE_LIBRARY_MODULES", "mncs.std.text_utf8.v1")
    os.environ.setdefault("MNCS_PROBE_CACHE_DIR", str(MAIN_ROOT / ".build" / "probe-cache"))
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = project.PROJECT_MODULE
    os.environ["MNCS_PROBE_BACKEND_ROOTS"] = "compile_project_target"
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": project.PROJECT_MODULE,
            "function": "compile_project_target",
            "type_arguments": [
                {"kind": "nat", "value": 1024},
                {"kind": "nat", "value": 1024},
            ],
        },
    ])

    leaf_src = (
        "mncs 0.18;\n"
        "module demo.leaf;\n"
        "fn helper(x: u64) -> (r: u64) { return x +% 1; }\n"
        "fn count<N: Nat>(xs: [byte; up_to N]) -> (r: u64) { return xs.len; }\n"
    )
    mid_unaliased_src = (
        "mncs 0.18;\n"
        "module demo.mid;\n"
        "use demo.leaf;\n"
        "fn mid_fn(x: u64) -> (r: u64) { return helper(x); }\n"
    )
    mid_aliased_src = (
        "mncs 0.18;\n"
        "module demo.mid_alias;\n"
        "use demo.leaf as lf;\n"
        "fn mid_fn(x: u64) -> (r: u64) { return lf.helper(x); }\n"
    )
    left_src = (
        "mncs 0.18;\n"
        "module demo.left;\n"
        "use demo.leaf;\n"
        "fn l_fn(x: u64) -> (r: u64) { return helper(x); }\n"
    )
    right_src = (
        "mncs 0.18;\n"
        "module demo.right;\n"
        "use demo.leaf as lf;\n"
        "fn r_fn(x: u64) -> (r: u64) { return lf.helper(x); }\n"
    )
    top_diamond_src = (
        "mncs 0.18;\n"
        "module demo.top;\n"
        "use demo.left;\n"
        "use demo.right;\n"
        "fn t_fn(x: u64) -> (r: u64) { return helper(x); }\n"
    )
    alt_a_src = (
        "mncs 0.18;\n"
        "module demo.alt_a;\n"
        "fn helper(x: u64) -> (r: u64) { return x +% 10; }\n"
    )
    alt_b_src = (
        "mncs 0.18;\n"
        "module demo.alt_b;\n"
        "fn helper(x: u64) -> (r: u64) { return x +% 20; }\n"
    )
    mid_shadow_src = (
        "mncs 0.18;\n"
        "module demo.mid_shadow;\n"
        "use demo.leaf;\n"
        "fn helper(x: u64) -> (r: u64) { return x +% 99; }\n"
    )

    positive_cases = [
        (
            "unaliased_bare_call",
            _make_sources([
                ("leaf.mncs", leaf_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.leaf;\n"
                    "fn entry(x: u64, xs: [byte; up_to 4]) -> (r: u64) {\n"
                    "    return helper(x) +% count<4>(xs);\n"
                    "}\n",
                ),
            ]),
            1,
            {
                "mncs:0.2:function:demo.root::entry": [
                    "mncs:0.2:function:demo.leaf::helper",
                    "mncs:0.2:function:demo.leaf::count",
                ]
            },
        ),
        (
            "aliased_bare_call",
            _make_sources([
                ("leaf.mncs", leaf_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.leaf as lf;\n"
                    "fn entry(x: u64) -> (r: u64) { return helper(x); }\n",
                ),
            ]),
            1,
            {
                "mncs:0.2:function:demo.root::entry": [
                    "mncs:0.2:function:demo.leaf::helper",
                ]
            },
        ),
        (
            "qualified_transitive_reexport_and_generics",
            _make_sources([
                ("leaf.mncs", leaf_src),
                ("mid.mncs", mid_unaliased_src),
                ("mid_alias.mncs", mid_aliased_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.mid as m;\nuse demo.mid_alias as ma;\n"
                    "fn entry(x: u64, xs: [byte; up_to 4]) -> (r: u64) {\n"
                    "    return m.helper(x) +% ma.count<4>(xs);\n"
                    "}\n",
                ),
            ]),
            3,
            {
                "mncs:0.2:function:demo.root::entry": [
                    "mncs:0.2:function:demo.leaf::helper",
                    "mncs:0.2:function:demo.leaf::count",
                ]
            },
        ),
        (
            "same_identity_diamond_bare_and_qualified",
            _make_sources([
                ("leaf.mncs", leaf_src),
                ("left.mncs", left_src),
                ("right.mncs", right_src),
                ("top.mncs", top_diamond_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.left;\nuse demo.right;\nuse demo.top as t;\n"
                    "fn entry(x: u64) -> (r: u64) {\n"
                    "    return helper(x) +% t.helper(x);\n"
                    "}\n",
                ),
            ]),
            4,
            {
                "mncs:0.2:function:demo.root::entry": [
                    "mncs:0.2:function:demo.leaf::helper",
                    "mncs:0.2:function:demo.leaf::helper",
                ]
            },
        ),
        (
            "local_and_provider_shadowing",
            _make_sources([
                ("leaf.mncs", leaf_src),
                ("mid_shadow.mncs", mid_shadow_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.leaf;\nuse demo.mid_shadow as ms;\n"
                    "fn helper(x: u64) -> (r: u64) { return x +% 5; }\n"
                    "fn entry(x: u64) -> (r: u64) {\n"
                    "    return helper(x) +% ms.helper(x);\n"
                    "}\n",
                ),
            ]),
            2,
            {
                "mncs:0.2:function:demo.root::helper": [],
                "mncs:0.2:function:demo.root::entry": [
                    "mncs:0.2:function:demo.root::helper",
                    "mncs:0.2:function:demo.mid_shadow::helper",
                ],
            },
        ),
    ]

    negative_cases = [
        (
            "conflicting_bare_binding",
            _make_sources([
                ("alt_a.mncs", alt_a_src),
                ("alt_b.mncs", alt_b_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.alt_a;\nuse demo.alt_b;\n"
                    "fn entry(x: u64) -> (r: u64) { return helper(x); }\n",
                ),
            ]),
            2,
        ),
        (
            "unresolved_qualified_binding",
            _make_sources([
                ("leaf.mncs", leaf_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.leaf as lf;\n"
                    "fn entry(x: u64) -> (r: u64) { return lf.missing(x); }\n",
                ),
            ]),
            1,
        ),
        (
            "unresolved_bare_binding",
            _make_sources([
                ("leaf.mncs", leaf_src),
                (
                    "root.mncs",
                    "mncs 0.18;\nmodule demo.root;\nuse demo.leaf;\n"
                    "fn entry(x: u64) -> (r: u64) { return missing(x); }\n",
                ),
            ]),
            1,
        ),
    ]

    report = {"status": "passed", "positive_cases": {}, "negative_cases": {}, "utf8_scalar_real": {}}
    probe = project.Probe()
    try:
        identities = project.identity_map(probe)

        for name, sources, target_idx, expected_calls in positive_cases:
            nat = _run_native(probe, identities, sources, target_idx)
            oracle = _stage0_oracle(probe, sources, target_idx)
            assert oracle["valid"] is True, (name, oracle)
            assert nat["valid"] is True and nat["value_ssa_valid"] is True, (name, nat)
            mod = project.flist(nat["modules"])[0]
            proof = mod["flow"]["proof"]
            ssa = mod["value_ssa"]
            assert proof["ok"] is True and mod["flow"]["valid"] is True and ssa["valid"] is True, (name, proof["ok"], ssa["valid"])
            assert ssa["verified_function_count"] == ssa["function_count"], (name, ssa)
            actual_calls = _collect_call_identities(ssa)
            assert actual_calls == expected_calls, f"{name}: calls mismatch: actual={actual_calls} expected={expected_calls}"
            report["positive_cases"][name] = {
                "valid": True,
                "ssa_valid": True,
                "verified_functions": ssa["verified_function_count"],
                "resolved_call_identities": actual_calls,
                "stage0_oracle_valid": oracle["valid"],
            }

        for name, sources, target_idx in negative_cases:
            nat = _run_native(probe, identities, sources, target_idx)
            oracle = _stage0_oracle(probe, sources, target_idx)
            assert oracle["valid"] is False, (name, oracle)
            assert nat["valid"] is False, (name, nat)
            mod = project.flist(nat["modules"])[0]
            native_diags = _native_fail_diags(mod["flow"]["proof"])
            oracle_diags = [(d["code"], d["span"]["start"], d["span"]["end"]) for d in oracle["diagnostics"]]
            assert native_diags == oracle_diags, f"{name}: diags mismatch native={native_diags} stage0={oracle_diags}"
            report["negative_cases"][name] = {
                "valid": False,
                "native_diagnostics": native_diags,
                "stage0_diagnostics": oracle_diags,
                "exact_match": True,
            }

        # Real unchanged Stdlib target: std/utf8_scalar.mncs with std/utf8_primitives.mncs
        prim_path = STDLIB_ROOT / "library" / "std" / "utf8_primitives.mncs"
        scal_path = STDLIB_ROOT / "library" / "std" / "utf8_scalar.mncs"
        prim_bytes = prim_path.read_bytes()
        scal_bytes = scal_path.read_bytes()
        utf8_sources = [
            ("std/utf8_primitives.mncs", prim_path, prim_bytes),
            ("std/utf8_scalar.mncs", scal_path, scal_bytes),
        ]
        nat_utf8 = _run_native(probe, identities, utf8_sources, 1)
        oracle_utf8 = _stage0_oracle(probe, utf8_sources, 1)
        assert oracle_utf8["valid"] is True, oracle_utf8
        assert nat_utf8["valid"] is True and nat_utf8["value_ssa_valid"] is True, nat_utf8
        mod_utf8 = project.flist(nat_utf8["modules"])[0]
        proof_utf8 = mod_utf8["flow"]["proof"]
        flow_utf8 = mod_utf8["flow"]
        ssa_utf8 = mod_utf8["value_ssa"]
        assert proof_utf8["ok"] is True, proof_utf8
        assert flow_utf8["valid"] is True, flow_utf8
        assert ssa_utf8["valid"] is True, ssa_utf8
        assert ssa_utf8["verified_function_count"] == ssa_utf8["function_count"] == 2, ssa_utf8
        utf8_calls = _collect_call_identities(ssa_utf8)
        expected_utf8_calls = {
            "mncs:0.2:function:mncs.std.utf8_scalar.v1::step_scalar_generic": [
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_lead_need",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_first_continuation_valid",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_first_continuation_valid",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::is_utf8_continuation",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_first_continuation_valid",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::is_utf8_continuation",
                "mncs:0.2:function:mncs.std.utf8_primitives.v1::is_utf8_continuation",
            ],
            "mncs:0.2:function:mncs.std.utf8_scalar.v1::is_whitespace_scalar": [],
        }
        assert utf8_calls == expected_utf8_calls, f"utf8_scalar calls mismatch: {utf8_calls}"
        report["utf8_scalar_real"] = {
            "source_sha256": hashlib.sha256(scal_bytes).hexdigest(),
            "provider_sha256": hashlib.sha256(prim_bytes).hexdigest(),
            "native_project_valid": nat_utf8["valid"],
            "native_project_ssa_valid": nat_utf8["value_ssa_valid"],
            "proof_ok": proof_utf8["ok"],
            "flow_valid": flow_utf8["valid"],
            "ssa_valid": ssa_utf8["valid"],
            "ssa_function_count": ssa_utf8["function_count"],
            "ssa_supported_function_count": ssa_utf8["supported_function_count"],
            "ssa_verified_function_count": ssa_utf8["verified_function_count"],
            "ssa_value_count": ssa_utf8["value_count"],
            "ssa_instruction_count": ssa_utf8["instruction_count"],
            "resolved_call_identities": utf8_calls,
            "stage0_oracle_valid": oracle_utf8["valid"],
        }
    finally:
        probe.close()

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
