#!/usr/bin/env python3
"""Focused enum variant-loop differential at and beyond the 64-item fast path."""
import hashlib
import json
import os

import test_decl


def main():
    os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
    probe = test_decl.Probe()
    try:
        status = probe.send({"execution_status": True})
        assert status.get("backend") is None, status
        assert status.get("retained_sessions") == 0, status

        positive_text = test_decl.VARIANT_BOUNDARY_POS
        positive_data = test_decl.source_bytes(positive_text)
        positive = probe.run(
            "decl", "parse_unit", test_decl.logical_args(positive_data)
        )
        positive_oracle = probe.send({"oracle": positive_data.decode()})
        test_decl.check_pos_parse(
            positive, positive_oracle, positive_data, positive_text
        )
        test_decl.check_enum_types(
            positive, positive_oracle, positive_data, positive_text
        )
        enum = test_decl.flist(positive["enums"], 1)[0]
        variants = test_decl.flist(enum["variants"], 1)
        assert len(variants) == 65, len(variants)

        negative_text = test_decl.VARIANT_BOUNDARY_NEG
        negative_data = test_decl.source_bytes(negative_text)
        negative = probe.run(
            "decl", "parse_unit", test_decl.logical_args(negative_data)
        )
        negative_oracle = probe.send({"oracle": negative_data.decode()})
        test_decl.check_neg_parse(negative, negative_oracle, negative_text)
        diagnostic = negative_oracle["diagnostics"][0]

        report = {
            "schema_version": 1,
            "kind": "decl-variant-loop-boundary-differential",
            "stage0_revision": json.loads(
                (test_decl.ROOT / "mncs-language.lock.json").read_text()
            )["revision"],
            "execution_mode": "reference_interpreter",
            "retained_backend": status.get("backend"),
            "positive_variant_count": len(variants),
            "positive_status": "SUCCESS",
            "negative_status": "SUCCESS",
            "negative_first_error_span": [
                diagnostic["span"]["start"], diagnostic["span"]["end"]
            ],
            "native_negative_error_span": [
                negative["err_start"], negative["err_end"]
            ],
            "target_stage": "parse only",
            "compiler_source_sha256": hashlib.sha256(
                (test_decl.ROOT / "src/compiler/decl.mncs").read_bytes()
            ).hexdigest(),
        }
        out = test_decl.ROOT / ".build" / "decl-variant-loop-boundary.json"
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
