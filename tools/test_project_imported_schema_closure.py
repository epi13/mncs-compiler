#!/usr/bin/env python3
"""Focused regression for transitive imported schemas, on-demand provider
signatures, qualified identities, generic arguments, spans, diagnostics,
and consistent normalized semantic hashing across probe transport paths."""

import hashlib
import json
import os
import resource
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_project as project


def _semantic_digest(native):
    """Hash a normalized decoded semantic payload consistently across
    Probe.send and Probe.send_encoded_decoded."""
    modules = []
    for mod in project.flist(native["modules"]):
        flow = mod["flow"]
        proof = flow["proof"]
        ssa = mod["value_ssa"]
        fns = []
        for fn in project.flist(ssa["functions"]):
            ident = fn["identity"]
            ident_str = bytes(ident["bytes"][:ident["len"]]).decode("utf-8", errors="replace") if ident["valid"] else ""
            fns.append({
                "identity": ident_str,
                "supported": fn["supported"],
                "verified": fn["verified"],
                "block_count": fn["block_count"],
                "value_count": fn["value_count"],
                "imported_record_count": len(project.flist(fn["imported_records"])),
                "imported_enum_count": len(project.flist(fn["imported_enums"])),
            })
        first_call = ssa["first_call_identity"]
        first_call_str = (
            bytes(first_call["bytes"][:first_call["len"]]).decode("utf-8", errors="replace")
            if first_call["valid"] else ""
        )
        modules.append({
            "source_id": bytes(mod["source_id"]).decode(),
            "source_index": mod["source_index"],
            "proof_ok": proof["ok"],
            "proof_err_span": [proof["err_start"], proof["err_end"]],
            "flow_valid": flow["valid"],
            "ssa_valid": ssa["valid"],
            "ssa_function_count": ssa["function_count"],
            "ssa_supported_function_count": ssa["supported_function_count"],
            "ssa_verified_function_count": ssa["verified_function_count"],
            "ssa_call_count": ssa["call_count"],
            "first_call_identity": first_call_str,
            "functions": fns,
        })
    normalized = {
        "valid": native["valid"],
        "value_ssa_valid": native["value_ssa_valid"],
        "source_count": native["source_count"],
        "module_count": native["module_count"],
        "import_count": native["import_count"],
        "diagnostics": project.flist(native["diagnostics"]),
        "modules": modules,
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), normalized


def _build_sources(root_body, *, extra_leaf_distractors=True):
    distractors = ""
    if extra_leaf_distractors:
        distractors = "\n".join(
            f"record UnusedRec{i} {{ a: u64, b: bool, c: u64 }}\n"
            f"enum UnusedEnum{i} {{ UOne {{ x: u64 }}, UTwo }}\n"
            f"fn unused_leaf_fn{i}(x: u64) -> (r: u64) {{ return x + {i}; }}"
            for i in range(12)
        )
    leaf = (
        "mncs 0.18;\n"
        "module demo.leaf;\n"
        "record Leaf { val: u64, flag: bool }\n"
        "enum Tag { Lit { n: u64 }, Empty }\n"
        + distractors + "\n"
        "fn make_leaf(x: u64) -> (r: Leaf) { return Leaf { val: x, flag: true }; }\n"
        "fn make_tag(x: u64) -> (r: Tag) { if x == 0 { return Tag.Empty; } return Tag.Lit { n: x }; }\n"
    )
    mid = (
        "mncs 0.18;\n"
        "module demo.mid;\n"
        "use demo.leaf as leaf;\n"
        "record Mid { item: leaf.Leaf, tag: leaf.Tag, items: [leaf.Leaf; up_to 4] }\n"
        "record UnusedMid { z: u64 }\n"
        "fn make_mid(x: u64) -> (r: Mid) {\n"
        "    let item: leaf.Leaf = leaf.make_leaf(x);\n"
        "    let tag: leaf.Tag = leaf.make_tag(x);\n"
        "    return Mid { item: item, tag: tag, items: [item] };\n"
        "}\n"
        "fn count_seq<N: Nat>(xs: [u64; up_to N]) -> (r: u64) { return xs.len; }\n"
        "fn unused_mid_fn(k: u64) -> (r: u64) { return k + 1; }\n"
    )
    root = (
        "mncs 0.18;\n"
        "module demo.root;\n"
        "use demo.mid as mid;\n"
        + root_body
    )
    return [
        ("a-leaf.mncs", Path("a-leaf.mncs"), leaf.encode()),
        ("b-mid.mncs", Path("b-mid.mncs"), mid.encode()),
        ("c-root.mncs", Path("c-root.mncs"), root.encode()),
    ]


def _oracle_check(probe, sources, target_index):
    import re
    def mod_name(raw):
        m = re.search(rb"(?m)^\s*module\s+([^;\s]+)\s*;", raw)
        return m.group(1).decode()
    module_map = {
        mod_name(raw): raw.decode()
        for i, (_, _, raw) in enumerate(sources) if i != target_index
    }
    return probe.send({"project_oracle": {
        "root": sources[target_index][2].decode(),
        "modules": module_map,
    }})


def main():
    if os.name == "posix":
        budget = int(os.environ.get("MNCS_FRONTIER_MAX_ADDRESS_SPACE_BYTES", str(8 * 1024**3)))
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        for lim in (soft, hard):
            if lim != resource.RLIM_INFINITY:
                budget = min(budget, lim)
        resource.setrlimit(resource.RLIMIT_AS, (budget, hard))
        os.environ.setdefault("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", str(budget))

    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = project.PROJECT_MODULE
    os.environ["MNCS_PROBE_BACKEND_ROOTS"] = "compile_project_target"
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": project.PROJECT_MODULE,
            "function": "compile_project_target",
            "type_arguments": [
                {"kind": "nat", "value": 2048},
                {"kind": "nat", "value": 1024},
            ],
        },
    ])

    positive_root = (
        "fn pure_helper(a: u64) -> (r: u64) { return a + 1; }\n"
        "fn read_transitive_record(x: u64) -> (r: u64) {\n"
        "    let m: mid.Mid = mid.make_mid(x);\n"
        "    return pure_helper(m.item.val);\n"
        "}\n"
        "fn read_transitive_enum(x: u64) -> (r: u64) {\n"
        "    let m: mid.Mid = mid.make_mid(x);\n"
        "    return match m.tag {\n"
        "        Lit { n: v } => v,\n"
        "        Empty => 0\n"
        "    };\n"
        "}\n"
        "fn call_generic_seq(a: u64, b: u64) -> (r: u64) {\n"
        "    let xs: [u64; up_to 4] = [a, b];\n"
        "    return mid.count_seq<4>(xs);\n"
        "}\n"
    )
    negative_unknown_field = (
        "fn bad_field(x: u64) -> (r: u64) {\n"
        "    let m: mid.Mid = mid.make_mid(x);\n"
        "    return m.item.missing;\n"
        "}\n"
    )
    negative_bad_variant = (
        "fn bad_variant(x: u64) -> (r: u64) {\n"
        "    let m: mid.Mid = mid.make_mid(x);\n"
        "    return match m.tag {\n"
        "        Lit { n: v } => v,\n"
        "        NoSuch => 0\n"
        "    };\n"
        "}\n"
    )

    probe = project.Probe()
    try:
        status = probe.send({"execution_status": True})
        identities = project.identity_map(probe)

        # 1. Positive transitive closure case via BOTH send_encoded_decoded and Probe.send
        pos_sources = _build_sources(positive_root, extra_leaf_distractors=True)
        encoded = project.encode_project_request_bytes(
            identities, pos_sources, stride=1024, page_bound=2048, target_index=2,
        )
        resp_enc = probe.send_encoded_decoded(encoded, module=project.PROJECT_MODULE)
        assert resp_enc.get("status") == "returned", resp_enc
        nat_enc = resp_enc["returned"][0]
        digest_enc, norm_enc = _semantic_digest(nat_enc)

        # Same request via older Probe.send wire path
        req_old = project.request_value(identities, pos_sources, stride=1024)
        req_old["target"] = {"module": project.PROJECT_MODULE, "function": "compile_project_target"}
        req_old["type_arguments"] = [
            {"kind": "nat", "value": 2048},
            {"kind": "nat", "value": 1024},
        ]
        req_old["arguments"].append(project.integer(2))
        resp_old = probe.send(req_old)
        assert resp_old.get("status") == "returned", resp_old
        nat_old = project.decode(resp_old["returned"][0])
        digest_old, norm_old = _semantic_digest(nat_old)

        assert digest_enc == digest_old, f"normalized semantic digest mismatch: {digest_enc} != {digest_old}"
        assert norm_enc["valid"] is True and norm_enc["value_ssa_valid"] is True, norm_enc
        mod = norm_enc["modules"][0]
        assert mod["proof_ok"] is True and mod["flow_valid"] is True and mod["ssa_valid"] is True, mod
        assert mod["ssa_function_count"] == 4 and mod["ssa_verified_function_count"] == 4, mod
        assert mod["first_call_identity"] in {
            "mncs:0.2:function:demo.mid::make_mid",
            "mncs:0.2:function:demo.mid::count_seq",
            "mncs:0.2:function:demo.root::pure_helper",
        }, mod
        # Verify per-function schema trimming: pure_helper retains 0 imported schemas,
        # read_transitive_record retains only Mid+Leaf (2 records, 0 enums),
        # read_transitive_enum retains only Mid+Tag (1 record, 1 enum), NOT the 12+ unused ones!
        by_fn = {f["identity"]: f for f in mod["functions"]}
        pure_fn = by_fn["mncs:0.2:function:demo.root::pure_helper"]
        rec_fn = by_fn["mncs:0.2:function:demo.root::read_transitive_record"]
        enum_fn = by_fn["mncs:0.2:function:demo.root::read_transitive_enum"]
        gen_fn = by_fn["mncs:0.2:function:demo.root::call_generic_seq"]
        assert pure_fn["imported_record_count"] == 0 and pure_fn["imported_enum_count"] == 0, pure_fn
        assert rec_fn["imported_record_count"] == 2 and rec_fn["imported_enum_count"] == 0, rec_fn
        assert enum_fn["imported_record_count"] == 1 and enum_fn["imported_enum_count"] == 1, enum_fn
        assert gen_fn["imported_record_count"] == 0 and gen_fn["imported_enum_count"] == 0, gen_fn

        # 2. Determinism without distractors: identical semantic verdict & call identity
        pos_clean = _build_sources(positive_root, extra_leaf_distractors=False)
        enc_clean = project.encode_project_request_bytes(
            identities, pos_clean, stride=1024, page_bound=2048, target_index=2,
        )
        resp_clean = probe.send_encoded_decoded(enc_clean, module=project.PROJECT_MODULE)
        digest_clean, norm_clean = _semantic_digest(resp_clean["returned"][0])
        assert digest_clean == digest_enc, "distractor declarations altered normalized semantic output!"

        # Stage-0 oracle agrees on positive case
        oracle_pos = _oracle_check(probe, pos_sources, 2)
        assert oracle_pos.get("valid") is True, oracle_pos

        # 3. Negative case: unknown field on transitive imported record (exact span!)
        neg1_sources = _build_sources(negative_unknown_field, extra_leaf_distractors=True)
        enc_neg1 = project.encode_project_request_bytes(
            identities, neg1_sources, stride=1024, page_bound=2048, target_index=2,
        )
        resp_neg1 = probe.send_encoded_decoded(enc_neg1, module=project.PROJECT_MODULE)
        digest_neg1, norm_neg1 = _semantic_digest(resp_neg1["returned"][0])
        assert norm_neg1["valid"] is False and norm_neg1["value_ssa_valid"] is False, norm_neg1
        assert norm_neg1["modules"][0]["proof_ok"] is False, norm_neg1
        neg1_raw = neg1_sources[2][2]
        err_s, err_e = norm_neg1["modules"][0]["proof_err_span"]
        assert b"missing" in neg1_raw[err_s:err_e], (err_s, err_e, neg1_raw[err_s:err_e])
        oracle_neg1 = _oracle_check(probe, neg1_sources, 2)
        assert oracle_neg1.get("valid") is False, oracle_neg1

        # 4. Negative case: unknown variant on transitive imported enum (exact span!)
        neg2_sources = _build_sources(negative_bad_variant, extra_leaf_distractors=True)
        enc_neg2 = project.encode_project_request_bytes(
            identities, neg2_sources, stride=1024, page_bound=2048, target_index=2,
        )
        resp_neg2 = probe.send_encoded_decoded(enc_neg2, module=project.PROJECT_MODULE)
        digest_neg2, norm_neg2 = _semantic_digest(resp_neg2["returned"][0])
        assert norm_neg2["valid"] is False and norm_neg2["value_ssa_valid"] is False, norm_neg2
        assert norm_neg2["modules"][0]["proof_ok"] is False, norm_neg2
        neg2_raw = neg2_sources[2][2]
        err2_s, err2_e = norm_neg2["modules"][0]["proof_err_span"]
        assert b"NoSuch" in neg2_raw[err2_s:err2_e], (err2_s, err2_e, neg2_raw[err2_s:err2_e])
        oracle_neg2 = _oracle_check(probe, neg2_sources, 2)
        assert oracle_neg2.get("valid") is False, oracle_neg2

        print(json.dumps({
            "status": "passed",
            "backend": status.get("backend"),
            "positive_normalized_semantic_sha256": digest_enc,
            "encoded_vs_legacy_digest_equal": digest_enc == digest_old,
            "distractor_invariant_digest_equal": digest_clean == digest_enc,
            "pure_fn_retained_schemas": {
                "records": pure_fn["imported_record_count"],
                "enums": pure_fn["imported_enum_count"],
            },
            "transitive_record_fn_retained_schemas": {
                "records": rec_fn["imported_record_count"],
                "enums": rec_fn["imported_enum_count"],
            },
            "transitive_enum_fn_retained_schemas": {
                "records": enum_fn["imported_record_count"],
                "enums": enum_fn["imported_enum_count"],
            },
            "negative_unknown_field_span": [err_s, err_e],
            "negative_bad_variant_span": [err2_s, err2_e],
            "stage0_oracle_agrees": True,
        }, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
