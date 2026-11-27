#!/usr/bin/env python3
"""Focused unit tests for tools/native_vm_artifact.py decoding helpers."""

from pathlib import Path
import sys
import unittest

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import native_vm_artifact as nva  # noqa: E402


def _sem_id(text: str) -> dict:
    raw = text.encode("utf-8")
    return {"bytes": list(raw), "len": len(raw), "valid": True}


def _identity_chunks(text: str, chunk_size: int = 64) -> dict:
    raw = text.encode("utf-8")
    cur: dict = {"$v": "ICNil", "$p": {}}
    parts = [raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)]
    for part in reversed(parts):
        cur = {
            "$v": "ICCons",
            "$p": {
                "head": {"bytes": list(part), "len": len(part)},
                "tail": cur,
            },
        }
    return cur


class DecodeLongIdentityMapTests(unittest.TestCase):
    def test_empty_returns_empty_mapping(self) -> None:
        self.assertEqual(nva._decode_long_identity_map({}), {})
        self.assertEqual(
            nva._decode_long_identity_map({"long_identities": {"$v": "LINil", "$p": {}}}),
            {},
        )

    def test_multi_chunk_long_identity_reconstructs_and_resolves(self) -> None:
        key1 = "mncs:0.2:record-type-ref:pkg.main::LargeState"
        full1 = "mncs:0.2:record-type:pkg.main::LargeState::" + ("a%3Au64%3B" * 60)
        key2 = "mncs:0.2:record-type-ref:pkg.main::Wrapper"
        full2 = "mncs:0.2:record-type:pkg.main::Wrapper::inner%3A" + ("b" * 520) + "%3B"
        self.assertGreater(len(full1), 512)
        self.assertGreater(len(full2), 512)

        vm_emit_result = {
            "long_identities": {
                "$v": "LICons",
                "$p": {
                    "head": {
                        "key": _sem_id(key1),
                        "full_len": len(full1.encode("utf-8")),
                        "chunks": _identity_chunks(full1, chunk_size=128),
                    },
                    "tail": {
                        "$v": "LICons",
                        "$p": {
                            "head": {
                                "key": _sem_id(key2),
                                "full_len": len(full2.encode("utf-8")),
                                "chunks": _identity_chunks(full2, chunk_size=100),
                            },
                            "tail": {"$v": "LINil", "$p": {}},
                        },
                    },
                },
            }
        }
        decoded = nva._decode_long_identity_map(vm_emit_result)
        self.assertEqual(decoded, {key1: full1, key2: full2})
        self.assertEqual(nva._resolve_semantic_id_str(_sem_id(key1), decoded), full1)
        self.assertEqual(
            nva._resolve_semantic_id_str(_sem_id("mncs:0.2:record-type:pkg::Small::x"), decoded),
            "mncs:0.2:record-type:pkg::Small::x",
        )

    def test_mismatched_full_len_fails(self) -> None:
        key = "mncs:0.2:record-type-ref:pkg.main::Mismatch"
        full = "x" * 600
        vm_emit_result = {
            "long_identities": {
                "$v": "LICons",
                "$p": {
                    "head": {
                        "key": _sem_id(key),
                        "full_len": 599,
                        "chunks": _identity_chunks(full, chunk_size=200),
                    },
                    "tail": {"$v": "LINil", "$p": {}},
                },
            }
        }
        with self.assertRaises(AssertionError):
            nva._decode_long_identity_map(vm_emit_result)


class BodyTypeAggregateSequenceTests(unittest.TestCase):
    def test_kind_8_record_sequence_with_viindex_and_visome(self) -> None:
        rec_id = "mncs:0.2:record-type:pkg.main::Item::val%3Au64%3B"
        exact_vi_index = {
            "valid": True,
            "kind": 8,
            "nominal_identity": {"$v": "VIIndex", "$p": {"index": 3}},
            "bound_kind": 1,
            "bound_value": 16,
        }
        self.assertEqual(
            nva._body_type(
                exact_vi_index,
                record_names={rec_id: "ItemAlias"},
                rec_by_index={3: rec_id},
            ),
            {
                "sequence": {
                    "element": {"record": {"identity": rec_id, "name": "ItemAlias"}},
                    "bound": {"exact": 16},
                }
            },
        )

        upto_vi_some = {
            "valid": True,
            "kind": 8,
            "nominal_identity": {"$v": "VISome", "$p": {"value": _sem_id(rec_id)}},
            "bound_kind": 2,
            "bound_value": 32,
        }
        self.assertEqual(
            nva._body_type(upto_vi_some),
            {
                "sequence": {
                    "element": {"record": {"identity": rec_id, "name": "Item"}},
                    "bound": {"up_to": 32},
                }
            },
        )

    def test_kind_9_finite_sequence_with_viindex_and_visome(self) -> None:
        fin_id = "mncs:0.2:finite-type:pkg.main::StepState::Idle%2COk"
        exact_vi_index = {
            "valid": True,
            "kind": 9,
            "nominal_identity": {"$v": "VIIndex", "$p": {"index": 0}},
            "bound_kind": 1,
            "bound_value": 4,
        }
        self.assertEqual(
            nva._body_type(
                exact_vi_index,
                finite_names={fin_id: "StepState"},
                fin_by_index={0: fin_id},
            ),
            {
                "sequence": {
                    "element": {"finite": {"identity": fin_id, "name": "StepState"}},
                    "bound": {"exact": 4},
                }
            },
        )

        upto_vi_some = {
            "valid": True,
            "kind": 9,
            "nominal_identity": {"$v": "VISome", "$p": {"value": _sem_id(fin_id)}},
            "bound_kind": 2,
            "bound_value": 8,
        }
        self.assertEqual(
            nva._body_type(upto_vi_some),
            {
                "sequence": {
                    "element": {"finite": {"identity": fin_id, "name": "StepState"}},
                    "bound": {"up_to": 8},
                }
            },
        )

    def test_kind_8_and_9_reject_invalid_bounds_and_unresolved_indices(self) -> None:
        rec_id = "mncs:0.2:record-type:pkg.main::Item::val%3Au64%3B"
        fin_id = "mncs:0.2:finite-type:pkg.main::Mode::A"
        with self.assertRaises(AssertionError):
            nva._body_type(
                {
                    "valid": True,
                    "kind": 8,
                    "nominal_identity": {"$v": "VISome", "$p": {"value": _sem_id(rec_id)}},
                    "bound_kind": 1,
                    "bound_value": 0,
                }
            )
        with self.assertRaises(ValueError):
            nva._body_type(
                {
                    "valid": True,
                    "kind": 9,
                    "nominal_identity": {"$v": "VIIndex", "$p": {"index": 7}},
                    "bound_kind": 2,
                    "bound_value": 4,
                },
                fin_by_index={},
            )
        with self.assertRaises(ValueError):
            nva._body_type(
                {
                    "valid": True,
                    "kind": 9,
                    "nominal_identity": {"$v": "VISome", "$p": {"value": _sem_id(fin_id)}},
                    "bound_kind": 99,
                    "bound_value": 4,
                }
            )


if __name__ == "__main__":
    unittest.main()
