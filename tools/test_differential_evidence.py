#!/usr/bin/env python3
"""Focused tests for differential parity aggregation and ambient diagnosis capture."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import differential_evidence as de  # noqa: E402

EXPECTED_FIVE = (
    "valid_unit",
    "qualified_imported_type",
    "page_split_inside_decl",
    "malformed_declaration",
    "malformed_split_decl",
)


class DifferentialEvidenceTests(unittest.TestCase):
    def test_all_expected_cases_pass(self) -> None:
        cases = {name: {"matches_stage0": True} for name in EXPECTED_FIVE}
        summary = de.summarize_differential_cases(EXPECTED_FIVE, cases)
        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["all_expected_match_stage0"])
        self.assertEqual(summary["passed_count"], 5)
        self.assertEqual(summary["missing_cases"], [])
        self.assertEqual(summary["failed_cases"], [])
        self.assertEqual(summary["duplicate_cases"], [])

    def test_missing_case_is_incomplete(self) -> None:
        cases = {name: {"matches_stage0": True} for name in EXPECTED_FIVE[:-1]}
        summary = de.summarize_differential_cases(EXPECTED_FIVE, cases)
        self.assertEqual(summary["status"], "incomplete")
        self.assertFalse(summary["all_expected_match_stage0"])
        self.assertEqual(summary["passed_count"], 4)
        self.assertEqual(summary["missing_cases"], ["malformed_split_decl"])
        self.assertEqual(summary["failed_cases"], [])

    def test_failed_case_fails_aggregate(self) -> None:
        cases = {name: {"matches_stage0": True} for name in EXPECTED_FIVE}
        cases["malformed_declaration"] = {"matches_stage0": False}
        summary = de.summarize_differential_cases(EXPECTED_FIVE, cases)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["all_expected_match_stage0"])
        self.assertEqual(summary["passed_count"], 4)
        self.assertEqual(summary["failed_cases"], ["malformed_declaration"])
        self.assertEqual(summary["missing_cases"], [])

    def test_duplicate_case_fails_aggregate(self) -> None:
        cases = [{"name": name, "matches_stage0": True} for name in EXPECTED_FIVE]
        cases.append({"name": "valid_unit", "matches_stage0": True})
        summary = de.summarize_differential_cases(EXPECTED_FIVE, cases)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["all_expected_match_stage0"])
        self.assertEqual(summary["duplicate_cases"], ["valid_unit"])

    def test_reconciles_committed_parse_unit_signatures_receipt(self) -> None:
        evidence_path = REPO_ROOT / "evidence/self-host-stage1-declaration-parse-unit-signatures-20261122.json"
        doc = json.loads(evidence_path.read_text(encoding="utf-8"))
        cases = (
            doc["step3_unit_passthrough_and_real_parse_unit_signatures"]
            ["parse_unit_signatures_4_32"]
            ["stage0_differential_executions"]
            ["cases"]
        )
        summary = de.summarize_differential_cases(EXPECTED_FIVE, cases)
        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["all_expected_match_stage0"])
        self.assertEqual(summary["passed_count"], 5)

    def test_extracts_ambient_compiler_investigation(self) -> None:
        entry = {
            "session_id": "ses_example",
            "diagnostic": {
                "compiler_investigation": {
                    "status": "executed",
                    "freshness": "current",
                    "reused": True,
                    "delivery_completeness": "complete",
                    "provenance": {
                        "target": "mncs:0.2:function:mncs.test.check_unit_caller.v1::call_check_unit",
                        "revision": "7dc49e462a20",
                        "specialization": [4, 32],
                        "last_completed_phase": "execution",
                        "failing_phase": None,
                        "artifact_id": "sha256:abc",
                    },
                    "measurements": {
                        "heavyweight_compile_launched": False,
                        "provider_launches": 0,
                    },
                }
            },
        }
        extracted = de.extract_ambient_compiler_investigation(entry, strict=True)
        self.assertTrue(extracted["present"])
        self.assertEqual(extracted["source_path"], "diagnostic.compiler_investigation")
        self.assertEqual(extracted["status"], "executed")
        self.assertEqual(extracted["last_completed_phase"], "execution")
        self.assertEqual(extracted["revision"], "7dc49e462a20")
        self.assertTrue(extracted["reused"])


if __name__ == "__main__":
    unittest.main()
